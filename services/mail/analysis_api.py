"""Owner-scoped analysis entry points, fixed scope, cache and resume."""
from __future__ import annotations
import json, hashlib
from config import Config
from web_compat import request, jsonify
from .analysis import manifest, analysis_page, VERSION, PROMPT_FINGERPRINT
from .people import resolve_person, bilateral
from .person_corpus import related_messages
from .research.runtime import MailResearchRuntime
from .issue_repository import issue_message_scope, ledger_dependency, workflow_scope
from .issue_state import timestamp

FILTERS=('date_from','date_to','q','has_attachment','participant','conversation_id','message_keys','roles','include_explicit_attachments','issue_id','as_of','manifest','view')

def make_spec(repo,pid,owner,payload):
    package=repo.get_package(pid,owner)
    if not package or package.get('status') in {'inventory','importing','failed'}:raise ValueError('请先完成邮件导入')
    if not isinstance(payload,dict):raise ValueError('分析请求必须是对象')
    kind=payload.get('kind','message')
    if kind not in {'message','pair','screen'}:raise ValueError('分析类型仅支持单封、双方或逐封筛查')
    raw_scope=payload.get('scope') or {}
    if not isinstance(raw_scope,dict):raise ValueError('分析范围无效')
    scope={k:raw_scope[k] for k in FILTERS if k in raw_scope}
    if not isinstance(scope.get('include_explicit_attachments',True),bool):raise ValueError('附件阅读范围需要布尔值')
    allowed=MailResearchRuntime(repo)._allowed_message_keys({'scope':scope,'analysis_version':3,'base_result_set_id':payload.get('base_result_set_id')},pid,owner)
    if allowed is None:
        with repo._connection() as c:allowed={r[0] for r in c.execute('SELECT DISTINCT message_key FROM mail_visible_occurrences WHERE package_id=?',(pid,))}
    if scope.get('manifest'):
        from .network_service import _index
        graph=_index(repo,pid,owner,view=scope.get('view','mailbox'),issue_id=scope.get('issue_id'),
                     as_of=scope.get('as_of'),manifest=scope['manifest'],**{k:v for k,v in scope.items() if k in FILTERS[:6]})
        allowed.intersection_update(graph['messages'])
        scope['message_keys']=sorted(allowed)
    if scope.get('as_of'):
        if timestamp(scope['as_of']) is None:raise ValueError('截止时间需要带时区的ISO时间')
        with repo._connection() as c:
            allowed.intersection_update(r[0] for r in c.execute('SELECT message_key FROM mail_messages WHERE julianday(sent_at_utc)<=julianday(?)',(scope['as_of'],)))
    if scope.get('issue_id'):
        allowed.intersection_update(issue_message_scope(repo,pid,owner,scope['issue_id'],as_of=scope.get('as_of'),scope={'message_keys':list(allowed),
            'include_explicit_attachments':scope.get('include_explicit_attachments',True)})['message_keys'])
    bindings=[]
    if kind=='message':
        key=str(payload.get('message_key') or '')
        if key not in allowed or not repo.get_message(key,owner,pid):raise ValueError('这封邮件不在当前分析范围')
        roots=[key]
        with repo._connection() as c:parties=sorted({r[0] for r in c.execute('SELECT a.normalized_address FROM mail_message_participants p JOIN mail_addresses a ON a.address_id=p.address_id WHERE p.message_key=?',(key,))})
        title='单封通信意图'
    elif kind=='pair':
        person=resolve_person(repo,pid,owner,payload.get('address'),payload.get('entity_id'))
        other=resolve_person(repo,pid,owner,payload.get('counterpart'),payload.get('counterpart_entity_id'))
        bindings=[{'entity_id':p['entity_id'],'addresses':p['addresses']} for p in (person,other) if p.get('entity_id')]
        filters={k:v for k,v in scope.items() if k in FILTERS[:6]}
        roots=[];offset=0
        while True:
            page=bilateral(repo,pid,owner,person['address'],other['address'],entity_id=person.get('entity_id'),counterpart_entity_id=other.get('entity_id'),limit=100,offset=offset,**filters)
            roots.extend(i['message_key'] for i in page['items'] if i['message_key'] in allowed)
            offset+=len(page['items'])
            if not page['items'] or offset>=page['total']:break
        parties=sorted(set(person['addresses']+other['addresses']));title='双方通信意图'
    else:
        roots=sorted(allowed);parties=['screen@local.invalid'];title='按目标逐封筛查'
    if not roots:raise ValueError('当前范围没有可分析邮件')
    context=sorted((set(related_messages(repo,pid,owner,roots))|set(roots))&allowed) if kind!='screen' else roots
    objective=str(payload.get('question') or ('分别梳理参与方的目标、通信动作、条件、分歧、变化和下一步要求，说明每项原文依据。' if kind!='screen' else '')).strip()
    if not 1<=len(objective)<=1200:raise ValueError('研究目标需要1至1200个字符')
    budget=payload.get('model_call_budget',Config.MAIL_ANALYSIS_MAX_MODEL_CALLS)
    if isinstance(budget,bool) or not isinstance(budget,int) or not 1<=budget<=Config.MAIL_RESEARCH_MODEL_CALL_LIMIT:raise ValueError(f'分析调用上限需要1至{Config.MAIL_RESEARCH_MODEL_CALL_LIMIT}的整数')
    snapshot=manifest(repo,pid,owner,context,include_attachments=scope.get('include_explicit_attachments',True))
    spec={'mode':{'message':'message_dossier','pair':'pair_dossier','screen':'intent_screen'}[kind],
        'analysis_version':3,'engine_version':VERSION,'package_id':pid,'source_revision':package['source_revision'],'index_epoch':package['index_epoch'],
        'message_keys':roots,'context_keys':context,'parties':parties,'objective':objective,'scope':scope,'manifest':snapshot,
        'model_call_budget':budget,'base_result_set_id':payload.get('base_result_set_id'),'entity_bindings':bindings}
    spec['phase2_enabled']=bool(getattr(Config,'MAIL_PHASE2_ENABLED',True))
    max_tokens=payload.get('max_tokens',Config.MAIL_RESEARCH_MAX_TOKENS)
    if isinstance(max_tokens,bool) or not isinstance(max_tokens,int) or not 1<=max_tokens<=Config.MAIL_RESEARCH_TOKEN_LIMIT:
        raise ValueError(f'分析token预算需要1至{Config.MAIL_RESEARCH_TOKEN_LIMIT}的整数')
    spec['max_tokens']=max_tokens
    spec['prompt_fingerprint']=PROMPT_FINGERPRINT
    spec['cache_key']=hashlib.sha256(json.dumps({k:spec[k] for k in ('mode','engine_version','prompt_fingerprint','message_keys','context_keys','parties','objective','scope','manifest','entity_bindings','phase2_enabled')},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return spec,title

def create_analysis(services,pid,owner,payload):
    from .api import _queue
    spec,title=make_spec(services.repository,pid,owner,payload)
    if not services.model:raise RuntimeError('模型暂不可用，仍可浏览邮件原文')
    with services.repository._connection() as c:
        rows=c.execute("SELECT turn_id,task_spec_json,final_json FROM mail_research_turns WHERE owner_id=? AND status='completed' AND final_json IS NOT NULL ORDER BY created_at DESC LIMIT 100",(owner,)).fetchall()
    for row in rows:
        old=json.loads(row['task_spec_json']);final=json.loads(row['final_json'])
        dependency_ok=spec['mode']=='intent_screen' or not spec['phase2_enabled'] or final.get('ledger_dependency')==ledger_dependency(services.repository,pid,owner,workflow_scope(spec))
        if old.get('cache_key')==spec['cache_key'] and final.get('complete') and dependency_ok and not final.get('ledger_publication_failures') and not payload.get('force'):
            return {'ok':True,'turn_id':row['turn_id'],'cached':True,'scope':spec['scope']}
    session=services.repository.create_session(pid,owner,title,spec['scope'])
    turn=services.repository.create_turn(session['session_id'],owner,spec['objective'],spec)
    try:
        job,created=_queue(services,pid,'mail_research',owner,{'package_id':pid,'turn_id':turn['turn_id'],'workflow_source':'mail_intent_analysis'})
        if not created:raise RuntimeError('当前资料包有邮件研究正在运行，请完成或停止后再提交')
        services.repository.update_turn(turn['turn_id'],owner,job_id=job)
    except Exception as exc:
        services.repository.update_turn(turn['turn_id'],owner,status='failed',error={'message':str(exc)[:300]});raise
    return {'ok':True,'turn_id':turn['turn_id'],'job_id':job,'scope':spec['scope'],'corpus':spec['manifest'],'cached':False}

def register_analysis_routes(app,services):
    from .api import _mail_endpoint,_owner,_queue,_limit,_offset
    @app.route('/api/mail/packages/<package_id>/intent-analyses',methods=['POST'])
    @_mail_endpoint
    def mail_create_intent_analysis(package_id):
        return jsonify(create_analysis(services,package_id,_owner(services),request.get_json(silent=True) or {})),202

    @app.route('/api/mail/intent-analyses/<turn_id>')
    @_mail_endpoint
    def mail_intent_analysis(turn_id):
        return jsonify({'ok':True,**analysis_page(services.repository,turn_id,_owner(services),_offset(request.args.get('offset')),_limit(request.args.get('limit'),10,50))})

    @app.route('/api/mail/intent-analyses/<turn_id>/resume',methods=['POST'])
    @_mail_endpoint
    def mail_resume_intent_analysis(turn_id):
        owner=_owner(services);repo=services.repository;turn=repo.get_turn(turn_id,owner)
        if not turn or turn['task_spec'].get('mode') not in {'message_dossier','pair_dossier','intent_screen'}:raise LookupError('意图分析不存在')
        if turn['status'] not in {'failed','cancelled','completed'}:raise ValueError('任务正在运行')
        original_spec=turn['task_spec'];spec={**original_spec};cp=turn['final']
        if spec.get('engine_version')!=VERSION or spec.get('phase2_enabled',True)!=bool(getattr(Config,'MAIL_PHASE2_ENABLED',True)):
            raise ValueError('分析引擎或功能配置已变化，请重新发起分析')
        if cp and cp.get('complete'):raise ValueError('此分析已完成，可重新提交新的分析')
        if manifest(repo,spec['package_id'],owner,spec['context_keys'],include_attachments=spec.get('scope',{}).get('include_explicit_attachments',True))['fingerprint']!=spec['manifest']['fingerprint']:raise ValueError('资料已变化，请重新分析')
        payload=request.get_json(silent=True) or {}
        if not isinstance(payload,dict):raise ValueError('恢复请求必须是对象')
        budget=payload.get('model_call_budget',spec['model_call_budget'])
        if isinstance(budget,bool) or not isinstance(budget,int) or not max(1,(cp or {}).get('model_calls',0)+1)<=budget<=Config.MAIL_RESEARCH_MODEL_CALL_LIMIT:raise ValueError(f'继续调用上限应高于已使用次数且不超过{Config.MAIL_RESEARCH_MODEL_CALL_LIMIT}')
        spec['model_call_budget']=budget
        max_tokens=payload.get('max_tokens',spec.get('max_tokens',Config.MAIL_RESEARCH_MAX_TOKENS))
        used=(cp or {}).get('token_usage',{})
        if isinstance(max_tokens,bool) or not isinstance(max_tokens,int) or not max(1,used.get('total_tokens',0)+used.get('reserved_tokens',0)+1)<=max_tokens<=Config.MAIL_RESEARCH_TOKEN_LIMIT:
            raise ValueError(f'继续token预算应高于已使用及预留token且不超过{Config.MAIL_RESEARCH_TOKEN_LIMIT}')
        spec['max_tokens']=max_tokens
        from .research.token_budget import init_usage
        restored={**cp,'budget_exhausted':False,'token_budget_exhausted':False,'complete':False,
                  'token_usage':init_usage({'max_tokens':max_tokens},cp.get('token_usage'))} if cp else None
        spec_json=json.dumps(spec,ensure_ascii=False,sort_keys=True)
        restored_json=json.dumps(restored,ensure_ascii=False,sort_keys=True) if restored else None
        with repo._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            latest=c.execute('SELECT status,task_spec_json,final_json FROM mail_research_turns WHERE turn_id=? AND owner_id=?',(turn_id,owner)).fetchone()
            if not latest or latest['status']!=turn['status'] or json.loads(latest['task_spec_json'])!=original_spec or (json.loads(latest['final_json']) if latest['final_json'] else None)!=cp:
                raise ValueError('分析检查点已变化，请刷新后再恢复')
            c.execute('UPDATE mail_research_turns SET task_spec_json=?,final_json=?,status=\'queued\',error_json=NULL WHERE turn_id=? AND owner_id=?',(spec_json,restored_json,turn_id,owner))
        try:
            job,created=_queue(services,spec['package_id'],'mail_research',owner,{'package_id':spec['package_id'],'turn_id':turn_id,'workflow_source':'mail_intent_resume'})
            if not created:raise RuntimeError('当前资料包已有研究任务，请稍后恢复')
            repo.update_turn(turn_id,owner,job_id=job)
        except Exception as exc:
            with repo._connection() as c:
                c.execute('BEGIN IMMEDIATE')
                c.execute('UPDATE mail_research_turns SET task_spec_json=?,final_json=?,status=?,error_json=?,sufficiency=?,job_id=?,updated_at=CURRENT_TIMESTAMP '
                    'WHERE turn_id=? AND owner_id=? AND status=\'queued\' AND task_spec_json=? AND final_json IS ?',
                    (json.dumps(original_spec,ensure_ascii=False,sort_keys=True),json.dumps(cp,ensure_ascii=False,sort_keys=True) if cp else None,
                     turn['status'],json.dumps(turn['error'],ensure_ascii=False) if turn.get('error') else None,turn.get('sufficiency','partial'),turn.get('job_id'),
                     turn_id,owner,spec_json,restored_json))
            raise
        return jsonify({'ok':True,'turn_id':turn_id,'job_id':job}),202
