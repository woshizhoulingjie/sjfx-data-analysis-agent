"""Additive owner-scoped correspondence endpoints; no translation hooks."""
import json
from web_compat import jsonify, request
from .people import authorize, contacts, correspondence, relationships, corpus_state
from .person_research import analysis_page


def register_people_routes(app, services):
    from .api import _mail_endpoint, _owner, _limit, _offset, _queue
    repo=services.repository

    @app.route('/api/mail/packages/<package_id>/people')
    @_mail_endpoint
    def mail_people(package_id):
        return jsonify({'ok':True,**contacts(repo,package_id,_owner(services),request.args.get('q',''),_offset(request.args.get('offset')),_limit(request.args.get('limit'),50,100))})

    @app.route('/api/mail/packages/<package_id>/person/messages')
    @_mail_endpoint
    def mail_person_messages(package_id):
        return jsonify({'ok':True,**correspondence(repo,package_id,_owner(services),request.args.get('address'),
            direction=request.args.get('direction','all'),offset=_offset(request.args.get('offset')),limit=_limit(request.args.get('limit'),25,100))})

    @app.route('/api/mail/packages/<package_id>/person/relationships')
    @_mail_endpoint
    def mail_person_relationships(package_id):
        return jsonify({'ok':True,**relationships(repo,package_id,_owner(services),request.args.get('address'),
            offset=_offset(request.args.get('offset')),limit=_limit(request.args.get('limit'),20,50))})

    @app.route('/api/mail/person-analyses/<turn_id>')
    @_mail_endpoint
    def mail_person_analysis(turn_id):
        owner=_owner(services)
        turn=repo.get_turn(turn_id,owner)
        if turn and turn.get('job_id') and turn['status'] not in {'completed','failed','cancelled'}:
            job=services.storage.get_job(turn['job_id'],owner)
            if job and job.get('status') in {'failed','cancelled'}:
                repo.update_turn(turn_id,owner,status=job['status'],sufficiency='insufficient',error={'message':'后台任务已停止，已完成的批次保留'})
        return jsonify({'ok':True,**analysis_page(repo,turn_id,owner,_offset(request.args.get('offset')),_limit(request.args.get('limit'),10,20))})

    @app.route('/api/mail/packages/<package_id>/person/analyses',methods=['GET','POST'])
    @_mail_endpoint
    def mail_person_analyses(package_id):
        owner=_owner(services);authorize(repo,package_id,owner)
        if request.method=='GET':
            address=authorize(repo,package_id,owner,request.args.get('address'))
            with repo._connection() as c:
                rows=c.execute('SELECT t.turn_id,t.status,t.question,t.created_at,t.task_spec_json FROM mail_research_turns t JOIN mail_research_sessions s ON s.session_id=t.session_id WHERE s.package_id=? AND t.owner_id=? ORDER BY t.rowid DESC',(package_id,owner))
                items=[]
                for row in rows:
                    spec=json.loads(row['task_spec_json'])
                    if spec.get('mode')=='person_correspondence' and spec.get('person_address')==address:
                        items.append({k:row[k] for k in ('turn_id','status','question','created_at')})
                    if len(items)>=20:break
            return jsonify({'ok':True,'items':items})
        return _create_analysis(services,package_id,owner,request.get_json(silent=True) or {},_queue)

    @app.route('/api/mail/person-analyses/<turn_id>/resume',methods=['POST'])
    @_mail_endpoint
    def resume_person_analysis(turn_id):
        owner=_owner(services);turn=repo.get_turn(turn_id,owner)
        if not turn or turn['task_spec'].get('mode')!='person_correspondence':raise LookupError('联系人分析不存在')
        if turn['status'] not in {'failed','cancelled'}:raise ValueError('仅可继续已失败或已停止的分析')
        job=services.storage.get_job(turn['job_id'],owner) if turn.get('job_id') else None
        if job and job.get('status') not in {'failed','cancelled','completed'}:raise ValueError('上一任务尚未结束，请稍后继续')
        if not services.model:raise RuntimeError('模型暂不可用')
        spec=turn['task_spec'];pid=spec['package_id']
        if corpus_state(repo,pid,owner,spec['person_address'])['fingerprint']!=spec['corpus']['fingerprint']:raise ValueError('资料已变化，请重新开始分析')
        with repo._connection() as c:
            claimed=c.execute("UPDATE mail_research_turns SET status='queued',error_json=NULL WHERE turn_id=? AND owner_id=? AND status IN ('failed','cancelled')",(turn_id,owner)).rowcount
        if not claimed:raise ValueError('该分析已经恢复，请刷新查看进度')
        try:
            job_id,created=_queue(services,pid,'mail_research',owner,{'package_id':pid,'turn_id':turn_id,'workflow_source':'mail_person_resume'})
            if not created:raise RuntimeError('另一个邮件任务正在运行，请稍后继续')
            repo.update_turn(turn_id,owner,job_id=job_id)
        except Exception:
            with repo._connection() as c:
                c.execute("UPDATE mail_research_turns SET status=? WHERE turn_id=? AND owner_id=? AND status='queued'",(turn['status'],turn_id,owner))
            raise
        return jsonify({'ok':True,'turn_id':turn_id,'job_id':job_id}),202


def _create_analysis(services,package_id,owner,payload,queue):
    if not isinstance(payload,dict):raise ValueError('请求内容必须是对象')
    repo=services.repository
    package=authorize(repo,package_id,owner)
    if package.get('status') in {'inventory','importing','failed'}:raise ValueError('请等待邮件导入完成后再开始全量分析')
    address=authorize(repo,package_id,owner,payload.get('address'))
    objective=str(payload.get('question') or '梳理这个邮箱发出的目标、行动、顾虑和变化，并区分收到的信息与本人表达。').strip()
    if not objective or len(objective)>1200:raise ValueError('研究问题请填写1至1200个字符')
    if not services.model:raise RuntimeError('模型暂不可用；仍可浏览全部往来邮件')
    corpus=corpus_state(repo,package_id,owner,address)
    if not corpus['characters']:raise ValueError('该邮箱尚无可分析正文，请先完成邮件导入')
    session=repo.create_session(package_id,owner,'联系人意图分析 · '+address,{'person_address':address})
    spec={'mode':'person_correspondence','package_id':package_id,'person_address':address,'objective':objective,'corpus':corpus}
    turn=repo.create_turn(session['session_id'],owner,objective,spec)
    try:
        job_id,created=queue(services,package_id,'mail_research',owner,{'package_id':package_id,'turn_id':turn['turn_id'],'workflow_source':'mail_person_analysis'})
        if not created:raise RuntimeError('已有邮件研究任务正在运行，请完成或取消后再试')
        repo.update_turn(turn['turn_id'],owner,job_id=job_id)
    except Exception:
        repo.update_turn(turn['turn_id'],owner,status='failed',sufficiency='insufficient',error={'message':'任务未入队，请稍后重试'})
        raise
    return jsonify({'ok':True,'turn_id':turn['turn_id'],'job_id':job_id,'corpus':corpus}),202
