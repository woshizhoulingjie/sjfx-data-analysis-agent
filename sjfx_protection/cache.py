"""Persist output integrity and the *producer's* immutable approved sources.

This is an internal producer/consumer contract, not a historical-cache upgrader.
Call ``record_payload`` only after a producer has consumed checked inputs or raw
approved bytes. A current package approval does not authenticate old DB content.
The OFF path does not open a database or inspect/serialize either payload or key.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import sqlite3
import time
from collections.abc import Mapping
from pathlib import Path

from . import business
from .store import ProtectionError

CONTRACT_VERSION = "payload-provenance-v1"
TABLE = "protection_cache_payloads"
SCHEMA = """
CREATE TABLE IF NOT EXISTS protection_cache_payloads (
 owner TEXT NOT NULL, resource_key TEXT NOT NULL,
 output_sha256 TEXT NOT NULL, sources_json TEXT NOT NULL,
 contract_version TEXT NOT NULL, producer_revision TEXT NOT NULL,
 created REAL NOT NULL,
 PRIMARY KEY(owner,resource_key)
)
"""
SOURCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS protection_cache_source_objects (
 kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(kind,id)
)
"""


def _canonical(value):
    # Explicit type tags keep JSON strings distinct from byte payloads and make
    # integer/float and mapping/list collisions impossible.
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Non-finite payload values cannot be authenticated")
        return ["float", value.hex()]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return ["bytes", base64.b64encode(bytes(value)).decode("ascii")]
    if isinstance(value, Path):
        return ["path", str(value)]
    if isinstance(value, Mapping):
        entries = [[_canonical(key), _canonical(item)] for key, item in value.items()]
        entries.sort(key=lambda item: _json(item[0]))
        return ["map", entries]
    if isinstance(value, (list, tuple)):
        return ["list", [_canonical(item) for item in value]]
    # sqlite3.Row is not a Mapping but has named keys; preserve all selected data.
    if hasattr(value, "keys") and callable(value.keys):
        return _canonical({key: value[key] for key in value.keys()})
    raise TypeError("Unsupported authenticated payload type: " + type(value).__name__)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def canonical_hash(payload):
    return hashlib.sha256(_json(_canonical(payload)).encode("utf-8")).hexdigest()


def resource_key(namespace, kind, business_id, *subkeys):
    """Namespace business keys by the actual DB path, cache kind and all IDs."""
    namespace = str(Path(namespace).resolve())
    return _json([CONTRACT_VERSION, namespace, str(kind), str(business_id),
                  [str(item) for item in subkeys]])


def _error(message, code="cache_provenance_missing"):
    return ProtectionError(message, 423, code)


def _clone(value):
    # Preserve shared immutable version lists while isolating caller mutation.
    # A JSON round trip duplicates a large package manifest for every input row.
    return copy.deepcopy(value)


def _contexts(ctx):
    if not isinstance(ctx, dict):
        raise _error("缓存需要生产时核准来源上下文")
    candidates = ctx.get("package_contexts") or [ctx]
    if not isinstance(candidates, list) or not candidates:
        raise _error("缓存来源上下文为空")
    for item in candidates:
        if (not isinstance(item, dict) or not item.get("owner") or
                not item.get("registration_id") or not item.get("manifest_revision") or
                not item.get("policy_revision") or not isinstance(item.get("versions"), list) or
                not item["versions"]):
            raise _error("缓存来源缺少核准版本集合")
        if ctx.get("owner") and item["owner"] != ctx["owner"]:
            raise _error("缓存集合来源账号不一致", "cache_owner_mismatch")
    return candidates


def capture_sources(context=None):
    """Freeze sources before producing content; pass them at the later save stage.

    For derived/aggregated content, union the sources returned by checked inputs
    instead. This function authenticates package approval, never historical text.
    """
    if not business.enabled():
        return None
    ctx = context if context is not None else business.context()
    candidates = _contexts(ctx)
    result = [{"registration_id": item["registration_id"],
               "versions": item["versions"], "context": item} for item in candidates]
    result = _clone(result)
    p = business.protection()
    for source in result:
        _require_source(p, source, source["context"]["owner"],
                        source["context"].get("operation") or "read")
    return result


def _sources(sources, context):
    if sources is None:
        sources = capture_sources(context)
    if not isinstance(sources, list) or not sources:
        raise _error("缓存没有可复查的生产来源")
    result = _clone(sources)
    _validate_sources(result)
    # Deduplicate complete immutable source records, not just registration IDs;
    # file/member/derived provenance must survive aggregation unchanged.
    digests = {}
    unique = {_source_token(item, digests): item for item in result}
    return [unique[token] for token in sorted(unique)]


def _source_token(source, digests):
    compact = dict(source)
    ctx = dict(compact['context'])
    for holder in (compact, ctx):
        versions = holder['versions']
        marker = id(versions)
        if marker not in digests:
            digests[marker] = (versions, hashlib.sha256(_json(versions).encode('utf-8')).hexdigest())
        holder['versions'] = {'versions_digest': digests[marker][1]}
    compact['context'] = ctx
    return _json(compact)


def _validate_sources(result):
    if not isinstance(result, list) or not result:
        raise _error("缓存没有可复查的生产来源")
    for source in result:
        if not isinstance(source, dict):
            raise _error("缓存来源格式无效")
        ctx = source.get("context")
        _contexts(ctx)
        if ctx.get("package_contexts"):
            raise _error("单项缓存来源必须指定确切登记")
        if (source.get("registration_id") != ctx.get("registration_id") or
                not isinstance(source.get("versions"), list) or not source["versions"]):
            raise _error("缓存来源登记或版本缺失")


def _intern(db, kind, value):
    encoded = _json(value)
    identifier = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
    db.execute('INSERT OR IGNORE INTO protection_cache_source_objects(kind,id,payload) VALUES(?,?,?)',
               (kind, identifier, encoded))
    return identifier


def _pack_sources(db, sources):
    compact = []
    version_ids = {}
    for source in sources:
        item = dict(source)
        ctx = dict(item.pop('context'))
        for holder in (ctx, item):
            identity = id(holder['versions'])
            if identity not in version_ids:
                version_ids[identity] = _intern(db, 'versions', holder['versions'])
            holder['versions'] = {'version_set': version_ids[identity]}
        item['context'] = {'context_object': _intern(db, 'context', ctx)}
        compact.append(item)
    return {'source_set': _intern(db, 'sources', compact)}


def _read_object(db, kind, identifier, memo):
    marker = ('source-object', kind, identifier)
    if marker in memo:
        return memo[marker]
    row = db.execute('SELECT payload FROM protection_cache_source_objects WHERE kind=? AND id=?',
                     (kind, identifier)).fetchone()
    if row is None or hashlib.sha256(row[0].encode('utf-8')).hexdigest() != identifier:
        raise _error('缓存不可变来源对象已缺失或变化')
    value = json.loads(row[0])
    memo[marker] = value
    return value


def _unpack_sources(db, stamp, memo):
    if isinstance(stamp, list):
        # Backward compatibility for records written by the earlier inline
        # contract; consumption still never upgrades or writes those records.
        return _sources(stamp, None), _json(stamp)
    if not isinstance(stamp, dict) or set(stamp) != {'source_set'}:
        raise _error('缓存来源引用格式无效')
    identifier = stamp['source_set']
    marker = ('source-set', identifier)
    if marker in memo:
        return memo[marker], identifier
    result = []
    for compact in _read_object(db, 'sources', identifier, memo):
        item = dict(compact)
        ctx = dict(_read_object(db, 'context', item['context']['context_object'], memo))
        ctx['versions'] = _read_object(db, 'versions', ctx['versions']['version_set'], memo)
        item['versions'] = _read_object(db, 'versions', item['versions']['version_set'], memo)
        item['context'] = ctx
        result.append(item)
    _validate_sources(result)
    memo[marker] = result
    return result, identifier


def union_sources(*groups):
    if not business.enabled():
        return None
    merged = []
    for group in groups:
        if not isinstance(group, list) or not group:
            raise _error("聚合输入缺少历史核准来源")
        merged.extend(group)
    return _sources(merged, None)


def _require_source(p, source, owner, operation):
    ctx = source["context"]
    if ctx["owner"] != owner:
        raise _error("缓存生产来源属于其它账号", "cache_owner_mismatch")
    return p.package.require_operation(
        owner, registration_id=source["registration_id"], operation=operation,
        device_id=ctx.get("device_id") or ctx.get("device") or "",
        context=ctx, versions=source["versions"],
        approval_id=ctx.get("approval_id"), record=False)


def _current(p, owner, sources, operation):
    candidates = _contexts(business.context())
    if any(item["owner"] != owner for item in candidates):
        raise _error("缓存读取账号与当前任务不一致", "cache_owner_mismatch")
    by_id = {item["registration_id"]: item for item in candidates}
    required = {source["registration_id"] for source in sources}
    if not required.issubset(by_id):
        raise _error("缓存来源不在当前任务获准集合内", "cache_scope_mismatch")
    for registration_id in sorted(required):
        ctx = by_id[registration_id]
        p.package.require_operation(
            owner, registration_id=registration_id, operation=operation,
            device_id=ctx.get("device_id") or ctx.get("device") or "",
            context=ctx, versions=ctx["versions"],
            approval_id=ctx.get("approval_id"), record=False)


def _owner(owner, sources=None):
    if owner:
        return str(owner)
    ctx = business.context()
    if isinstance(ctx, dict) and ctx.get("owner"):
        return str(ctx["owner"])
    if sources:
        owners = {source["context"]["owner"] for source in sources}
        if len(owners) == 1:
            return next(iter(owners))
    raise _error("缓存缺少明确资料归属")


def record_payload(key, payload, sources=None, *, context=None, owner=None,
                   producer_revision="", operation=None):
    """Record a newly produced *actual stored output*, with frozen input sources.

    Business writes happen first. If this separate DB write fails, readers reject
    the changed hash/absent record. Never call this on a cache hit or metadata-only
    update: a current gate cannot establish the provenance of historical content.
    """
    if not business.enabled():
        return payload
    sources = _sources(sources, context)
    owner = _owner(owner, sources)
    record_payloads([(key, payload, owner)], sources,
                    producer_revision=producer_revision, operation=operation)
    return payload


def record_payloads(records, sources, *, producer_revision='', operation=None):
    """Commit a producer transaction's exact outputs with one frozen source set."""
    if not business.enabled():
        return
    records = list(records)
    if not records:
        return
    sources = _sources(sources, None)
    p = business.protection()
    owners = {str(owner) for key, payload, owner in records}
    for owner in owners:
        for source in sources:
            _require_source(p, source, owner, operation or source["context"].get("operation") or "read")
    outputs = [(str(owner), str(key), canonical_hash(payload)) for key, payload, owner in records]
    with p.store.db(True) as db:
        db.execute(SCHEMA)
        db.execute(SOURCE_SCHEMA)
        stamp = _json(_pack_sources(db, sources))
        created = time.time()
        db.executemany("INSERT OR REPLACE INTO " + TABLE +
                   "(owner,resource_key,output_sha256,sources_json,contract_version,producer_revision,created) "
                   "VALUES(?,?,?,?,?,?,?)",
                   [(owner, key, output_hash, stamp, CONTRACT_VERSION,
                     str(producer_revision), created) for owner, key, output_hash in outputs])


def require_payload(key, payload, *, owner=None, operation="read", return_sources=False,
                    _gate_memo=None):
    """Check exact output and both current and historical approval before reuse."""
    if not business.enabled():
        return None if return_sources else payload
    owner = _owner(owner)
    p = business.protection()
    memo = _gate_memo if isinstance(_gate_memo, dict) else {}
    with p.store.db() as db:
        # No lazy migration on consumption: missing history remains missing.
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
        row = (db.execute("SELECT output_sha256,sources_json,contract_version FROM " + TABLE +
                          " WHERE owner=? AND resource_key=?", (owner, str(key))).fetchone()
               if exists else None)
        if row is None:
            raise _error("历史缓存尚无核准来源，请重新生成")
        if row["contract_version"] != CONTRACT_VERSION:
            raise _error("缓存来源协议版本不受支持", "cache_contract_mismatch")
        if canonical_hash(payload) != row["output_sha256"]:
            raise _error("缓存内容已变化，不能复用历史核准来源", "cache_output_mismatch")
        try:
            sources, source_id = _unpack_sources(db, json.loads(row["sources_json"]), memo) if row else (None, None)
        except (ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            raise _error("缓存生产来源记录已损坏") from exc
    try:
        _validate_sources(sources)
    except (ValueError, TypeError, KeyError) as exc:
        raise _error("缓存生产来源记录已损坏") from exc
    marker = ('checked-gates', owner, operation, source_id, id(business.context()))
    if _gate_memo is None or marker not in _gate_memo:
        _current(p, owner, sources, operation)
        for source in sources:
            _require_source(p, source, owner, operation)
        if _gate_memo is not None:
            if isinstance(_gate_memo, dict):
                _gate_memo[marker] = True
            else:
                _gate_memo.add(marker)
    return (sources if _gate_memo is not None else _clone(sources)) if return_sources else payload
