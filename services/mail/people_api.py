"""Additive owner-scoped correspondence endpoints; no translation hooks."""
import json
import hashlib
from config import Config
from web_compat import jsonify, request
from .people import authorize, contacts, correspondence, relationships, bilateral, corpus_state
from .person_research import analysis_page
from .person_corpus import (resolve_selection, selection_for_spec, corpus_state as analysis_corpus_state,
    PERSON_DEFAULT_MODEL_CALL_BUDGET, PERSON_MAX_MODEL_CALL_BUDGET)


def register_people_routes(app, services):
    register_network_routes(app, services)
    from .api import _mail_endpoint, _owner, _limit, _offset, _queue
    repo=services.repository

    def filters():
        return {'date_from':request.args.get('date_from'),'date_to':request.args.get('date_to'),
            'q':request.args.get('q',''),'has_attachment':request.args.get('has_attachment'),'participant':request.args.get('participant'),
            'conversation_id':request.args.get('conversation_id')}

    @app.route('/api/mail/packages/<package_id>/people')
    @_mail_endpoint
    def mail_people(package_id):
        # Legacy q searches addresses/names; contact_query opts into the shared
        # mail-content q filter without changing existing callers.
        selected=filters()
        contact_query=request.args.get('contact_query')
        if contact_query is None:contact_query=selected.pop('q')
        return jsonify({'ok':True,**contacts(repo,package_id,_owner(services),contact_query,_offset(request.args.get('offset')),
            _limit(request.args.get('limit'),50,100),view=request.args.get('view','mailbox'),**selected)})

    @app.route('/api/mail/packages/<package_id>/person/messages')
    @_mail_endpoint
    def mail_person_messages(package_id):
        return jsonify({'ok':True,**correspondence(repo,package_id,_owner(services),request.args.get('address'),
            entity_id=request.args.get('entity_id'),direction=request.args.get('direction','all'),
            offset=_offset(request.args.get('offset')),limit=_limit(request.args.get('limit'),25,100),**filters())})

    @app.route('/api/mail/packages/<package_id>/person/relationships')
    @_mail_endpoint
    def mail_person_relationships(package_id):
        return jsonify({'ok':True,**relationships(repo,package_id,_owner(services),request.args.get('address'),
            entity_id=request.args.get('entity_id'),view=request.args.get('view','mailbox'),
            offset=_offset(request.args.get('offset')),limit=_limit(request.args.get('limit'),20,50),**filters())})

    @app.route('/api/mail/packages/<package_id>/person/pair/messages')
    @_mail_endpoint
    def mail_pair_messages(package_id):
        return jsonify({'ok':True,**bilateral(repo,package_id,_owner(services),request.args.get('address'),
            request.args.get('counterpart'),entity_id=request.args.get('entity_id'),counterpart_entity_id=request.args.get('counterpart_entity_id'),
            direction=request.args.get('direction','all'),offset=_offset(request.args.get('offset')),limit=_limit(request.args.get('limit'),25,100),**filters())})

    @app.route('/api/mail/packages/<package_id>/entities/<entity_id>/revoke',methods=['POST'])
    @_mail_endpoint
    def mail_revoke_entity(package_id,entity_id):
        from .graph_metrics import revoke_entity
        payload=request.get_json(silent=True) or {}
        if not isinstance(payload,dict):raise ValueError('请求内容必须是对象')
        return jsonify({'ok':True,**revoke_entity(repo,package_id,_owner(services),entity_id,payload.get('reason',''))})

    @app.route('/api/mail/packages/<package_id>/entities/history')
    @_mail_endpoint
    def mail_entity_history(package_id):
        from .graph_metrics import entity_history
        return jsonify({'ok':True,**entity_history(repo,package_id,_owner(services),request.args.get('entity_id'),
            offset=_offset(request.args.get('offset')),limit=_limit(request.args.get('limit'),50,100))})

    @app.route('/api/mail/packages/<package_id>/entities/<entity_id>/history')
    @_mail_endpoint
    def mail_one_entity_history(package_id,entity_id):
        from .graph_metrics import entity_history
        return jsonify({'ok':True,**entity_history(repo,package_id,_owner(services),entity_id,
            offset=_offset(request.args.get('offset')),limit=_limit(request.args.get('limit'),50,100))})

    @app.route('/api/mail/person-analyses/<turn_id>')
    @_mail_endpoint
    def mail_person_analysis(turn_id):
        owner=_owner(services)
        turn=repo.get_turn(turn_id,owner,include_events=False)
        if turn and turn.get('job_id') and turn['status'] not in {'completed','failed','cancelled'}:
            job=services.storage.get_job(turn['job_id'],owner)
            if job and job.get('status') in {'failed','cancelled'}:
                repo.update_turn(turn_id,owner,status=job['status'],sufficiency='insufficient',error={'message':'后台任务已停止，已完成的批次保留'})
        return jsonify({'ok':True,**analysis_page(repo,turn_id,owner,_offset(request.args.get('offset')),_limit(request.args.get('limit'),10,20))})

    @app.route('/api/mail/person-analyses/<turn_id>/report')
    @_mail_endpoint
    def mail_person_report(turn_id):
        from .person_activity import activity_page
        return jsonify({'ok':True,**activity_page(repo,turn_id,_owner(services))})

    @app.route('/api/mail/person-analyses/<turn_id>/report/download')
    @_mail_endpoint
    def mail_person_report_download(turn_id):
        from .person_activity import activity_page
        from .api import _download
        page=activity_page(repo,turn_id,_owner(services))
        inherited=page['purpose']=='deep_analysis'
        coverage=page['progress'].get('source_coverage',{}) if inherited else page['progress']
        scan_complete=page['progress'].get('source_scan_complete') if inherited else page['progress'].get('scan_complete')
        if (page['status']!='completed' or page['progress'].get('complete') is not True or
            scan_complete is not True or page['report'].get('complete') is not True or
            coverage.get('characters_read')!=page['progress'].get('corpus',{}).get('characters')):
            raise ValueError('全包扫描和报告尚未完成，暂不能下载完整文件')
        return _download(page['report']['markdown'].encode('utf-8-sig'),'person-report-'+turn_id+'.md','text/markdown; charset=utf-8')

    @app.route('/api/mail/packages/<package_id>/person/analyses',methods=['GET','POST'])
    @_mail_endpoint
    def mail_person_analyses(package_id):
        owner=_owner(services);authorize(repo,package_id,owner)
        if request.method=='GET':
            selection=resolve_selection(repo,package_id,owner,request.args.get('address'),request.args.get('entity_id'))
            with repo._connection() as c:
                rows=c.execute('SELECT t.turn_id,t.status,t.question,t.created_at,t.task_spec_json,t.final_json FROM mail_research_turns t JOIN mail_research_sessions s ON s.session_id=t.session_id WHERE s.package_id=? AND t.owner_id=? ORDER BY t.rowid DESC',(package_id,owner))
                items=[]
                for row in rows:
                    spec=json.loads(row['task_spec_json'])
                    if spec.get('mode')!='person_correspondence':continue
                    if spec.get('analysis_version') in {2,3}:
                        matches=(spec.get('person_entity_id')==selection['entity_id'] if selection['entity_id'] else
                            not spec.get('person_entity_id') and spec.get('person_addresses')==selection['addresses'])
                    else:
                        matches=not selection['entity_id'] and spec.get('person_address')==selection['address']
                    if matches:
                        final=json.loads(row['final_json']) if row['final_json'] else {}
                        items.append({**{k:row[k] for k in ('turn_id','status','question','created_at')},
                            'analysis_version':spec.get('analysis_version'), 'analysis_mode':spec.get('analysis_mode'),
                            'purpose':spec.get('purpose'),'source_report_turn_id':spec.get('source_report_turn_id'),
                            'target':spec.get('target'),'report_complete':bool(final.get('complete') and final.get('report',{}).get('complete'))})
                    if len(items)>=20:break
            return jsonify({'ok':True,'items':items})
        return _create_analysis(services,package_id,owner,request.get_json(silent=True) or {},_queue)

    @app.route('/api/mail/person-analyses/<turn_id>/resume',methods=['POST'])
    @_mail_endpoint
    def resume_person_analysis(turn_id):
        owner=_owner(services);turn=repo.get_turn(turn_id,owner,include_events=False)
        if not turn or turn['task_spec'].get('mode')!='person_correspondence':raise LookupError('联系人分析不存在')
        payload=request.get_json(silent=True) or {}
        if not isinstance(payload,dict):raise ValueError('请求内容必须是对象')
        if turn['task_spec'].get('analysis_version')==3:
            from .person_activity_api import resume_analysis
            return resume_analysis(services,turn,owner,payload,_queue)
        checkpoint=turn.get('final') or {}
        from .person_research import LEDGER_ENGINE
        if turn['task_spec'].get('analysis_version')==2 and checkpoint and turn['task_spec'].get('ledger_engine')!=LEDGER_ENGINE:
            raise ValueError('旧版人物分析检查点不兼容当前事项与状态引擎，请重新开始分析')
        exhausted=(turn['status']=='completed' and turn.get('sufficiency')=='partial' and checkpoint.get('budget_exhausted') is True
            and checkpoint.get('complete') is False and turn['task_spec'].get('analysis_version')==2)
        rebuild_projection=(payload.get('rebuild_projection') is True and turn['status']=='completed' and turn['task_spec'].get('analysis_version')==2
            and checkpoint.get('complete') is True and checkpoint.get('characters_read')==turn['task_spec']['corpus']['characters']
            and checkpoint.get('context_review',{}).get('verified_findings')==checkpoint.get('findings_count'))
        if turn['status'] not in {'failed','cancelled'} and not exhausted and not rebuild_projection:raise ValueError('仅可继续已失败、已停止、预算耗尽的阶段分析，或明确请求重建已读核查后的语义投影')
        budget=None
        token_exhausted=bool(checkpoint.get('token_budget_exhausted') or (checkpoint.get('token_usage') or {}).get('exhausted'))
        if exhausted:
            old_budget=checkpoint.get('model_call_budget')
            budget=payload.get('model_call_budget',old_budget if token_exhausted else None)
            minimum=old_budget if token_exhausted and checkpoint.get('model_calls',0)<(old_budget or 0) else (old_budget or 0)+1
            if isinstance(budget,bool) or not isinstance(budget,int) or not isinstance(old_budget,int) or not minimum<=budget<=PERSON_MAX_MODEL_CALL_BUDGET:
                raise ValueError(f'继续分析需要高于原上限且不超过{PERSON_MAX_MODEL_CALL_BUDGET}的整数调用上限')
        elif 'model_call_budget' in payload:
            raise ValueError('仅预算耗尽的阶段分析可修改调用上限')
        job=services.storage.get_job(turn['job_id'],owner) if turn.get('job_id') else None
        if job and job.get('status') not in {'failed','cancelled','completed'}:raise ValueError('上一任务尚未结束，请稍后继续')
        if not services.model:raise RuntimeError('模型暂不可用')
        spec=turn['task_spec'];pid=spec['package_id']
        from .research.token_budget import init_usage
        max_tokens=payload.get('max_tokens',spec.get('max_tokens',Config.MAIL_RESEARCH_MAX_TOKENS))
        used=checkpoint.get('token_usage') or {}
        if isinstance(max_tokens,bool) or not isinstance(max_tokens,int) or not max(1,used.get('total_tokens',0)+used.get('reserved_tokens',0)+1)<=max_tokens<=Config.MAIL_RESEARCH_TOKEN_LIMIT:
            raise ValueError(f'继续分析的 token 上限需要高于已使用与预留的合计，且不超过{Config.MAIL_RESEARCH_TOKEN_LIMIT}')
        if spec.get('analysis_version')==2:
            current=analysis_corpus_state(repo,pid,owner,selection_for_spec(repo,spec,owner))
        else:
            current=corpus_state(repo,pid,owner,spec['person_address'])
        if current['fingerprint']!=spec['corpus']['fingerprint']:raise ValueError('资料已变化，请重新开始分析')
        with repo._connection() as c:
            before=c.execute('SELECT status,task_spec_json,final_json,error_json,job_id FROM mail_research_turns WHERE turn_id=? AND owner_id=?',(turn_id,owner)).fetchone()
            if not before or before['status']!=turn['status'] or json.loads(before['task_spec_json'])!=spec or (json.loads(before['final_json']) if before['final_json'] else None)!=turn.get('final'):
                raise ValueError('分析检查点已变化，请刷新后继续')
            next_spec,next_final=before['task_spec_json'],before['final_json']
            changed_spec={**spec,'max_tokens':max_tokens}
            changed_final={**checkpoint,'token_budget_exhausted':False,'token_usage':init_usage({'max_tokens':max_tokens},used)} if checkpoint else None
            if rebuild_projection:
                changed_final.update(phase='synthesis',complete=False,synthesis={'batches_read':0,'themes':[]},
                                     semantic_workflow=None,semantic_unverified_findings=0)
            if exhausted:
                changed_spec['model_call_budget']=budget
                changed_final.update(model_call_budget=budget,budget_exhausted=False,complete=False)
            next_spec=json.dumps(changed_spec,ensure_ascii=False,sort_keys=True)
            next_final=json.dumps(changed_final,ensure_ascii=False,sort_keys=True) if changed_final else None
            # Detach the old terminal job during the claim. A concurrent GET
            # must not turn this newly queued continuation back into failed.
            claimed=c.execute("UPDATE mail_research_turns SET status='queued',task_spec_json=?,final_json=?,job_id=NULL,error_json=NULL,updated_at=CURRENT_TIMESTAMP WHERE turn_id=? AND owner_id=? AND status=? AND task_spec_json=? AND final_json IS ? AND job_id IS ?",(next_spec,next_final,turn_id,owner,before['status'],before['task_spec_json'],before['final_json'],before['job_id'])).rowcount
        if not claimed:raise ValueError('该分析已经恢复，请刷新查看进度')
        try:
            job_id,created=_queue(services,pid,'mail_research',owner,{'package_id':pid,'turn_id':turn_id,'workflow_source':'mail_person_resume'})
            if not created:raise RuntimeError('另一个邮件任务正在运行，请稍后继续')
            repo.update_turn(turn_id,owner,job_id=job_id)
        except Exception:
            with repo._connection() as c:
                c.execute("UPDATE mail_research_turns SET status=?,task_spec_json=?,final_json=?,job_id=?,error_json=?,updated_at=CURRENT_TIMESTAMP WHERE turn_id=? AND owner_id=? AND status='queued' AND task_spec_json=? AND final_json IS ? AND job_id IS NULL",(before['status'],before['task_spec_json'],before['final_json'],before['job_id'],before['error_json'],turn_id,owner,next_spec,next_final))
            raise
        return jsonify({'ok':True,'turn_id':turn_id,'job_id':job_id}),202


def _create_analysis(services,package_id,owner,payload,queue):
    if not isinstance(payload,dict):raise ValueError('请求内容必须是对象')
    # The new UI explicitly opts in; legacy callers and stored v2 jobs retain
    # their exact execution path without being migrated or cancelled.
    if 'analysis_mode' in payload or payload.get('analysis_version')==3:
        from .person_activity_api import create_analysis
        return create_analysis(services,package_id,owner,payload,queue)
    repo=services.repository
    package=authorize(repo,package_id,owner)
    if package.get('status') in {'inventory','importing','failed'}:raise ValueError('请等待邮件导入完成后再开始全量分析')
    selection=resolve_selection(repo,package_id,owner,payload.get('address'),payload.get('entity_id'))
    objective=str(payload.get('question') or '梳理这个邮箱发出的目标、行动、顾虑和变化，并区分收到的信息与本人表达。').strip()
    if not objective or len(objective)>1200:raise ValueError('研究问题请填写1至1200个字符')
    budget=payload.get('model_call_budget',PERSON_DEFAULT_MODEL_CALL_BUDGET)
    if isinstance(budget,bool) or not isinstance(budget,int) or not 1<=budget<=PERSON_MAX_MODEL_CALL_BUDGET:
        raise ValueError(f'分析调用上限需要1至{PERSON_MAX_MODEL_CALL_BUDGET}的整数')
    max_tokens=payload.get('max_tokens',Config.MAIL_RESEARCH_MAX_TOKENS)
    if isinstance(max_tokens,bool) or not isinstance(max_tokens,int) or not 1<=max_tokens<=Config.MAIL_RESEARCH_TOKEN_LIMIT:
        raise ValueError(f'token 上限需要1至{Config.MAIL_RESEARCH_TOKEN_LIMIT}的整数')
    if not services.model:raise RuntimeError('模型暂不可用；仍可浏览全部往来邮件')
    from .conversations import rebuild
    rebuild(repo,package_id,owner)
    corpus=analysis_corpus_state(repo,package_id,owner,selection)
    if not corpus['characters']:raise ValueError('该人物尚无可分析正文或附件文本，请先完成邮件导入')
    from .person_research import LEDGER_ENGINE,PERSON_EXECUTION_ENGINE
    from .issue_repository import ledger_dependency, workflow_scope
    spec={'mode':'person_correspondence','analysis_version':2,'package_id':package_id,'person_address':selection['address'],
        'person_addresses':selection['addresses'],'person_entity_id':selection['entity_id'],'model_call_budget':budget,'objective':objective,'corpus':corpus,
        'phase2_enabled':getattr(Config,'MAIL_PHASE2_ENABLED',True),'ledger_engine':LEDGER_ENGINE,
        'execution_engine':PERSON_EXECUTION_ENGINE,'max_tokens':max_tokens}
    spec['cache_key']=hashlib.sha256(json.dumps({k:v for k,v in spec.items() if k not in {'model_call_budget','max_tokens'}},
        sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    with repo._connection() as c:
        candidates=c.execute("SELECT t.turn_id,t.task_spec_json,t.final_json FROM mail_research_turns t "
            "JOIN mail_research_sessions s ON s.session_id=t.session_id WHERE s.package_id=? AND t.owner_id=? "
            "AND t.status='completed' AND t.final_json IS NOT NULL ORDER BY t.rowid DESC LIMIT 100",(package_id,owner)).fetchall()
    dependency=None
    for row in candidates:
        old=json.loads(row['task_spec_json']);final=json.loads(row['final_json'])
        if old.get('execution_engine')!=PERSON_EXECUTION_ENGINE or old.get('cache_key')!=spec['cache_key'] or not final.get('complete') or payload.get('force'):continue
        if spec['phase2_enabled']:
            if dependency is None:dependency=ledger_dependency(repo,package_id,owner,workflow_scope(spec,selection))
            if final.get('ledger_dependency')!=dependency:continue
        return jsonify({'ok':True,'turn_id':row['turn_id'],'cached':True,'corpus':corpus}),202
    session=repo.create_session(package_id,owner,'联系人意图分析 · '+selection['display_name'],{'person_address':selection['address'],'person_entity_id':selection['entity_id']})
    turn=repo.create_turn(session['session_id'],owner,objective,spec)
    try:
        job_id,created=queue(services,package_id,'mail_research',owner,{'package_id':package_id,'turn_id':turn['turn_id'],'workflow_source':'mail_person_analysis'})
        if not created:raise RuntimeError('已有邮件研究任务正在运行，请完成或取消后再试')
        repo.update_turn(turn['turn_id'],owner,job_id=job_id)
    except Exception:
        repo.update_turn(turn['turn_id'],owner,status='failed',sufficiency='insufficient',error={'message':'任务未入队，请稍后重试'})
        raise
    return jsonify({'ok':True,'turn_id':turn['turn_id'],'job_id':job_id,'corpus':corpus}),202


def register_network_routes(app, services):
    """Graph pages use the same authorized message scope as correspondence."""
    from .api import _mail_endpoint, _owner, _scope
    from . import network_service

    def selected():
        return {**_scope(), 'conversation_id': request.args.get('conversation_id'),
            'view': request.args.get('view') or 'mailbox', 'issue_id': request.args.get('issue_id'),
            'as_of': request.args.get('as_of'), 'manifest': request.args.get('manifest')}

    def page_args():
        return {'cursor': request.args.get('cursor'), 'limit': request.args.get('limit')}

    def issue_nodes():
        try:
            return json.loads(request.args.get('nodes','[]'))
        except (TypeError,ValueError) as exc:
            raise ValueError('图谱事项节点列表无效') from exc

    @app.route('/api/mail/packages/<package_id>/relations/network/overview')
    @_mail_endpoint
    def graph_overview(package_id):
        scope = selected()
        scope.pop('manifest')
        return jsonify({'ok': True, 'network': network_service.overview(services.repository, package_id,
            _owner(services), limit=request.args.get('limit',30), **scope)})

    @app.route('/api/mail/packages/<package_id>/relations/network/search')
    @_mail_endpoint
    def graph_search(package_id):
        return jsonify({'ok': True, 'network': network_service.search(services.repository, package_id,
            _owner(services), node_query=request.args.get('node_query',''), **page_args(), **selected())})

    @app.route('/api/mail/packages/<package_id>/relations/network/neighbors')
    @_mail_endpoint
    def graph_neighbors(package_id):
        return jsonify({'ok': True, 'network': network_service.neighbors(services.repository, package_id,
            _owner(services), node=request.args.get('node'), **page_args(), **selected())})

    @app.route('/api/mail/packages/<package_id>/relations/network/edge/messages')
    @_mail_endpoint
    def graph_messages(package_id):
        return jsonify({'ok': True, **network_service.edge_messages(services.repository, package_id,
            _owner(services), from_node=request.args.get('from_node'), to_node=request.args.get('to_node'),
            recipient_role=request.args.get('recipient_role'), **page_args(), **selected())})

    @app.route('/api/mail/packages/<package_id>/relations/network/issues')
    @_mail_endpoint
    def graph_issues(package_id):
        return jsonify({'ok': True, **network_service.issue_page(services.repository, package_id,
            _owner(services), nodes=issue_nodes(), **page_args(), **selected())})

    @app.route('/api/mail/packages/<package_id>/relations/network/issues/<timeline_issue_id>/timeline')
    @_mail_endpoint
    def graph_issue_timeline(package_id, timeline_issue_id):
        return jsonify({'ok': True, **network_service.issue_timeline(services.repository, package_id,
            _owner(services), timeline_issue_id=timeline_issue_id, nodes=issue_nodes(), **page_args(), **selected())})
