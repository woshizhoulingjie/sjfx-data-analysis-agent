"""Small durable policy file; the OFF fast path never opens SQLite or a watcher."""
from __future__ import annotations
import json
import math
import os
import tempfile
import time
from pathlib import Path
from .store import ProtectionError

DEFAULT_POLICY = {
    'enabled': False, 'revision': 0, 'allowed_registration_modes': ['local'],
    'operation_policy': {'bulk_delete': 'approval', 'delete': 'approval','bulk_rewrite':'approval',
                         'sensitive_export': 'approval', 'automatic_quarantine': False,
                         'observation_only': True, 'anomaly_count': 20, 'anomaly_window': 60,'anomaly_response':'pause'},
    'inspection_profile': {'revision': 1, 'rules_revision': 'sjfx-local-20261013-v1',
        'virus_db_revision': 'unknown', 'engine_revision': 'configured',
        'static_max_bytes': 20*1024*1024, 'hash_max_bytes': 32*1024**3,
        'virus_max_bytes': 2*1024**3, 'parse_max_bytes': 2*1024**3,
        'snapshot_quota_bytes': 32*1024**3, 'snapshot_retention_seconds': 7*86400,
        'inspection_ttl_seconds': 86400, 'required_engines': ['static', 'clamav'],
        'large_timeout_seconds': 300,'inventory_max_files':200000,'inventory_timeout_seconds':7200,
        'worker_concurrency':2,'watch_interval_seconds':60},
}

def policy_path(state_dir):
    return Path(state_dir)/'package-policy.json'

def read_policy(state_dir):
    path = policy_path(state_dir)
    if not path.exists():
        return json.loads(json.dumps(DEFAULT_POLICY))
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict) or not isinstance(value.get('enabled'), bool):
            raise ValueError('enabled must be boolean')
        result = json.loads(json.dumps(DEFAULT_POLICY))
        result.update(value)
        return result
    except (OSError, ValueError, TypeError):
        # Corruption of a previously persisted switch must never make protection OFF.
        return {**DEFAULT_POLICY, 'enabled': True, 'invalid': True, 'revision': -1}

def protection_enabled(state_dir):
    return read_policy(state_dir)['enabled']

def write_policy(state_dir, actor, is_admin, changes, reason):
    if not is_admin:
        raise ProtectionError('只有管理员可以修改保护策略',403,'admin_required')
    if not isinstance(changes,dict) or not str(reason).strip():
        raise ProtectionError('修改策略必须提供理由')
    current=read_policy(state_dir)
    allowed={'enabled','allowed_registration_modes','operation_policy','inspection_profile'}
    if set(changes)-allowed:
        raise ProtectionError('不支持的策略字段')
    result=json.loads(json.dumps(DEFAULT_POLICY if current.get('invalid') else current))
    for key,value in changes.items():
        if key in {'operation_policy','inspection_profile'}:
            if not isinstance(value,dict):raise ProtectionError('策略配置必须为对象')
            result[key].update(value)
        else:result[key]=value
    if not isinstance(result['enabled'],bool):raise ProtectionError('enabled 必须为布尔值')
    modes=result['allowed_registration_modes']
    if not isinstance(modes,(list,dict)) or (isinstance(modes,list) and (not modes or set(modes)-{'local'})):
        raise ProtectionError('登记模式策略无效')
    for key in ('static_max_bytes','hash_max_bytes','virus_max_bytes','parse_max_bytes','snapshot_quota_bytes',
                'snapshot_retention_seconds','inspection_ttl_seconds','large_timeout_seconds',
                'inventory_max_files','inventory_timeout_seconds','worker_concurrency','watch_interval_seconds'):
        value=result['inspection_profile'].get(key)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or (isinstance(value,float) and not math.isfinite(value)) or value<=0:
            raise ProtectionError('检查容量和期限必须为有限正数')
    required=result['inspection_profile'].get('required_engines',[])
    if not isinstance(required,list) or not all(isinstance(engine,str) for engine in required):
        raise ProtectionError('必要检查引擎必须为名称列表')
    if set(required)-{'static','clamav','yara_x','rspamd'}:
        raise ProtectionError('未知必要检查引擎')
    if not {'static','clamav'}.issubset(required) and os.getenv('SJFX_PROTECTION_TEST_STATIC_ONLY')!='1':
        raise ProtectionError('正式准入必须同时完成内容规则和真实病毒检查；static-only仅供明确隔离测试',400,'test_profile_required')
    for key in ('delete','bulk_delete','bulk_rewrite','sensitive_export'):
        if result['operation_policy'].get(key) not in {'allow','warn','approval','deny'}:
            raise ProtectionError('操作规则必须为 allow/warn/approval/deny')
    for key,value in result['operation_policy'].items():
        if key not in {'automatic_quarantine','observation_only','anomaly_count','anomaly_window','anomaly_response','role_rules'} and isinstance(value,str) and value not in {'allow','warn','approval','deny'}:
            raise ProtectionError('操作规则必须为 allow/warn/approval/deny')
    for key in ('automatic_quarantine','observation_only'):
        if not isinstance(result['operation_policy'].get(key),bool):raise ProtectionError('自动处置和观察配置必须为布尔值')
    role_rules=result['operation_policy'].get('role_rules',{})
    if not isinstance(role_rules,dict) or any(not isinstance(roles,dict) or any(decision not in {'allow','warn','approval','deny'} for decision in roles.values()) for roles in role_rules.values()):
        raise ProtectionError('角色操作规则格式无效')
    result.update(revision=max(0,int(current.get('revision',0)))+1,
                  updated=time.time(),actor=str(actor),reason=str(reason).strip()[:2000])
    directory=Path(state_dir);directory.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix='.package-policy-',dir=directory)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as output:
            json.dump(result,output,ensure_ascii=False,sort_keys=True);output.flush();os.fsync(output.fileno())
        os.replace(temp,policy_path(directory))
        # Append-only operational history supplements (does not replace) the SQLite hash chain.
        with (directory/'package-policy-history.jsonl').open('a',encoding='utf-8') as history:
            history.write(json.dumps(result,ensure_ascii=False,sort_keys=True)+'\n')
    finally:
        if os.path.exists(temp):os.unlink(temp)
    return result

def mode_allowed(policy, owner, mode):
    modes=policy['allowed_registration_modes']
    if isinstance(modes,dict):modes=modes.get(owner,modes.get('*',['local']))
    return mode in modes
