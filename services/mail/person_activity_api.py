"""Owner-bound creation/resumption for the independent two-step person reports."""
import hashlib
import json

from config import Config
from web_compat import jsonify
from .people import authorize
from .person_corpus import (resolve_selection,selection_for_spec,
    PERSON_DEFAULT_MODEL_CALL_BUDGET,PERSON_MAX_MODEL_CALL_BUDGET)
from .person_activity import (VERSION,ENGINE,DEEP_WORKFLOW,FACT_OBJECTIVE,full_corpus,
    validate_source_report,source_report_basis,validate_deep_basis,_fact_output_failure)
from .research.token_budget import init_usage


def _budget(payload, old=None):
    value=payload.get('model_call_budget',old if old is not None else PERSON_DEFAULT_MODEL_CALL_BUDGET)
    if isinstance(value,bool) or not isinstance(value,int) or not 1<=value<=PERSON_MAX_MODEL_CALL_BUDGET:
        raise ValueError('模型调用上限必须是1至%s的整数'%PERSON_MAX_MODEL_CALL_BUDGET)
    return value


def _tokens(payload, old=None):
    value=payload.get('max_tokens',old if old is not None else Config.MAIL_RESEARCH_MAX_TOKENS)
    if isinstance(value,bool) or not isinstance(value,int) or not 1<=value<=Config.MAIL_RESEARCH_TOKEN_LIMIT:
        raise ValueError('token上限必须是1至%s的整数'%Config.MAIL_RESEARCH_TOKEN_LIMIT)
    return value


def create_analysis(services,pid,owner,payload,queue):
    repo=services.repository;package=authorize(repo,pid,owner)
    if package.get('status') in {'inventory','importing','failed'}:
        raise ValueError('请等待资料包导入成功后再开始全量扫描')
    selection=resolve_selection(repo,pid,owner,payload.get('address'),payload.get('entity_id'))
    mode=payload.get('analysis_mode','full_report')
    if not isinstance(mode,str) or mode not in {'full_report','deep_analysis'}:
        raise ValueError('请选择事实报告或目标深度分析')
    purpose='activity_report' if mode=='full_report' else 'deep_analysis'
    if mode=='full_report':
        # A user's mail-list filters never narrow the first-step package scan.
        objective=FACT_OBJECTIVE;target=None;source=None
    else:
        target=payload.get('target');objective=payload.get('question');source=payload.get('source_report_turn_id')
        if not isinstance(target,str) or not target.strip() or len(target)>600:
            raise ValueError('请填写1至600字符的具体事情、对象或东西')
        if not isinstance(objective,str) or not objective.strip() or len(objective)>1200:
            raise ValueError('请填写1至1200字符的态度、想法或意图问题')
        if not isinstance(source,str) or not source.strip():raise ValueError('请先生成完整事实报告')
        target=target.strip();objective=objective.strip();source=source.strip()
    if not services.model:raise RuntimeError('模型暂不可用，尚未创建报告任务')
    if mode=='full_report':
        from .conversations import rebuild
        rebuild(repo,pid,owner)
    corpus=full_corpus(repo,pid,owner,selection)
    if not corpus['characters']:raise ValueError('资料包没有可读取正文或附件文本')
    spec={'mode':'person_correspondence','analysis_version':VERSION,'execution_engine':ENGINE,
        'analysis_mode':mode,'purpose':purpose,'package_id':pid,'person_address':selection['address'],
        'person_addresses':selection['addresses'],'person_entity_id':selection['entity_id'],
        'objective':objective,'target':target,'source_report_turn_id':source,'corpus':corpus,
        'model_call_budget':_budget(payload),'max_tokens':_tokens(payload)}
    if mode=='deep_analysis':
        parent=validate_source_report(repo,source,owner,spec,check_content=False)
        spec.update(workflow_revision=DEEP_WORKFLOW,source_report_evidence_hash=source_report_basis(repo,parent)[1])
    spec['cache_key']=hashlib.sha256(json.dumps({k:v for k,v in spec.items() if k not in {'model_call_budget','max_tokens'}},
        ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if not payload.get('force'):
        with repo._connection() as c:
            rows=c.execute("SELECT t.turn_id,t.task_spec_json,t.final_json FROM mail_research_turns t JOIN mail_research_sessions s "
                "ON s.session_id=t.session_id WHERE t.owner_id=? AND s.package_id=? AND t.status='completed' ORDER BY t.rowid DESC",(owner,pid))
            for row in rows:
                old=json.loads(row['task_spec_json']);final=json.loads(row['final_json']) if row['final_json'] else {}
                covered=(final.get('source_scan_complete') is True and
                    final.get('coverage_source')=='completed_activity_report' and
                    final.get('source_report_evidence_hash')==spec.get('source_report_evidence_hash') and
                    final.get('source_coverage',{}).get('characters_read')==corpus['characters']) if mode=='deep_analysis' else (
                    final.get('scan_complete') is True and final.get('characters_read')==corpus['characters'])
                if (old.get('cache_key')==spec['cache_key'] and old.get('execution_engine')==ENGINE and final.get('complete') is True and
                    (mode!='deep_analysis' or old.get('workflow_revision')==DEEP_WORKFLOW) and
                    covered and final.get('report',{}).get('complete') is True):
                    return jsonify({'ok':True,'turn_id':row['turn_id'],'cached':True,'corpus':corpus,'analysis_mode':mode,'analysis_version':VERSION}),202
    session=repo.create_session(pid,owner,('人物事实报告 · ' if mode=='full_report' else '人物深度分析 · ')+selection['display_name'],
        {'person_address':selection['address'],'person_entity_id':selection['entity_id'],'purpose':purpose})
    turn=repo.create_turn(session['session_id'],owner,objective,spec);tid=turn['turn_id']
    try:
        job_id,created=queue(services,pid,'mail_research',owner,{'package_id':pid,'turn_id':tid,'workflow_source':'person_report_v3'})
        if not created:raise RuntimeError('已有邮件任务正在运行，请等待任务完成后再试')
        repo.update_turn(tid,owner,job_id=job_id)
    except Exception:
        repo.update_turn(tid,owner,status='failed',sufficiency='insufficient',error={'message':'报告任务未入队，请稍后继续'})
        raise
    return jsonify({'ok':True,'turn_id':tid,'job_id':job_id,'corpus':corpus,'analysis_version':VERSION,'analysis_mode':mode}),202


def resume_analysis(services,turn,owner,payload,queue):
    repo=services.repository;spec=turn['task_spec'];checkpoint=turn.get('final') or {};tid=turn['turn_id'];pid=spec['package_id']
    if spec.get('execution_engine')!=ENGINE:raise ValueError('旧检查点与当前报告引擎不兼容，请重新开始')
    exhausted=(turn['status']=='completed' and checkpoint.get('budget_exhausted') is True and checkpoint.get('complete') is False)
    if turn['status'] not in {'failed','cancelled'} and not exhausted:
        raise ValueError('只能继续失败、停止或预算耗尽的未完成报告')
    job=services.storage.get_job(turn['job_id'],owner) if turn.get('job_id') else None
    if job and job.get('status') not in {'failed','cancelled','completed'}:raise ValueError('上一任务尚未结束，请稍后继续')
    if not services.model:raise RuntimeError('模型暂不可用')
    selection=selection_for_spec(repo,spec,owner)
    if full_corpus(repo,pid,owner,selection)['fingerprint']!=spec['corpus']['fingerprint']:
        raise ValueError('资料包或人物身份已变化，请重新开始')
    if spec['purpose']=='deep_analysis':validate_deep_basis(repo,spec,owner,turn.get('final'))
    budget=_budget(payload,spec['model_call_budget']);max_tokens=_tokens(payload,spec['max_tokens'])
    used=checkpoint.get('token_usage') or {}
    if budget<=checkpoint.get('model_calls',0):raise ValueError('继续分析的调用上限需要高于已使用次数')
    if max_tokens<=used.get('total_tokens',0)+used.get('reserved_tokens',0):
        raise ValueError('继续分析的token上限需要高于已使用和预留的合计')
    changed_spec={**spec,'model_call_budget':budget,'max_tokens':max_tokens}
    changed_final={**checkpoint,'model_call_budget':budget,'budget_exhausted':False,'token_budget_exhausted':False,
                   'token_usage':init_usage({'max_tokens':max_tokens},used)} if checkpoint else None
    next_spec=json.dumps(changed_spec,ensure_ascii=False,sort_keys=True)
    old_error=(turn.get('error') or {}).get('message','')
    if (changed_final and spec['purpose']=='activity_report' and checkpoint.get('phase')=='reading' and
            not checkpoint.get('adaptive_batch') and _fact_output_failure(old_error)):
        changed_final['_adaptive_resume_output_error']=old_error
    next_final=json.dumps(changed_final,ensure_ascii=False,sort_keys=True) if changed_final else None
    with repo._connection() as c:
        before=c.execute('SELECT status,task_spec_json,final_json,error_json,job_id FROM mail_research_turns WHERE turn_id=? AND owner_id=?',(tid,owner)).fetchone()
        if (not before or before['status']!=turn['status'] or json.loads(before['task_spec_json'])!=spec or
            (json.loads(before['final_json']) if before['final_json'] else None)!=turn.get('final')):
            raise ValueError('人物检查点已变化，请刷新后继续')
        claimed=c.execute("UPDATE mail_research_turns SET status='queued',task_spec_json=?,final_json=?,job_id=NULL,error_json=NULL,updated_at=CURRENT_TIMESTAMP "
            'WHERE turn_id=? AND owner_id=? AND status=? AND task_spec_json=? AND final_json IS ? AND job_id IS ?',
            (next_spec,next_final,tid,owner,before['status'],before['task_spec_json'],before['final_json'],before['job_id'])).rowcount
    if not claimed:raise ValueError('报告已经恢复，请刷新查看')
    try:
        job_id,created=queue(services,pid,'mail_research',owner,{'package_id':pid,'turn_id':tid,'workflow_source':'person_report_v3_resume'})
        if not created:raise RuntimeError('另一个邮件任务仍在运行，请稍后继续')
        repo.update_turn(tid,owner,job_id=job_id)
    except Exception:
        with repo._connection() as c:
            c.execute("UPDATE mail_research_turns SET status=?,task_spec_json=?,final_json=?,error_json=?,job_id=?,updated_at=CURRENT_TIMESTAMP "
                "WHERE turn_id=? AND owner_id=? AND status='queued' AND task_spec_json=? AND final_json IS ? AND job_id IS NULL",
                (before['status'],before['task_spec_json'],before['final_json'],before['error_json'],before['job_id'],tid,owner,next_spec,next_final))
        raise
    return jsonify({'ok':True,'turn_id':tid,'job_id':job_id}),202
