"""Explicit package execution and row proofs for the independent QA index."""
import json
from contextlib import contextmanager
from functools import wraps
import inspect
from sjfx_protection import business
from sjfx_protection.store import ProtectionError
from services.large_package_protection import recheck_active

@contextmanager
def qa_store_execution(store,package_id,operation='analyze'):
    if not business.enabled():
        yield
        return
    item=store.get(package_id)
    if not item:raise ProtectionError('问答索引没有实际数据包归属',423,'package_not_found')
    frozen=item.get('protection_context')
    if isinstance(frozen,str):
        try:frozen=json.loads(frozen)
        except ValueError:frozen=None
    with business.saved_execution(item['owner_id'],[item['root_path']],frozen,operation=operation):yield


def qa_lane(function):
    sig=inspect.signature(function)
    @wraps(function)
    def guarded(*args,**kwargs):
        if not business.enabled():return function(*args,**kwargs)
        values=sig.bind(*args,**kwargs).arguments
        store=values.get('store');index=values.get('self') or values.get('index')
        request=values.get('request')
        pid=values.get('package_id') or getattr(index,'package_id',None) or getattr(request,'scan_id',None)
        if store and pid:
            with qa_store_execution(store,pid):return function(*args,**kwargs)
        recheck_active()
        result=function(*args,**kwargs)
        recheck_active()
        return result
    return guarded


def qa_model_text(*args, **kwargs):
    """Recheck independent QA evidence before sending and accepting a model call."""
    from services.conversation import _model_text
    recheck_active()
    try:
        result = _model_text(*args, **kwargs)
    except BaseException:
        recheck_active()
        raise
    recheck_active()
    return result
