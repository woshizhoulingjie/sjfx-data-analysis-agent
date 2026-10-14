"""Explicit persisted protection contexts for every isolated large-package lane."""
from contextlib import contextmanager
from functools import wraps
import inspect
import json
import contextvars
from pathlib import Path
_active_packages=contextvars.ContextVar("large_verified_packages",default=())
from sjfx_protection.store import ProtectionError

@contextmanager
def large_package_execution(store,package_id,operation='analyze'):
    from sjfx_protection.business import enabled,saved_execution
    if not enabled():
        yield
        return
    item=store.admit_protection(package_id,wait=False)
    if not item:raise ProtectionError('数据包不存在',404,'package_not_found')
    try:
        with saved_execution(item['owner_id'],[item['root_path']],item.get('protection_context'),operation=operation):
            active=_active_packages.get()
            if str(package_id) not in active:
                _validate_package_cache(store,item)
            token=_active_packages.set(active+(str(package_id),))
            try:yield
            finally:_active_packages.reset(token)
    except ProtectionError as exc:
        if str(package_id) not in _active_packages.get():
            if exc.code!='inspection_pending':store.update_package(package_id,status='paused',message='资料保护暂停：'+str(exc))
        raise

def package_lane(function=None,*,operation='analyze'):
    """Resolve a persisted package in this thread, never inherit an HTTP decision."""
    def decorate(func):
        signature=inspect.signature(func)
        @wraps(func)
        def guarded(*args,**kwargs):
            from sjfx_protection.business import enabled
            if not enabled():return func(*args,**kwargs)
            bound=signature.bind(*args,**kwargs);values=bound.arguments
            owner=values.get('self') or values.get('worker')
            store=values.get('store') or values.get('large_store') or getattr(owner,'store',None) or (owner if hasattr(owner,'get') else None)
            package_id=values.get('package_id')
            for key in ('package','item'):
                value=values.get(key)
                if not package_id and isinstance(value,dict):package_id=value.get('id') or value.get('package_id')
            if not store or not package_id:raise ProtectionError('后台车道缺少明确包来源',423,'protection_context_required')
            with large_package_execution(store,package_id,operation):return func(*args,**kwargs)
        return guarded
    return decorate(function) if function is not None else decorate

def recheck_active(operation='analyze'):
    from sjfx_protection.business import enabled,context,protection,select_context
    if not enabled():return
    value=context() or {};p=protection()
    contexts=value.get('package_contexts') or [value]
    if not value.get('owner') or not contexts:raise ProtectionError('模型车道缺少显式保护上下文',423,'protection_context_required')
    for frozen in contexts:
        if not frozen.get('registration_id'):raise ProtectionError('模型车道缺少登记身份',423,'protection_context_required')
        p.package.require_operation(value['owner'],registration_id=frozen['registration_id'],operation=operation,context=frozen,
            device_id=frozen.get('device_id',''),approval_id=frozen.get('approval_id'),record=False)

def current_provenance():
    from sjfx_protection.business import enabled,context
    if not enabled():return {}
    recheck_active();return dict(context() or {})

def validate_cached_rows(store,package_id,rows):
    from sjfx_protection.business import enabled,protection
    if not enabled():return
    item=store.get(package_id)
    with large_package_execution(store,package_id,operation='read'):
        for row in rows:
            if not (row.get('local_summary') or row.get('deep_summary')):continue
            frozen=row.get('protection_context')
            if not isinstance(frozen,dict) or not frozen.get('registration_id'):
                raise ProtectionError('历史大包缓存缺少源版本，需重新解析',423,'cached_version_unverified')
            protection().package.require_operation(item['owner_id'],registration_id=frozen['registration_id'],context=frozen,operation='read',
                device_id=frozen.get('device_id',''),record=False)

def stamp_fields(fields):
    if not ('local_summary' in fields or 'deep_summary' in fields):return fields
    from sjfx_protection.business import enabled
    if enabled():return {**fields,'protection_context':current_provenance()}
    return fields


def _validate_package_cache(store,item):
    from contextlib import closing
    from sjfx_protection.business import protection
    with closing(store._connect()) as db:
        rows=db.execute("SELECT DISTINCT protection_context FROM files WHERE package_id=? AND (local_summary NOT IN ('{}','null','') OR deep_summary NOT IN ('{}','null',''))",(str(item['id']),)).fetchall()
    for row in rows:
        try:frozen=json.loads(row[0])
        except (TypeError,ValueError):frozen={}
        if not isinstance(frozen,dict) or not frozen.get('registration_id'):
            raise ProtectionError('历史大包缓存缺少源版本，需重新解析',423,'cached_version_unverified')
        protection().package.require_operation(item['owner_id'],registration_id=frozen['registration_id'],context=frozen,
            operation='read',device_id=frozen.get('device_id',''),approval_id=frozen.get('approval_id'),record=False)


def guarded_model_call(client,method,*args,**kwargs):
    recheck_active()
    try:
        result=getattr(client,method)(*args,**kwargs)
    except BaseException:
        # Revoke before a caller considers its fallback or model retry.
        recheck_active()
        raise
    recheck_active()
    return result


def record_report_artifact(path):
    from sjfx_protection.business import enabled,protection
    if not enabled():return Path(path)
    ctx=current_provenance();p=protection()
    contexts=ctx.get('package_contexts') or [ctx]
    sources=[{'registration_id':value['registration_id'],'versions':value['versions'],'context':value} for value in contexts]
    result=p.package.record_artifact(ctx['owner'],Path(path),sources)
    with p.store.db(True) as db:
        db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('large-artifact:'+ctx['owner']+':'+str(Path(path).resolve()),result['id']))
    return Path(require_report_artifact(path))


def require_report_artifact(path,operation='read'):
    from sjfx_protection.business import enabled,protection,context
    if not enabled():return Path(path)
    ctx=context() or {};p=protection()
    with p.store.db() as db:
        row=db.execute('SELECT value FROM settings WHERE key=?',('large-artifact:'+ctx['owner']+':'+str(Path(path).resolve()),)).fetchone()
    if not row:raise ProtectionError('历史报告缺少受保护来源，需重新生成',423,'cached_version_unverified')
    artifact=p.package.require_artifact(ctx['owner'],row[0],device_id=ctx.get('device_id',''),operation=operation,approval_id=ctx.get('approval_id'))
    return Path(artifact['path'])


def preview_context_kwargs():
    from sjfx_protection.business import enabled
    return {'protection_context':current_provenance()} if enabled() else {}


def run_saved_lane(store,package_id,function):
    with large_package_execution(store,package_id):return function()


@contextmanager
def batch_execution(store,items):
    from sjfx_protection.business import enabled,activate,context
    if not enabled():
        yield
        return
    contexts=[];owner=None
    for pid in dict.fromkeys(str(item['package_id']) for item in items):
        with large_package_execution(store,pid):
            frozen=dict(context() or {})
            if owner is not None and owner!=frozen['owner']:raise ProtectionError('批次不能混合账号来源',423,'owner_mismatch')
            owner=frozen['owner'];contexts.append(frozen)
    value=contexts[0] if len(contexts)==1 else {'owner':owner,'package_contexts':contexts}
    token=_active_packages.set(_active_packages.get()+tuple(str(item['package_id']) for item in items))
    try:
        with activate(value):yield
    finally:_active_packages.reset(token)


def package_batch(function):
    @wraps(function)
    def guarded(self,items):
        try:
            with batch_execution(self,items):return function(self,items)
        except ProtectionError as exc:
            for pid in dict.fromkeys(str(item['package_id']) for item in items):
                self.update_package(pid,status='paused',message='资料保护暂停：'+str(exc))
            raise
    return guarded


def active_lane(function):
    @wraps(function)
    def guarded(*args,**kwargs):
        recheck_active()
        result=function(*args,**kwargs)
        recheck_active()
        return result
    return guarded
