# -*- coding: utf-8 -*-
"""大数据包智能目录 → 普通数据包导入 的桥接层。

设计依据：《SJFX 大数据包智能目录进入普通数据包导入设计文档》。
交付边界：**已完成智能目录的物理文件及已核验归档成员** → 节点勾选 →
服务端展开与校验 → 创建普通 scan → 普通解析 → 普通摘要/检索/问答。

本模块只做"把服务端算出来的文件集合变成一个普通数据包"，具体是：

    export_selection()  →  服务端得到最终文件集合
                        →  一个事务里创建 scan / inventory / selection /
                           import_task / controls / analysis_job / 桥接审计
                        →  普通 Worker 接手解析

设计原则（文档 §3，逐条落实）：
  1. 前端只提交节点选择，不自行展开文件（本模块只接受 node_ids/paths/排除项）；
  2. 服务端重新计算文件集合（一律走 `LargePackageStore.export_selection`）；
  3. 不复制文件：普通 scan 的 root_path 就是大包根目录，清单里是相对路径；
  4. 创建新的 scan_id，不改动原大包任务；
  5. 后续复用普通导入状态机与 Worker（只写普通表，不新建第二套解析状态机）；
  6. 保存来源与选择快照（`large_catalog_imports` + `scans.payload`）；
  7. 请求幂等键：`(large_package_id, request_key)`；
  8. 被引用的大包不可直接删除（`referenced_by` 供删除路径调用）。

**不修改任何现有的目录算法、命名逻辑与普通导入行为**：本模块只新增。
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from contextlib import closing
from pathlib import Path
from services.scanner import human_size

import logging
import re

LOGGER = logging.getLogger("sjfx.large_catalog_import")

_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:")

# §8.2：压缩包内部成员同时有"物理容器路径"和"逻辑成员路径"，不能把逻辑路径当成
# 普通物理文件。新清点以来源库的物理来源标记判定；旧数据仍保守处理。
_ARCHIVE_SUFFIXES = ('.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz', '.tgz')


def looks_like_archive_member(path):
    """路径里某一层是压缩包、其后还有成员段 → 压缩包内部成员。"""
    parts = str(path or '').replace('\\', '/').split('/')
    return (any(part.lower().endswith(_ARCHIVE_SUFFIXES) for part in parts[:-1])
            or any(suffix + '::' in str(path or '').lower() for suffix in _ARCHIVE_SUFFIXES))


def file_fingerprint(sha256, size, modified_at_ns):
    """文件指纹：优先大包算好的 sha256；没有就退化成 大小+修改时间。"""
    digest = str(sha256 or '').strip()
    return ("sha256:" + digest) if digest else "meta:%d:%d" % (int(size or 0), int(modified_at_ns or 0))


def _source_fingerprint(item):
    value = file_fingerprint(item.get('sha256'), item.get('size'), item.get('modified_at_ns'))
    if item.get('source_kind') == 'archive_member':
        from services.large_archive_members import source_signature
        value += ':archive:' + source_signature(item)
    return value


def load_file_fingerprints(large_store, package_id, paths, *, strict=False):
    """从**大包库**读取选中文件的 sha256/大小/修改时间（§9.2 快照要用）。"""
    if not large_store or not paths:
        return {}
    out = {}
    try:
        with closing(large_store._connect()) as conn:
            # All bounded batches must observe the same source-index revision.
            conn.execute('BEGIN')
            # Older live stores may not yet have undergone the additive migration.
            columns = {row[1] for row in conn.execute('PRAGMA table_info(files)')}
            origin = 'source_kind' if 'source_kind' in columns else "'' AS source_kind"
            metadata = 'source_metadata' if 'source_metadata' in columns else "'{}' AS source_metadata"
            for chunk_start in range(0, len(paths), 500):
                chunk = list(paths[chunk_start:chunk_start + 500])
                marks = ','.join('?' * len(chunk))
                for row in conn.execute(
                        "SELECT path,sha256,size,modified_at_ns,sensitive," + origin + "," + metadata + " FROM files "
                        "WHERE package_id=? AND path IN (%s)" % marks,
                        [str(package_id)] + chunk):
                    out[str(row['path'])] = {'path': row['path'], 'sha256': row['sha256'],
                                             'size': row['size'],
                                             'modified_at_ns': row['modified_at_ns'],
                                             'sensitive': bool(row['sensitive']),
                                             'source_kind': str(row['source_kind'] or ''),
                                             'source_metadata': json.loads(row['source_metadata'] or '{}')}
    except Exception:
        LOGGER.exception('load_file_fingerprints failed: %s', package_id)
        if strict:
            raise SelectionError('暂时无法核验来源文件版本，请稍后重试；未创建导入任务', 503)
    return out


def detect_source_changes(storage, large_store, scan_id):
    """§9.2：普通数据包创建后源文件是否变过。变化则标记 `source_changed`。

    返回变化文件的列表（空列表＝没有变化）。**不静默**：一旦有变化就把
    `scans.payload.source_changed=true` 与变化清单一并落库，供解析/报告如实标注。
    """
    with storage._connect() as conn:
        scan = conn.execute("SELECT payload FROM scans WHERE id=?", (str(scan_id),)).fetchone()
        rows = conn.execute("SELECT node_path,payload FROM inventory_entries "
                            "WHERE scan_id=? AND kind='file'", (str(scan_id),)).fetchall()
    if not scan:
        return []
    try:
        payload = json.loads(scan[0] or '{}')
    except (TypeError, ValueError):
        payload = {}
    package_id = str(payload.get('source_large_package_id') or '')
    if not package_id or not large_store:
        return []
    stored = {}
    for row in rows:
        try:
            item = json.loads(row[1] or '{}')
        except (TypeError, ValueError):
            item = {}
        stored[str(row[0])] = item.get('fingerprint') or ''
    current = load_file_fingerprints(large_store, package_id, list(stored))
    changed = []
    for path, was in stored.items():
        now = current.get(path)
        if now is None:
            changed.append({'path': path, 'reason': 'missing'})
            continue
        if _source_fingerprint(now) != was:
            changed.append({'path': path, 'reason': 'changed'})
    payload['source_changed'] = bool(changed)
    payload['source_changed_files'] = changed[:200]
    payload['source_checked_at'] = time.time()
    with storage.lock, storage._connect() as conn:
        conn.execute("UPDATE scans SET payload=? WHERE id=?",
                     (json.dumps(payload, ensure_ascii=False), str(scan_id)))
    return changed

# 与 `/export` 路由同源的规模上限（文档 §5.1「节点、文件和容量沿用系统配置上限」）
MAX_NODES = 2000
MAX_FILES = 50000
MAX_EXCLUSIONS = 50000
MAX_NAME_CHARS = 120

DDL = """
CREATE TABLE IF NOT EXISTS large_catalog_imports (
    id TEXT PRIMARY KEY,
    large_package_id TEXT NOT NULL,
    scan_id TEXT NOT NULL UNIQUE,
    job_id TEXT,
    owner_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    request_payload TEXT NOT NULL,
    selection_snapshot TEXT NOT NULL,
    selected_file_count INTEGER NOT NULL DEFAULT 0,
    selected_bytes INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'created',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(large_package_id, request_key)
);
CREATE INDEX IF NOT EXISTS large_catalog_imports_package
    ON large_catalog_imports(large_package_id);
"""


def init(conn):
    """幂等建表。由 Storage.init 调用一次，重复调用无副作用。"""
    conn.executescript(DDL)


class SelectionError(ValueError):
    """选择不合法 —— 路由层翻译成 400/404/409/413。"""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = int(status)


# --------------------------------------------------------------------------
# 校验（文档 §4.2 / §9.1）
# --------------------------------------------------------------------------
def _clean_paths(values):
    """规整并校验相对路径。

    **绝对路径必须在 strip("/") 之前判断。** 先 strip 会把 "/etc/passwd" 悄悄变成
    "etc/passwd" 从而混过校验 —— 这正是文档 §4.2/§9.1「拒绝绝对路径和 `..`」要防的。
    （这条是回归测试抓出来的：`test_rejects_path_traversal_and_absolute_paths`。）
    """
    out = []
    seen = set()
    for value in values or []:
        raw = str(value).strip()
        if not raw:
            continue
        text = raw.replace("\\", "/")
        if text.startswith("/") or _WINDOWS_ABSOLUTE.match(text):
            raise SelectionError("选择中包含非法路径")
        text = text.strip("/")
        if not text:
            continue
        if any(part == ".." for part in text.split("/")):
            raise SelectionError("选择中包含非法路径")
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out


def validate_request(payload, large_package):
    """把请求体规整成受信任的入参。任何不合法都抛 SelectionError。"""
    payload = payload or {}
    node_ids = payload.get("node_ids") or []
    paths = payload.get("paths") or []
    if not isinstance(node_ids, list) or not isinstance(paths, list):
        raise SelectionError("目录节点和文件选择必须是列表")
    node_ids = [str(v).strip() for v in node_ids if str(v).strip()]
    node_ids = list(dict.fromkeys(node_ids))
    paths = _clean_paths(paths)
    exclude_paths = _clean_paths(payload.get("exclude_paths") or [])
    exclude_node_ids = [str(v).strip() for v in (payload.get("exclude_node_ids") or []) if str(v).strip()]
    exclude_node_ids = list(dict.fromkeys(exclude_node_ids))

    if not node_ids and not paths:
        raise SelectionError("请至少选择一个目录节点或文件")
    if len(node_ids) > MAX_NODES or len(paths) > MAX_FILES or len(exclude_paths) > MAX_EXCLUSIONS:
        raise SelectionError("一次导入的选择数量超出上限", 413)
    # 临时节点不是"已生成的智能目录"，不能进普通导入（文档 §4.2）
    for node_id in node_ids + exclude_node_ids:
        if node_id.startswith(("preview:", "unread:")):
            raise SelectionError("该目录节点尚未形成可导入的智能目录", 409)

    name = str(payload.get("name") or "").strip()
    if not name:
        raise SelectionError("请输入普通数据包名称")
    if len(name) > MAX_NAME_CHARS:
        raise SelectionError("名称不能超过 %d 个字符" % MAX_NAME_CHARS)

    parse_mode = str(payload.get("parse_mode") or "auto").strip().lower()
    if parse_mode not in {"auto", "fast", "accurate"}:
        raise SelectionError("解析模式只能是 auto、fast 或 accurate")

    request_key = str(payload.get("request_key") or "").strip()
    if not request_key:
        raise SelectionError("缺少幂等请求号")
    if len(request_key) > 200:
        raise SelectionError("幂等请求号过长")

    status = str(large_package.get("status") or "")
    if status in {"cancelled", "paused"}:
        raise SelectionError("当前大数据包不可用，无法进入普通导入", 409)

    return {
        "node_ids": node_ids, "paths": paths,
        "exclude_paths": exclude_paths, "exclude_node_ids": exclude_node_ids,
        "name": name, "parse_mode": parse_mode, "request_key": request_key,
    }


def selection_fingerprint(selection):
    """选择内容的稳定指纹 —— 用于幂等与"同参数/不同参数"判定（文档 §5.3）。"""
    material = json.dumps({
        "nodes": sorted(str(item.get("node_id")) for item in selection.get("nodes") or []),
        "paths": sorted(str(p) for p in selection.get("paths") or []),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def build_snapshot(selection, large_package):
    """文档 §4.3 的选择快照；§9.2 要求的指纹一并落进去。

    注意 `export_selection()` 返回的是
        {"paths": [...], "inventory": {path: 条目}, "nodes": [...]}
    —— `inventory` 是**按路径索引的字典**，而且返回体里**没有** selected_bytes，
    所以字节数一律从清单里算，不要依赖调用方传。
    """
    paths = list(selection.get("paths") or [])
    nodes = list(selection.get("nodes") or [])
    inventory = selection.get("inventory") or {}
    total_bytes = sum(int((item or {}).get("size") or 0) for item in inventory.values())
    return {
        "large_package_id": str(large_package.get("id") or ""),
        "root_path": str(large_package.get("root_path") or ""),
        "nodes": [{"node_id": item.get("node_id"), "name": item.get("name"),
                   "file_count": int(item.get("file_count") or 0),
                   "kind": item.get("kind") or "topic"} for item in nodes],
        "paths": paths,
        "selected_file_count": len(paths),
        "selected_bytes": total_bytes,
        "selection_fingerprint": selection_fingerprint(selection),
        "captured_at": time.time(),
    }


def _import_identity(records, root_path, owner_id, parse_mode):
    """Bounded metadata hashing, never a second read/copy of large source files."""
    digest = hashlib.sha256()
    for record in sorted(records, key=lambda item: item['path']):
        item = record['payload']
        version = [record['path'], str(item.get('sha256') or ''),
                   int(item.get('size') or 0), int(item.get('modified_at_ns') or 0)]
        if item.get('source_kind') == 'archive_member':
            from services.large_archive_members import source_signature
            version.append(source_signature(item))
        digest.update(json.dumps(version, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        digest.update(bytes([10]))
    return {'version': 1, 'owner_id': str(owner_id or 'legacy'),
            'parse_mode': 'fast' if parse_mode == 'fast' else 'accurate',
            'root_path': str(Path(root_path)), 'source_versions': digest.hexdigest()}


def _stored_import_identity(conn, record, snapshot):
    """Recover pre-upgrade identity from persisted job options and inventory."""
    scan = conn.execute('SELECT root_path,owner_id FROM scans WHERE id=?',
                        (record['scan_id'],)).fetchone()
    job = conn.execute('SELECT options,owner_id FROM analysis_jobs WHERE id=? AND scan_id=?',
                       (record.get('job_id'), record['scan_id'])).fetchone()
    if (not scan or not job or scan['owner_id'] != record.get('owner_id')
            or job['owner_id'] != scan['owner_id']):
        return None
    try:
        options = json.loads(job['options'] or '{}')
        if not isinstance(options, dict) or options.get('parse_mode') not in {'fast', 'accurate', 'auto'}:
            return None
        if 'import_identity' in snapshot:
            identity = snapshot['import_identity']
            if (not isinstance(identity, dict) or identity.get('owner_id') != scan['owner_id']
                    or identity.get('root_path') != str(Path(scan['root_path']))
                    or identity.get('parse_mode') != ('fast' if options['parse_mode'] == 'fast' else 'accurate')
                    or not _same_target_paths(snapshot.get('imported_paths'), options.get('target_paths'))):
                return None
            return identity
        records = []
        for row in conn.execute(
                "SELECT node_path,payload FROM inventory_entries WHERE scan_id=? AND kind='file'",
                (record['scan_id'],)):
            item = json.loads(row['payload'] or '{}')
            if not isinstance(item, dict) or item.get('path') != row['node_path']:
                return None
            records.append({'path': row['node_path'], 'payload': item})
        if not records:
            return None
        if not _same_target_paths([item['path'] for item in records], options.get('target_paths')):
            return None
        return _import_identity(records, scan['root_path'], scan['owner_id'], options['parse_mode'])
    except (TypeError, ValueError):
        return None


def _same_target_paths(expected, actual):
    return (isinstance(expected, list) and isinstance(actual, list)
            and all(isinstance(path, str) for path in expected + actual)
            and len(expected) == len(actual) and set(expected) == set(actual))


# --------------------------------------------------------------------------
# 幂等与引用（文档 §5.3 / §9.3）
# --------------------------------------------------------------------------
def _ensure_table(storage):
    with storage.lock, storage._connect() as conn:
        init(conn)


def find_import(storage, large_package_id, request_key, *, conn=None):
    if conn is None:
        _ensure_table(storage)
        with storage._connect() as current:
            return find_import(storage, large_package_id, request_key, conn=current)
    # 注意：`Storage._connect()` 是 @contextmanager（生成器），不是裸连接 ——
    # 不能套 closing()，用普通 with 即可。
    row = conn.execute(
        "SELECT * FROM large_catalog_imports WHERE large_package_id=? AND request_key=?",
        (str(large_package_id), str(request_key)),
    ).fetchone()
    return dict(row) if row else None


def find_active_import_by_fingerprint(storage, large_package_id, fingerprint, *, identity=None, conn=None):
    """§5.3：同一大包内选择指纹相同且任务仍活动的导入 → 复用，避免重复解析。"""
    if not fingerprint:
        return None
    if conn is None:
        _ensure_table(storage)
        with storage._connect() as current:
            return find_active_import_by_fingerprint(storage, large_package_id, fingerprint,
                                                     identity=identity, conn=current)
    # Do not lose active jobs behind an arbitrary recent-history limit. Iterate
    # records instead of loading all selection snapshots into Python memory.
    owner_clause = ' AND i.owner_id=? AND s.owner_id=?' if identity else ''
    params = [str(large_package_id)]
    if identity:
        params.extend([identity['owner_id'], identity['owner_id']])
    rows = conn.execute(
        "SELECT i.* FROM large_catalog_imports i JOIN scans s ON s.id=i.scan_id "
        "WHERE i.large_package_id=?" + owner_clause + " ORDER BY i.created_at DESC", params)
    for row in rows:
        record = dict(row)
        try:
            prior = json.loads(record.get("selection_snapshot") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(prior, dict) or prior.get("selection_fingerprint") != fingerprint:
            continue
        if identity is not None and _stored_import_identity(conn, record, prior) != identity:
            continue
        scan_id = record.get("scan_id")
        job = conn.execute('SELECT status FROM analysis_jobs WHERE id=? AND scan_id=?',
                           (record.get('job_id'), scan_id)).fetchone()
        task = conn.execute("SELECT status FROM import_tasks WHERE scan_id=? LIMIT 1",
                            (scan_id,)).fetchone()
        if not job or not task:
            continue
        job_status = str((job or {})["status"] if job else "")
        task_status = str((task or {})["status"] if task else "")
        done = {"completed", "partial", "failed", "cancelled", "deleted"}
        if job_status in done or task_status in done:
            continue
        return record
    return None


def _scan_alive(storage, scan_id, *, conn=None):
    """桥接记录指向的普通数据包是否还在（Phase 3 任务恢复用）。"""
    if not scan_id:
        return False
    if conn is None:
        with storage._connect() as current:
            return _scan_alive(storage, scan_id, conn=current)
    row = conn.execute("SELECT 1 FROM scans WHERE id=?", (str(scan_id),)).fetchone()
    return bool(row)


def _request_reuse(existing, fingerprint, *, identity=None, storage=None, conn=None):
    """The same key has the same conflict rules before and inside a transaction."""
    if identity is not None and conn is None:
        with storage._connect() as current:
            return _request_reuse(existing, fingerprint, identity=identity, conn=current)
    try:
        prior = json.loads(existing.get("selection_snapshot") or "{}")
    except (TypeError, ValueError):
        prior = {}
    if not isinstance(prior, dict) or prior.get("selection_fingerprint") != fingerprint:
        raise SelectionError("相同请求号的选择与首次不一致，请更换请求号重试", 409)
    if identity is not None and _stored_import_identity(conn, existing, prior) != identity:
        raise SelectionError('相同请求号的账号、解析模式或来源文件版本已变化，请刷新后使用新请求号', 409)
    return {"scan_id": existing["scan_id"], "job_id": existing.get("job_id"),
            "selected_file_count": int(existing.get("selected_file_count") or 0),
            "selected_bytes": int(existing.get("selected_bytes") or 0),
            "reused": True}


def list_imports(storage, large_package_id, limit=50):
    """审计日志：某大数据包的全部普通导入记录（含状态机轨迹与排除统计）。"""
    _ensure_table(storage)
    with storage._connect() as conn:
        rows = conn.execute(
            "SELECT * FROM large_catalog_imports WHERE large_package_id=? "
            "ORDER BY created_at DESC LIMIT ?",
            (str(large_package_id), int(limit))).fetchall()
    out = []
    for row in rows:
        record = dict(row)
        try:
            snapshot = json.loads(record.get("selection_snapshot") or "{}")
        except (TypeError, ValueError):
            snapshot = {}
        record["selection_snapshot"] = snapshot
        try:
            record["request_payload"] = json.loads(record.get("request_payload") or "{}")
        except (TypeError, ValueError):
            record["request_payload"] = {}
        record["selection_fingerprint"] = snapshot.get("selection_fingerprint")
        record["state_trace"] = snapshot.get("state_trace") or []
        record["excluded"] = snapshot.get("excluded") or {}
        record["source_node_names"] = [str(n.get("name") or "")
                                        for n in (snapshot.get("nodes") or [])]
        record["scan_alive"] = _scan_alive(storage, record.get("scan_id"))
        out.append(record)
    return out


def apply_source_change_check(storage, large_store, scan_id, job_id=""):
    """§9.2：解析前检查来自大包的普通数据包，源文件是否变过。

    · 只对 `scans.payload.source_kind == large_catalog_selection` 生效；
    · **每个任务只检查一次**（记住 `source_checked_job_id`），不轮询；
    · 变化时由 `detect_source_changes()` 落 `source_changed` + 变化清单；
    · 任何异常都不向外抛（解析绝不因为这项检查失败）。
    """
    scan_id = str(scan_id or "")
    job_id = str(job_id or "")
    if not scan_id:
        return []
    try:
        def _read():
            with storage._connect() as conn:
                row = conn.execute("SELECT payload FROM scans WHERE id=?",
                                   (scan_id,)).fetchone()
            try:
                return json.loads((row[0] if row else "") or "{}")
            except (TypeError, ValueError):
                return {}

        payload = _read()
        if str(payload.get("source_kind") or "") != "large_catalog_selection":
            return []
        if not payload.get("source_large_package_id"):
            return []
        if job_id and str(payload.get("source_checked_job_id") or "") == job_id:
            return []
        changed = detect_source_changes(storage, large_store, scan_id)
        payload = _read()                      # detect 已写入 source_changed，勿覆盖
        payload["source_checked_job_id"] = job_id
        with storage.lock, storage._connect() as conn:
            conn.execute("UPDATE scans SET payload=? WHERE id=?",
                         (json.dumps(payload, ensure_ascii=False), scan_id))
        if changed:
            LOGGER.warning('large catalog source changed scan=%s files=%d',
                           scan_id, len(changed))
        return changed
    except Exception:                          # noqa: BLE001
        LOGGER.exception('source change check failed scan=%s', scan_id)
        return []


def _tree_from_records(records, root_payload):
    """与 `Storage.build_inventory_tree()` 同一套算法：把清单记录拼成物理目录树。

    普通分析流水线需要 `scans.payload["tree"]`（读，不是只写），所以桥接必须自己
    产出这个键，形状必须与普通 scan 一致。
    """
    nodes, parents = {}, {}
    for record in records:
        node = dict(record["payload"])
        node["children"] = []
        nodes[record["path"]] = node
        parents[record["path"]] = record["parent_path"]
    root = dict(root_payload)
    root["children"] = []
    nodes["."] = root
    parents["."] = None
    for path in sorted(nodes, key=lambda value: (value.count("/"), value)):
        parent = parents.get(path)
        if parent is not None and parent in nodes:
            nodes[parent]["children"].append(nodes[path])
    for node in nodes.values():
        node["children"].sort(
            key=lambda item: (item.get("kind") != "directory",
                              str(item.get("name") or "").casefold()))
    return nodes["."]


def missing_nodes(large_store, package_id, node_ids):
    """§11：选择里有哪些节点在智能目录里并不存在（路由据此返回 404 让用户刷新）。"""
    if not node_ids:
        return []
    missing = []
    with closing(large_store._connect()) as conn:
        from services.large_package_catalog_hierarchy import visible
        branches = {str(item.get("node_id")) for item in visible(conn, package_id)}
        for node_id in node_ids:
            if node_id in branches:
                continue
            row = conn.execute("SELECT 1 FROM large_catalog_groups WHERE package_id=? AND id=?",
                               (str(package_id), node_id)).fetchone()
            if not row:
                missing.append(node_id)
    return missing


def missing_paths(large_store, package_id, paths):
    """§11：选择里有哪些文件已不属于该大包（路由据此返回 409 并附上文件列表）。"""
    if not paths:
        return []
    missing = []
    with closing(large_store._connect()) as conn:
        for chunk_start in range(0, len(paths), 500):
            chunk = list(paths[chunk_start:chunk_start + 500])
            marks = ','.join('?' * len(chunk))
            found = {str(r[0]) for r in conn.execute(
                "SELECT path FROM files WHERE package_id=? AND path IN (%s)" % marks,
                [str(package_id)] + chunk)}
            missing.extend(path for path in chunk if path not in found)
    return missing


def references_for(storage, large_package_id):
    """该大包被哪些普通数据包引用（文档 §9.3：有引用时删除返回 409）。"""
    _ensure_table(storage)
    with storage._connect() as conn:
        rows = conn.execute(
            "SELECT scan_id, job_id, status, created_at FROM large_catalog_imports "
            "WHERE large_package_id=? ORDER BY created_at",
            (str(large_package_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def referenced_by(storage, large_package_id):
    return bool(references_for(storage, large_package_id))


# --------------------------------------------------------------------------
# 建立普通数据包（文档 §4.4，一个事务；任一步失败全部回滚）
# --------------------------------------------------------------------------
def _inventory_records(selection, large_package, fingerprints=None):
    """把选中文件写成普通清单记录。**不复制文件**，只引用大包根目录 + 相对路径。

    §8.1：敏感文件不进普通导入；§8.2：压缩包内部成员首版不接受；
    §9.2：每条记录带文件指纹，供解析前判断源文件是否变过。
    """
    fingerprints = fingerprints or {}
    package_id = str(large_package.get("id") or "")
    records = []
    directories = {}
    excluded = {"sensitive": 0, "archive_member": 0}
    # `inventory` 是 {path: 条目} 字典（见 build_snapshot 的说明）
    for item in (selection.get("inventory") or {}).values():
        item = item or {}
        path = str(item.get("path") or "")
        if not path:
            continue
        mark = fingerprints.get(path) or {}
        if bool(item.get("sensitive")) or bool(mark.get('sensitive')):
            excluded["sensitive"] += 1
            continue
        # Trust only the source database, never a caller-supplied inventory flag.
        # A real directory called reports.zip is not an archive container.
        source_kind = str(mark.get('source_kind') or '')
        logical = {}
        if source_kind == 'archive_member':
            from services.large_archive_members import member_node
            try:
                logical = member_node({**item, **mark})
            except ValueError:
                excluded['archive_member'] += 1
                continue
        elif source_kind != 'physical_file' and looks_like_archive_member(path):
            excluded["archive_member"] += 1
            continue
        parent = path.rsplit("/", 1)[0] if "/" in path else "."
        ancestor = parent
        while ancestor != '.' and ancestor not in directories:
            directories[ancestor] = True
            ancestor = ancestor.rsplit('/', 1)[0] if '/' in ancestor else '.'
        records.append({
            "path": path,
            "parent_path": parent,
            "position": len(records),
            "payload": {
                **logical,
                "kind": "file",
                "source_kind": source_kind,
                "path": path,
                "name": item.get("name") or Path(path).name,
                "extension": item.get("extension") or Path(path).suffix.lower(),
                "size": int(item.get("size") or 0),
                "modified_at_ns": int(item.get("modified_at_ns") or 0),
                # §9.2 文件指纹（优先大包算好的 sha256）
                "sha256": str(mark.get("sha256") or ""),
                "fingerprint": _source_fingerprint({**item, **mark}),
                # 来源可追溯（文档 §6）
                "source_large_package_id": package_id,
                "source_path": path,
            },
        })
    return records, sorted(directories), excluded


def create_scan_from_large_selection(storage, large_package, selection, name, parse_mode,
                                     owner_id, request_key, request_payload=None,
                                     large_store=None):
    """在**一个事务**里创建普通数据包并挂上解析任务。

    返回 `{"scan_id","job_id","selected_file_count","selected_bytes","reused":bool}`。
    幂等：`(large_package_id, request_key)` 命中已有记录时直接返回第一次的 id；
    相同请求号但选择指纹不同则报 409（文档 §5.3）。
    """
    started_at = time.time()
    _ensure_table(storage)          # 懒建桥接表：不触碰既有的 Storage.init 脚本
    package_id = str(large_package.get("id") or "")
    snapshot = build_snapshot(selection, large_package)
    fingerprint = snapshot["selection_fingerprint"]
    paths = list(snapshot["paths"])
    if not paths:
        raise SelectionError("没有可导入的有效文件")
    if parse_mode not in {'auto', 'fast', 'accurate'}:
        raise SelectionError('解析模式只能是 auto、fast 或 accurate')
    root_path = str(large_package.get('root_path') or '')
    owner = str(owner_id or 'legacy')
    marks = load_file_fingerprints(large_store, package_id, paths, strict=True)
    inventory = selection.get('inventory') or {}
    if set(inventory) != set(paths):
        raise SelectionError('选择与文件清单不一致，请刷新后重试', 409)
    for path in paths:
        item = inventory[path] or {}
        if item.get('path') != path:
            raise SelectionError('选择与文件清单不一致，请刷新后重试', 409)
        if large_store is not None:
            mark = marks.get(path)
            if mark is None or any(int(mark.get(field) or 0) != int(item.get(field) or 0)
                                   for field in ('size', 'modified_at_ns')):
                raise SelectionError('来源文件在选择后发生变化，请刷新目录后重试', 409)
    records, directories, excluded_counts = _inventory_records(
        selection, large_package, fingerprints=marks)
    if not records:
        if excluded_counts['archive_member']:
            raise SelectionError('压缩包成员缺少有效来源身份，请重新盘点后再导入', 409)
        if excluded_counts['sensitive']:
            raise SelectionError('选中的文件都被标记为敏感，不能进入普通导入')
        raise SelectionError('没有可导入的有效文件')
    identity = _import_identity(records, root_path, owner, parse_mode)
    snapshot['import_identity'] = identity
    # Keep the requested scope for audit, but never send excluded paths to a worker.
    paths = [record['path'] for record in records]
    snapshot['imported_paths'] = paths

    existing = find_import(storage, package_id, request_key)
    if existing and _scan_alive(storage, existing.get("scan_id")):
        return _request_reuse(existing, fingerprint, identity=identity, storage=storage)
    if existing:
        # Phase 3 任务恢复：幂等记录还在、普通数据包已被删除 → 不返回死 id，重建。
        LOGGER.warning('large catalog import request=%s 指向的普通数据包已不存在，重建任务',
                       request_key)

    # §5.3 第三条：相同文件集合且任务仍活动 → 复用，不做第二次解析。
    active = find_active_import_by_fingerprint(storage, package_id, fingerprint, identity=identity)
    if active:
        LOGGER.info('large catalog import reused by fingerprint package=%s scan=%s',
                    package_id, active.get("scan_id"))
        return {"scan_id": active["scan_id"], "job_id": active.get("job_id"),
                "selected_file_count": int(active.get("selected_file_count") or 0),
                "selected_bytes": int(active.get("selected_bytes") or 0),
                "reused": True, "reused_reason": "same_selection_active"}

    scan_id = "scan-" + uuid.uuid4().hex[:12]
    job_id = uuid.uuid4().hex[:12]
    bridge_id = uuid.uuid4().hex[:16]
    now = time.time()
    total_bytes = sum(int(r["payload"]["size"]) for r in records)
    snapshot['requested_file_count'] = snapshot['selected_file_count']
    snapshot['requested_bytes'] = snapshot['selected_bytes']
    snapshot['selected_file_count'] = len(records)
    snapshot['selected_bytes'] = total_bytes
    type_counts = {}
    for record in records:
        ext = record["payload"]["extension"] or "(none)"
        type_counts[ext] = type_counts.get(ext, 0) + 1

    scan_payload = {
        "root": root_path, "scan_id": scan_id,
        "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "parse_mode": identity['parse_mode'],
        "inventory_mode": "large_catalog_selection_v1",
        "inventory_complete": True,
        "file_count": len(records),
        "directory_count": len(directories),
        "scanned_node_count": len(records) + len(directories) + 1,
        "total_size": total_bytes,
        "total_size_human": human_size(total_bytes),
        "type_counts": type_counts,
        "truncated": False,
        # 来源信息（文档 §6）
        "source_kind": "large_catalog_selection",
        "source_large_package_id": package_id,
        "source_catalog_id": package_id,
        "source_node_ids": [item["node_id"] for item in snapshot["nodes"]],
        "source_node_names": [item["name"] for item in snapshot["nodes"]],
        "source_request_key": request_key,
        "selected_paths": paths,
        "selection_fingerprint": fingerprint,
    }
    # 中间目录也要有清单记录：普通 scan 一向如此，`build_inventory_tree()` 正是靠
    # 目录节点把文件挂到父级；缺了它们，文件在树里会变成孤儿。
    directory_records = []
    for index, directory in enumerate(directories):
        if not directory or directory == ".":
            continue
        parent = directory.rsplit("/", 1)[0] if "/" in directory else "."
        directory_records.append({
            "path": directory, "parent_path": parent, "position": index,
            "payload": {"kind": "directory", "path": directory, "name": Path(directory).name,
                        "source_large_package_id": package_id},
        })
    root_dict = {"kind": "directory", "path": ".", "name": Path(root_path).name or root_path,
                 "source_large_package_id": package_id}
    root_payload = json.dumps(root_dict, ensure_ascii=False)
    # §9.2/一致性：普通分析流水线会读 `scan["tree"]`，必须与普通 scan 形状一致。
    scan_payload["tree"] = _tree_from_records(records + directory_records, root_dict)
    scan_payload["directory_count"] = len(directory_records) + 1
    scan_payload["scanned_node_count"] = len(records) + len(directory_records) + 1

    _dumps = lambda item: json.dumps(item["payload"], ensure_ascii=False, separators=(",", ":"))
    inventory_rows = [(scan_id, r["path"], r["parent_path"], r["position"],
                       "directory" if r["payload"].get("kind") == "directory" else "file", _dumps(r))
                      for r in (records + directory_records)]
    inventory_rows.append((scan_id, ".", None, 0, "directory", root_payload))
    # The paged UI consumes immediate-child counts and sibling positions.
    # Reuse the already sorted full tree instead of marking every folder as a
    # leaf or giving the root its total descendant count (phantom next pages).
    child_counts, sibling_positions = {}, {}
    pending = [scan_payload["tree"]]
    while pending:
        node = pending.pop()
        children = node.get("children") or []
        child_counts[node["path"]] = len(children)
        for position, child in enumerate(children):
            sibling_positions[child["path"]] = position
            pending.append(child)
    tree_rows = [(scan_id, "physical", r["path"], r["parent_path"],
                  sibling_positions[r["path"]], _dumps(r), child_counts[r["path"]])
                 for r in (records + directory_records)]
    tree_rows.append((scan_id, "physical", ".", None, 0, root_payload,
                      child_counts["."]))

    checkpoint = {"selected_paths": paths, "selection_version": 1,
                  "preliminary_summary_job_ids": [], "preliminary_node_job_ids": [],
                  "preliminary_overview_job_id": None,
                  "workflow_source": "large_catalog_selection"}

    options = {
        "target_paths": paths,
        # Reuse the ordinary selected-file pipeline, including preliminary
        # file/node summaries and overview before any idle deep backfill.
        # Provenance remains separately recorded in the scan and checkpoint.
        "workflow_source": "selected_file_parse",
        "scope_label": "来自大数据包智能目录的选择（%d 个文件）" % len(paths),
        "parse_mode": identity['parse_mode'],
        "continue_full": False,
        "candidate_preview": False,
        "selection_version": 1,
        "deep_batch_id": None,
        "deep_selection": {"kind": "files", "paths": paths, "selected_file_count": len(paths)},
        "source_large_package_id": package_id,
        "source_node_ids": [item["node_id"] for item in snapshot["nodes"]],
    }
    priority = storage._job_priority("analyze_package", options)

    # §10 桥接状态机轨迹。本桥接**同步完成**（只写库、不调模型），所以这里把经过的
    # 状态如实记录下来，供审计与任务恢复排查；后续状态由普通 import_tasks/analysis_jobs 管。
    snapshot["excluded"] = dict(excluded_counts)
    snapshot["state_trace"] = [
        {"state": "created", "at": started_at},
        {"state": "validating", "at": started_at},
        {"state": "resolving_selection", "at": started_at},
        {"state": "materializing_scan", "at": time.time()},
        {"state": "queued", "at": time.time()},
    ]

    with storage.lock, storage._connect() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            # The optimistic lookups above are only a fast path. Another API
            # process can commit while we materialize the selected inventory.
            # Recheck on this SAME connection under SQLite's writer reservation
            # before inserting anything; no nested DDL/connection may release it.
            existing = find_import(storage, package_id, request_key, conn=conn)
            if existing and _scan_alive(storage, existing.get("scan_id"), conn=conn):
                return _request_reuse(existing, fingerprint, identity=identity, conn=conn)
            active = find_active_import_by_fingerprint(storage, package_id, fingerprint,
                                                       identity=identity, conn=conn)
            if active:
                return {"scan_id": active["scan_id"], "job_id": active.get("job_id"),
                        "selected_file_count": int(active.get("selected_file_count") or 0),
                        "selected_bytes": int(active.get("selected_bytes") or 0),
                        "reused": True, "reused_reason": "same_selection_active"}
            conn.execute("INSERT INTO scans(id,root_path,payload,owner_id) VALUES (?,?,?,?)",
                         (scan_id, root_path, json.dumps(scan_payload, ensure_ascii=False), owner))
            conn.executemany(
                "INSERT INTO inventory_entries(scan_id,node_path,parent_path,position,kind,payload,updated_at) "
                "VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)", inventory_rows)
            conn.executemany(
                "INSERT INTO tree_nodes(scan_id,tree_kind,node_key,parent_key,position,payload,child_count) "
                "VALUES (?,?,?,?,?,?,?)", tree_rows)
            conn.execute(
                "INSERT INTO scan_selections(scan_id,status,version,included_paths,excluded_paths,rules,"
                "inventory_fingerprint,confirmed_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                (scan_id, "confirmed", 1, json.dumps(paths, ensure_ascii=False), "[]",
                 json.dumps({"source_kind": "large_catalog_selection",
                             "source_large_package_id": package_id}, ensure_ascii=False),
                 fingerprint))
            conn.execute(
                "INSERT INTO import_tasks(task_id,scan_id,root_path,status,total_files,discovered_files,"
                "current_phase,checkpoint,owner_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, scan_id, root_path, "parsing_selected", len(records), len(records),
                 "parsing_selected", json.dumps(checkpoint, ensure_ascii=False), owner, now, now))
            conn.execute("INSERT OR IGNORE INTO package_processing_controls("
                         "scan_id,state,pause_requested,reason) VALUES (?,'running',0,NULL)", (scan_id,))
            conn.execute(
                "INSERT INTO analysis_jobs(id,scan_id,task_type,status,stage,progress,message,options,"
                "owner_id,priority,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                (job_id, scan_id, "analyze_package", "queued", "parsing_selected", 0,
                 "已提交选中文件解析任务，等待 Worker 开始。",
                 json.dumps(options, ensure_ascii=False), owner, priority))
            conn.execute(
                "DELETE FROM large_catalog_imports WHERE large_package_id=? AND request_key=? "
                "AND scan_id NOT IN (SELECT id FROM scans)",
                (package_id, request_key))
            conn.execute(
                "INSERT INTO large_catalog_imports(id,large_package_id,scan_id,job_id,owner_id,request_key,"
                "request_payload,selection_snapshot,selected_file_count,selected_bytes,status,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (bridge_id, package_id, scan_id, job_id, owner, request_key,
                 json.dumps(request_payload or {}, ensure_ascii=False),
                 json.dumps(snapshot, ensure_ascii=False), len(records), total_bytes,
                 "queued", now, now))
            conn.commit()
        except Exception:                             # noqa: BLE001
            conn.rollback()
            # Constraint/storage errors are not proof of a valid duplicate.
            # Reuse was checked atomically above; unexpected failures stay errors.
            raise

    LOGGER.info('large catalog import package=%s scan=%s job=%s files=%d bytes=%d',
                package_id, scan_id, job_id, len(records), total_bytes)
    return {"scan_id": scan_id, "job_id": job_id, "selected_file_count": len(records),
            "selected_bytes": total_bytes, "reused": False}
