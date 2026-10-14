from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import math
import os
import secrets
import sqlite3
import time
import uuid
from pathlib import Path


def uid():
    return uuid.uuid4().hex


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


class ProtectionError(Exception):
    def __init__(self, message, status=400, code="protection_error"):
        super().__init__(message)
        self.status, self.code = status, code


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS download_proofs (
 token_hash TEXT PRIMARY KEY,owner TEXT NOT NULL,device TEXT NOT NULL,
 filename TEXT NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS risk_reviews (
 file_id TEXT PRIMARY KEY,owner TEXT NOT NULL,sha256 TEXT NOT NULL,reviewer TEXT NOT NULL,
 reason TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS enrollments (
 token_hash TEXT PRIMARY KEY,owner TEXT NOT NULL,user_id TEXT NOT NULL,
 expires REAL NOT NULL,used REAL);
CREATE TABLE IF NOT EXISTS devices (
 id TEXT PRIMARY KEY,owner TEXT NOT NULL,user_id TEXT NOT NULL,name TEXT NOT NULL,
 public_key TEXT NOT NULL UNIQUE,created REAL NOT NULL,revoked REAL,
 heartbeat REAL NOT NULL DEFAULT 0,health TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS nonces (
 device TEXT NOT NULL,nonce TEXT NOT NULL,expires REAL NOT NULL,
 PRIMARY KEY(device,nonce));
CREATE TABLE IF NOT EXISTS collections (
 id TEXT PRIMARY KEY,owner TEXT NOT NULL,root TEXT NOT NULL,state TEXT NOT NULL,
 created REAL NOT NULL,updated REAL NOT NULL,error TEXT NOT NULL DEFAULT '',
 file_count INTEGER NOT NULL DEFAULT 0,worker_pid INTEGER);
CREATE INDEX IF NOT EXISTS collections_owner ON collections(owner,created);
CREATE TABLE IF NOT EXISTS files (
 id TEXT PRIMARY KEY,collection TEXT NOT NULL,owner TEXT NOT NULL,
 path TEXT NOT NULL,relative_path TEXT NOT NULL,size INTEGER NOT NULL,
 sha256 TEXT NOT NULL,mtime_ns INTEGER NOT NULL,identity TEXT NOT NULL,
 risk TEXT NOT NULL DEFAULT 'unchecked',findings TEXT NOT NULL DEFAULT '[]',
 scan_status TEXT NOT NULL DEFAULT '{}',updated REAL NOT NULL,
 UNIQUE(collection,path));
CREATE INDEX IF NOT EXISTS files_hash ON files(owner,sha256);
CREATE TABLE IF NOT EXISTS bindings (
 id TEXT PRIMARY KEY,file_id TEXT NOT NULL,device TEXT NOT NULL,owner TEXT NOT NULL,
 local_path TEXT NOT NULL,local_identity TEXT NOT NULL,sha256 TEXT NOT NULL,
 state TEXT NOT NULL,updated REAL NOT NULL,
 UNIQUE(device,file_id,local_path));
CREATE TABLE IF NOT EXISTS events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT NOT NULL UNIQUE,
 owner TEXT NOT NULL,device TEXT NOT NULL,file_id TEXT NOT NULL,instance_id TEXT NOT NULL,
 action TEXT NOT NULL,occurred REAL NOT NULL,received REAL NOT NULL,
 payload TEXT NOT NULL,previous_hash TEXT NOT NULL,record_hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_owner ON events(owner,seq);
CREATE INDEX IF NOT EXISTS events_file ON events(owner,file_id,seq);
CREATE TABLE IF NOT EXISTS actions (
 id TEXT PRIMARY KEY,owner TEXT NOT NULL,device TEXT NOT NULL,binding TEXT NOT NULL,
 kind TEXT NOT NULL,expected_sha256 TEXT NOT NULL,reason TEXT NOT NULL,
 state TEXT NOT NULL,created REAL NOT NULL,result TEXT NOT NULL DEFAULT '{}',
 restore_action_id TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS binding_clocks (
 binding TEXT PRIMARY KEY,occurred REAL NOT NULL);
CREATE INDEX IF NOT EXISTS actions_binding_version ON actions(binding,expected_sha256,state);
CREATE TABLE IF NOT EXISTS action_targets (
 action_id TEXT PRIMARY KEY,target TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS device_sequences (
 device TEXT PRIMARY KEY,sequence INTEGER NOT NULL);
"""


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "protection.db"
        with self.db() as db:
            db.executescript(SCHEMA)
        if os.name != "nt":
            os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def db(self, write=False):
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def enrollment(self, owner, user_id):
        token = secrets.token_urlsafe(32)
        with self.db(True) as db:
            db.execute("INSERT INTO enrollments VALUES(?,?,?,?,NULL)",
                       (digest(token), owner, user_id, time.time() + 600))
        return token

    def enroll(self, token, public_key, name):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
        except Exception:
            raise ProtectionError("设备公钥无效")
        with self.db(True) as db:
            row = db.execute("SELECT * FROM enrollments WHERE token_hash=?", (digest(token),)).fetchone()
            if not row or row["used"] or row["expires"] < time.time():
                raise ProtectionError("绑定码无效、已使用或已过期", 401)
            device = uid()
            try:
                db.execute("INSERT INTO devices(id,owner,user_id,name,public_key,created) VALUES(?,?,?,?,?,?)",
                           (device, row["owner"], row["user_id"], str(name)[:128], public_key, time.time()))
            except sqlite3.IntegrityError:
                raise ProtectionError("该设备密钥已绑定")
            db.execute("UPDATE enrollments SET used=? WHERE token_hash=?", (time.time(), digest(token)))
            self._event(db, row["owner"], "device.enrolled", device=device,
                        payload={"name": str(name)[:128]})
        return device

    def verify(self, headers, message_prefix, owner=None, require_healthy=False):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        device = str(headers.get("x-sjfx-device", ""))
        stamp = str(headers.get("x-sjfx-time", ""))
        nonce = str(headers.get("x-sjfx-nonce", ""))
        signature = str(headers.get("x-sjfx-proof", ""))
        try:
            if not math.isfinite(float(stamp)) or abs(time.time() - float(stamp)) > 60 or not 16 <= len(nonce) <= 128:
                raise ValueError()
            with self.db(True) as db:
                row = db.execute("SELECT * FROM devices WHERE id=? AND revoked IS NULL", (device,)).fetchone()
                if not row or (owner is not None and row["owner"] != owner):
                    raise ValueError()
                message = "\n".join([message_prefix, device, stamp, nonce]).encode()
                Ed25519PublicKey.from_public_bytes(base64.b64decode(row["public_key"])).verify(
                    base64.b64decode(signature, validate=True), message)
                db.execute("DELETE FROM nonces WHERE expires<?", (time.time(),))
                db.execute("INSERT INTO nonces VALUES(?,?,?)", (device, nonce, time.time() + 120))
                result = dict(row)
        except Exception:
            raise ProtectionError("需要当前电脑的助手验证，凭据无效或已重放", 403, "device_proof_required")
        if require_healthy:
            health = json.loads(result["health"])
            if time.time() - result["heartbeat"] > 60 or health.get("healthy") is not True:
                raise ProtectionError("本地助手离线或监视异常", 423, "device_unhealthy")
        return result

    def heartbeat(self, device, health):
        if not isinstance(health, dict) or not isinstance(health.get('healthy'), bool):
            raise ProtectionError('设备健康状态必须明确为 true 或 false')
        def lost_count(value, fallback=0):
            if value is None:return fallback
            if type(value) is not int or value<0:
                raise ProtectionError('lost_events 必须为非负整数或 null')
            return value
        allowed = {k: health.get(k) for k in ("healthy", "watch_roots", "queue_depth", "errors",
                   "lost_events", "agent_version", "capabilities", "binding_count", "last_error")}
        with self.db(True) as db:
            previous=db.execute('SELECT * FROM devices WHERE id=? AND revoked IS NULL',(device,)).fetchone()
            if not previous:raise ProtectionError('设备已撤销',403,'device_revoked')
            old_health=json.loads(previous['health'])
            previous_lost=lost_count(old_health.get('lost_events'))
            # Missing/null means unreported; retain the last valid measurement.
            current_lost=lost_count(health.get('lost_events'),previous_lost)
            allowed['lost_events']=current_lost
            if (previous['heartbeat'] and time.time()-previous['heartbeat']>60) or (old_health.get('healthy') is True and health.get('healthy') is False) or current_lost>previous_lost:
                self._package_gap(db,previous['owner'],device,None,'助手离线、监控失效或事件丢失')
                db.execute("UPDATE bindings SET state='unverified' WHERE device=? AND state NOT IN ('missing','quarantined')",(device,))
                db.execute("UPDATE actions SET state='stale' WHERE device=? AND kind!='restore' AND state IN ('pending','running')",(device,))
            db.execute("UPDATE devices SET heartbeat=?,health=? WHERE id=? AND revoked IS NULL",
                       (time.time(), encode(allowed), device))

    def bind_download(self, owner, device, filename, token, ttl):
        with self.db(True) as db:
            db.execute('DELETE FROM download_proofs WHERE expires<?', (time.time(),))
            db.execute('INSERT INTO download_proofs VALUES(?,?,?,?,?)',
                       (digest(token), owner, device, filename, time.time()+min(ttl,60)))

    def consume_download(self, owner, filename, token):
        with self.db(True) as db:
            row=db.execute('SELECT p.*,d.heartbeat,d.health,d.revoked FROM download_proofs p '
                           'JOIN devices d ON d.id=p.device WHERE p.token_hash=? AND p.owner=? '
                           'AND p.filename=? AND p.expires>?', (digest(token),owner,filename,time.time())).fetchone()
            if not row or row['revoked'] or time.time()-row['heartbeat']>60 or json.loads(row['health']).get('healthy') is not True:
                raise ProtectionError('请通过已连接助手的工作台重新申请下载',423,'device_proof_required')
            db.execute('DELETE FROM download_proofs WHERE token_hash=?',(digest(token),))
            return row['device']

    def _event(self, db, owner, action, *, device="", file_id="", instance_id="", payload=None,
               event_id=None, occurred=None):
        event_id = event_id or uid()
        if db.execute("SELECT 1 FROM events WHERE event_id=?", (event_id,)).fetchone():
            return False
        previous = db.execute("SELECT record_hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        previous = previous[0] if previous else "0" * 64
        now = time.time()
        record = {"event_id": event_id, "owner": owner, "device": device, "file_id": file_id,
                  "instance_id": instance_id, "action": action, "occurred": occurred or now,
                  "received": now, "payload": payload or {}}
        raw = encode(record)
        db.execute("INSERT INTO events(event_id,owner,device,file_id,instance_id,action,occurred,received,"
                   "payload,previous_hash,record_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (event_id, owner, device, file_id, instance_id, action, record["occurred"], now,
                    raw, previous, digest(previous + raw)))
        return True

    def event(self, owner, action, **kwargs):
        with self.db(True) as db:
            return self._event(db, owner, action, **kwargs)

    def create_collection(self, owner, root):
        collection = uid()
        now = time.time()
        with self.db(True) as db:
            db.execute("INSERT INTO collections(id,owner,root,state,created,updated) VALUES(?,?,?,?,?,?)",
                       (collection, owner, root, "queued", now, now))
            self._event(db, owner, "collection.preparing", payload={"collection_id": collection, "root": root})
        return collection

    def collection(self, owner, collection):
        with self.db() as db:
            row = db.execute("SELECT * FROM collections WHERE id=? AND owner=?", (collection, owner)).fetchone()
        if not row:
            raise ProtectionError("资料清单不存在", 404)
        return dict(row)

    def list_rows(self, table, owner, *, limit=200, offset=0, collection=None, device=None, file_id=None):
        if table not in {"devices", "collections", "files", "bindings", "events", "actions"}:
            raise ValueError("unknown table")
        fields = "*" if table != "devices" else "id,name,created,revoked,heartbeat,health"
        where, params = ["owner=?"], [owner]
        for key, value in (("collection", collection), ("device", device), ("file_id", file_id)):
            if value:
                if table=='events' and key=='file_id':
                    # Associate history by the recorded logical identity, never
                    # by today's manifest or a shared content hash.
                    where.append("(file_id=? OR (file_id='' AND json_extract(payload,'$.payload.collection_id') IN "
                                 "(SELECT collection FROM files WHERE id=? AND owner=?)) OR "
                                 "(json_type(payload,'$.payload.targets')='array' AND EXISTS "
                                 "(SELECT 1 FROM json_each(events.payload,'$.payload.targets') target "
                                 "WHERE target.type='object' AND json_extract(target.value,'$.file_id')=?)) OR "
                                 "(json_type(payload,'$.payload.context.versions')='array' AND EXISTS "
                                 "(SELECT 1 FROM json_each(events.payload,'$.payload.context.versions') version "
                                 "WHERE version.type='object' AND json_extract(version.value,'$.file_id')=?)))")
                    params.extend([value,value,owner,value,value])
                    continue
                where.append(key + "=?")
                params.append(value)
        order = "seq DESC" if table == "events" else "rowid DESC"
        with self.db() as db:
            rows = db.execute("SELECT " + fields + " FROM " + table + " WHERE " + " AND ".join(where)
                              + " ORDER BY " + order + " LIMIT ? OFFSET ?",
                              params + [min(max(int(limit), 1), 500), max(int(offset), 0)]).fetchall()
        return [dict(row) for row in rows]

    def bind(self, device, records):
        from .validation import local_path, identity, sha256
        if not isinstance(records, list) or len(records) > 500:
            raise ProtectionError("每批最多绑定 500 个文件")
        out = []
        with self.db(True) as db:
            for rec in records:
                if not isinstance(rec, dict):
                    raise ProtectionError('文件绑定记录必须是对象')
                if not isinstance(rec.get('file_id'), str):
                    raise ProtectionError('文件编号无效')
                file = db.execute("SELECT * FROM files WHERE id=? AND owner=?",
                                  (rec.get("file_id"), device["owner"])).fetchone()
                if not file or sha256(rec.get('sha256')) != file["sha256"]:
                    raise ProtectionError("文件不存在或内容与服务器版本不一致", 409)
                path, file_identity = local_path(rec.get('local_path')), identity(rec.get('local_identity'))
                existing = db.execute("SELECT id FROM bindings WHERE device=? AND file_id=? AND local_path=?",
                                      (device["id"], file["id"], path)).fetchone()
                bid = existing[0] if existing else uid()
                db.execute("INSERT INTO bindings VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(device,file_id,local_path) "
                           "DO UPDATE SET local_identity=excluded.local_identity,sha256=excluded.sha256,"
                           "state='watching',updated=excluded.updated",
                           (bid, file["id"], device["id"], device["owner"], path, file_identity, file["sha256"],
                            "watching", time.time()))
                self._event(db, device["owner"], "file.bound", device=device["id"], file_id=file["id"],
                            instance_id=bid, payload={"local_path": path, "identity": file_identity,
                                                     "sha256": file["sha256"], "basis": "content_equal"})
                out.append({"binding_id": bid, "file_id": file["id"], "local_path": path})
                self._queue_known_threat(db, bid)
        return out

    def _queue_known_threat(self, db, binding_id):
        from .policy import read_policy
        policy=read_policy(self.directory)
        legacy=db.execute("SELECT value FROM settings WHERE key='mode'").fetchone()
        if not policy['enabled'] or policy.get('invalid') or policy['operation_policy'].get('observation_only',True) or not policy['operation_policy'].get('automatic_quarantine') or (legacy and legacy[0] == 'audit'):
            return
        binding = db.execute('SELECT * FROM bindings WHERE id=?', (binding_id,)).fetchone()
        if not binding or binding['state'] in {'missing', 'unavailable', 'unverified'}:
            return
        known = db.execute(
            "SELECT id FROM files WHERE owner=? AND sha256=? AND risk='dangerous' LIMIT 1",
            (binding['owner'], binding['sha256'])).fetchone()
        if not known or db.execute(
                "SELECT 1 FROM actions WHERE binding=? AND expected_sha256=? AND kind='quarantine' AND state IN ('pending','running')",
                (binding_id, binding['sha256'])).fetchone():
            return
        # A completed quarantine protects the old instance, not a newly reappearing copy.
        if binding['state'] in {'quarantined'}:
            return
        aid = uid()
        db.execute('INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (aid, binding['owner'], binding['device'], binding_id, 'quarantine', binding['sha256'],
                    '自动处置：当前文件内容命中同账号已确认的危险文件', 'pending', time.time(), '{}', ''))
        self._freeze_action(db,aid,binding)
        self._event(db, binding['owner'], 'action.requested', device=binding['device'],
                    file_id=binding['file_id'], instance_id=binding_id,
                    payload={'action_id': aid, 'kind': 'quarantine', 'automatic': True,
                             'sha256': binding['sha256'], 'evidence_file_id': known['id']})

    def related_copy(self, device, data):
        from .validation import local_path, identity, sha256
        with self.db(True) as db:
            source=db.execute('SELECT * FROM bindings WHERE id=? AND device=? AND owner=?',
                              (data.get('source_binding'),device['id'],device['owner'])).fetchone()
            if not source or sha256(data.get('sha256'))!=source['sha256']:
                raise ProtectionError('对应文件版本已经变化，请重新核对',409)
            path = local_path(data.get('local_path'))
            file_identity = identity(data.get('local_identity'))
            existing=db.execute('SELECT id FROM bindings WHERE device=? AND file_id=? AND local_path=?',
                                (device['id'],source['file_id'],path)).fetchone()
            if existing:
                current = db.execute('SELECT * FROM bindings WHERE id=?', (existing[0],)).fetchone()
                if current['sha256'] != source['sha256'] or current['local_identity'] != file_identity:
                    raise ProtectionError('目标路径关联的文件版本不同，请重新关联', 409)
                self._queue_known_threat(db, existing[0])
                return {'binding_id':existing[0]}
            bid=uid()
            db.execute('INSERT INTO bindings VALUES(?,?,?,?,?,?,?,?,?)',(bid,source['file_id'],device['id'],device['owner'],
                       path,file_identity,source['sha256'],'watching',time.time()))
            self._event(db,device['owner'],'file.identical_instance',device=device['id'],file_id=source['file_id'],instance_id=bid,
                        payload={'source_binding':source['id'],'local_path':path,'identity':file_identity,'sha256':source['sha256'],
                                 'basis':'content_equal_not_proven_copy'})
            self._queue_known_threat(db, bid)
            return {'binding_id':bid}

    def ingest(self, device, events):
        from .validation import local_path, identity as validate_identity, sha256, timestamp
        if not isinstance(events, list) or len(events) > 500:
            raise ProtectionError("每批最多 500 条事件")
        accepted = []
        with self.db(True) as db:
            for item in events:
                if not isinstance(item, dict):
                    raise ProtectionError('事件必须是对象')
                item = dict(item)
                event_id = str(item.get("event_id", ""))
                if not 8 <= len(event_id) <= 100:
                    raise ProtectionError("事件编号无效")
                bid = str(item.get("binding_id", ""))
                binding = db.execute("SELECT * FROM bindings WHERE id=? AND device=? AND owner=?",
                                     (bid, device["id"], device["owner"])).fetchone() if bid else None
                action = str(item.get("action", ""))
                if not action or len(action) > 100 or not action.startswith(('file.', 'monitor.')):
                    raise ProtectionError('本地事件类型无效')
                if action == 'monitor.directory_change_observed':
                    # A Word lock file can be observed under a selected root without
                    # being an admitted member. Validate scope before accepting the
                    # metadata; it is never content/version evidence for a file.
                    from .registration import _within_local
                    if bid or not self._has_table(db, 'registration_devices') or not self._has_table(db, 'registrations'):
                        raise ProtectionError('目录观察缺少已登记范围',403,'directory_observation_scope_invalid')
                    reg_id=item.get('registration_id')
                    if not isinstance(reg_id,str) or not 1<=len(reg_id)<=100:
                        raise ProtectionError('目录观察缺少登记身份',403,'directory_observation_scope_invalid')
                    scope=db.execute('SELECT rd.local_root FROM registration_devices rd JOIN registrations r ON r.id=rd.registration_id '
                        'WHERE rd.registration_id=? AND rd.device_id=? AND r.owner=?',
                        (reg_id,device['id'],device['owner'])).fetchone()
                    observed=local_path(item.get('path'))
                    claimed_root=local_path(item.get('local_root'))
                    if (not scope or not _within_local(observed,scope['local_root']) or
                            not _within_local(claimed_root,scope['local_root']) or not _within_local(scope['local_root'],claimed_root) or
                            item.get('device_id',device['id'])!=device['id'] or item.get('owner_id',device['owner'])!=device['owner'] or
                            any(item.get(k) for k in ('identity','sha256','version_id')) or
                            isinstance(item.get('gap_epoch'),bool) or not isinstance(item.get('gap_epoch'),int) or item['gap_epoch']<0):
                        raise ProtectionError('目录观察超出已登记范围或冒充内容版本',403,'directory_observation_scope_invalid')
                elif binding is None and action not in {"monitor.gap", "monitor.health", "monitor.error"}:
                    raise ProtectionError("事件不属于已关联文件", 403)
                if len(encode(item)) > 16000:
                    raise ProtectionError("单条事件过大")
                occurred = timestamp(item.get('occurred', time.time()), time.time())
                clock = db.execute('SELECT occurred FROM binding_clocks WHERE binding=?', (bid,)).fetchone() if bid else None
                late = bool(clock and occurred < clock[0])
                sequence=item.get('sequence')
                if sequence is not None:
                    if isinstance(sequence,bool) or not isinstance(sequence,int) or sequence<1:raise ProtectionError('设备事件序号无效')
                    last_seq=db.execute('SELECT sequence FROM device_sequences WHERE device=?',(device['id'],)).fetchone()
                    late=late or bool(last_seq and sequence<=last_seq[0])
                if late:
                    item['out_of_order'] = True
                for key in ('path', 'old_path'):
                    if item.get(key):
                        item[key] = local_path(item[key])
                if item.get('sha256'):
                    item['sha256'] = sha256(item['sha256'])
                if item.get('identity'):
                    item['identity'] = validate_identity(item['identity'])
                if binding and self._has_table(db,'file_instances'):
                    instance=db.execute('SELECT registration_id,version_id FROM file_instances WHERE id=?',(bid,)).fetchone()
                    if instance:
                        item.setdefault('registration_id',instance['registration_id'])
                        item.setdefault('version_id',item.get('sha256') or instance['version_id'])
                        item.setdefault('version_basis','observed_content_hash' if item.get('sha256') else 'binding_at_collection_not_accessed_bytes')
                inserted = self._event(db, device["owner"], action[:100], device=device["id"],
                    file_id=binding["file_id"] if binding else "", instance_id=bid,
                    event_id=device["id"] + ":" + event_id,
                    occurred=occurred, payload=item)
                if inserted and not late and sequence is not None:
                    db.execute('INSERT OR REPLACE INTO device_sequences VALUES(?,?)',(device['id'],sequence))
                if inserted and not late and action == 'monitor.gap':
                    db.execute("UPDATE bindings SET state='unverified',updated=? WHERE device=? AND owner=? AND state!='missing'",
                               (time.time(), device['id'], device['owner']))
                    db.execute("UPDATE actions SET state='stale',result=? WHERE device=? AND owner=? AND kind!='restore' AND state='pending'",
                               (encode({'status': 'stale', 'detail': '监控存在缺口，必须重新核验'}), device['id'], device['owner']))
                    self._package_gap(db,device['owner'],device['id'],item.get('registration_id'),item.get('detail','监控事件缺口'))
                if inserted and not late and binding and action in {'file.modified','file.replaced','file.renamed','file.deleted',
                                                       'file.reappeared','file.unavailable','file.protected','file.restored'}:
                    state = binding['state'] if action == 'file.renamed' else 'watching'
                    if action == 'file.deleted':
                        state = 'missing'
                    if action in {"file.unavailable", "monitor.gap"}:
                        state = "unavailable"
                    if action == 'file.protected':
                        # An observation is not a successful command receipt. action_result
                        # is the only command path that promotes an instance to protected.
                        state = binding['state']
                    path = str(item.get("path") or binding["local_path"])
                    sha = str(item.get("sha256") or binding["sha256"])
                    identity = str(item.get("identity") or binding["local_identity"])
                    if action in {'file.modified', 'file.replaced', 'file.reappeared', 'file.restored','file.renamed'}:
                        if not item.get('sha256') or not item.get('identity'):
                            state = 'unverified'
                        else:
                            original = db.execute('SELECT sha256 FROM files WHERE id=?', (binding['file_id'],)).fetchone()
                            if not original or sha != original[0]:
                                state = 'changed'
                    # Ordinary notifications never close a monitoring gap or restore lease proof.
                    if binding['state'] in {'unverified','unavailable'} and state not in {'missing','changed'}:
                        state='unverified'
                    if action=='file.restored':state='unverified'
                    collision = db.execute('SELECT id FROM bindings WHERE device=? AND file_id=? AND local_path=? AND id!=?',
                                           (device['id'], binding['file_id'], path, bid)).fetchone()
                    if collision:
                        path, state = binding['local_path'], 'unverified'
                        self._event(db, device['owner'], 'monitor.path_conflict', device=device['id'],
                                    file_id=binding['file_id'], instance_id=bid,
                                    payload={'reported_path': item.get('path'), 'conflicting_binding': collision[0]})
                    item.setdefault('version_id',sha)
                    db.execute("UPDATE bindings SET local_path=?,local_identity=?,sha256=?,state=?,updated=? WHERE id=?",
                               (path, identity, sha, state, time.time(), bid))
                    db.execute('INSERT OR REPLACE INTO binding_clocks VALUES(?,?)', (bid, occurred))
                    self._package_instance_event(db,device,binding,path,identity,sha,state,item)
                    self._package_anomaly(db,device,binding,item)
                    if sha != binding['sha256'] or identity != binding['local_identity'] or state in {'missing', 'unavailable', 'unverified'}:
                        db.execute("UPDATE actions SET state='stale',result=? WHERE binding=? AND state='pending'",
                                   (encode({'status': 'stale', 'detail': '文件版本或可用状态已变化，请重新核验'}), bid))
                    self._queue_known_threat(db, bid)
                accepted.append(event_id)
        return accepted

    def require_root(self, owner, device, root):
        # Match a finalized inventory, not a browser-supplied file count.
        root = os.path.abspath(os.path.expanduser(root))
        with self.db() as db:
            collection = db.execute("SELECT * FROM collections WHERE owner=? AND root=? "
                                    "ORDER BY created DESC LIMIT 1", (owner, root)).fetchone()
            if not collection or collection['state'] != 'ready':
                raise ProtectionError("请先在文件保护页登记该服务器目录并完成本地关联", 423, "binding_required")
            missing = db.execute("SELECT f.id FROM files f WHERE f.collection=? AND NOT EXISTS "
                "(SELECT 1 FROM bindings b WHERE b.file_id=f.id AND b.device=? AND b.sha256=f.sha256 AND b.state IN ('watching','protected')) LIMIT 1",
                (collection["id"], device)).fetchone()
            if missing or not collection["file_count"]:
                raise ProtectionError("当前设备尚未监视该批资料中的全部文件", 423, "binding_incomplete")
        return collection["id"]

    def blocked_file(self, collection):
        with self.db() as db:
            return db.execute("SELECT f.id FROM files f WHERE f.collection=? AND (f.risk IN ('dangerous','unchecked') OR "
                "(f.risk='review' AND NOT EXISTS (SELECT 1 FROM risk_reviews r WHERE r.file_id=f.id AND r.sha256=f.sha256))) LIMIT 1",
                (collection,)).fetchone()

    def approve_review(self, owner, user_id, file_id, sha256, reason):
        if not reason.strip():raise ProtectionError('请填写复核依据')
        with self.db(True) as db:
            row=db.execute('SELECT * FROM files WHERE id=? AND owner=?',(file_id,owner)).fetchone()
            if not row or row['risk']!='review' or row['sha256']!=sha256:
                raise ProtectionError('仅可复核当前版本的可疑文件；已确认危险的文件不允许放行',409)
            coverage=json.loads(row['scan_status']).get('coverage')
            if not coverage or coverage.get('complete') is not True:
                raise ProtectionError('必要检查覆盖不完整，人工复核不能批准未知状态',423,'inspection_incomplete')
            db.execute('INSERT OR REPLACE INTO risk_reviews VALUES(?,?,?,?,?,?)',(file_id,owner,sha256,user_id,reason[:2000],time.time()))
            self._event(db,owner,'risk.review_approved',file_id=file_id,payload={'sha256':sha256,'reviewer':user_id,'reason':reason[:2000]})

    def command(self, owner, binding_id, kind, reason, restore_action_id=""):
        if kind not in {"quarantine", "deny_read", "protect_delete", "restore"}:
            raise ProtectionError("不支持该处置；不会执行整机 USB、网络或任意命令")
        if not reason.strip():
            raise ProtectionError("必须填写处置原因")
        with self.db(True) as db:
            binding = db.execute("SELECT * FROM bindings WHERE id=? AND owner=?", (binding_id, owner)).fetchone()
            if not binding:
                raise ProtectionError("文件关联不存在", 404)
            if kind!='restore' and binding['state'] in {'missing', 'unavailable', 'unverified'}:
                raise ProtectionError('文件当前状态未核验，不能执行处置；请先重新关联', 409, 'binding_unverified')
            if self._has_table(db,'file_instances'):
                instance=db.execute('SELECT * FROM file_instances WHERE id=?',(binding_id,)).fetchone()
                if instance:
                    rd=db.execute('SELECT * FROM registration_devices WHERE registration_id=? AND device_id=?',(instance['registration_id'],binding['device'])).fetchone()
                    if not rd or (kind!='restore' and (rd['gap_epoch']!=rd['verified_epoch'] or rd['state']!='verified')):
                        raise ProtectionError('登记实例仍有核验或监控缺口',423,'monitoring_gap')
            active_device = db.execute('SELECT id FROM devices WHERE id=? AND owner=? AND revoked IS NULL',
                                       (binding['device'], owner)).fetchone()
            if not active_device:
                raise ProtectionError('设备已撤销，不能下发处置', 409, 'device_revoked')
            if kind == "restore":
                previous = db.execute("SELECT * FROM actions WHERE id=? AND binding=? AND state='completed'",
                                      (restore_action_id, binding_id)).fetchone()
                if not previous or previous["kind"] == "restore":
                    raise ProtectionError("不存在可撤销的成功处置")
                original=db.execute('SELECT target FROM action_targets WHERE action_id=?',(restore_action_id,)).fetchone()
                original=json.loads(original[0]) if original else None
                expected_state={'quarantine':'quarantined','deny_read':'read_locked','protect_delete':'delete_locked'}.get(previous['kind'])
                if not original or (binding['state'] not in {expected_state,'watching','unverified','unavailable'} and not (previous['kind']=='protect_delete' and binding['state']=='changed')) or any(original[key]!=binding[column] for key,column in
                        [('binding_id','id'),('device_id','device'),('local_identity','local_identity')]):
                    raise ProtectionError('恢复目标不是原成功处置的同一实例',409,'stale_action')
                if previous['kind']!='quarantine' and original['local_path']!=binding['local_path']:
                    raise ProtectionError('恢复目标位置与原锁定实例不一致',409,'stale_action')
                if previous['expected_sha256'] != binding['sha256'] and previous['kind']!='protect_delete':
                    raise ProtectionError('原处置对应的文件版本已变化，不能恢复',409,'stale_action')
                if db.execute("SELECT 1 FROM files WHERE owner=? AND sha256=? AND risk='dangerous' LIMIT 1",
                              (owner, binding['sha256'])).fetchone():
                    raise ProtectionError('已确认危险的文件不允许直接恢复；请先由安全管理员完成离线复核',
                                          423, 'dangerous_restore_denied')
            pending = db.execute("SELECT id FROM actions WHERE binding=? AND kind=? AND expected_sha256=? AND restore_action_id=? AND state IN ('pending','running')",
                                 (binding_id, kind, binding['sha256'], restore_action_id)).fetchone()
            if pending:
                return pending[0]
            aid = uid()
            db.execute("INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (aid, owner, binding["device"], binding_id, kind, binding["sha256"], reason[:1000],
                        "pending", time.time(), "{}", restore_action_id))
            self._freeze_action(db,aid,binding)
            self._event(db, owner, "action.requested", device=binding["device"], file_id=binding["file_id"],
                        instance_id=binding_id, payload={"action_id": aid, "kind": kind, "reason": reason[:1000]})
        return aid

    def pending_actions(self, device):
        with self.db(True) as db:
            rows = db.execute("SELECT a.* FROM actions a "
                              "WHERE a.device=? AND a.state IN ('pending','running') ORDER BY a.created LIMIT 100",
                              (device,)).fetchall()
            result=[]
            for row in rows:
                frozen=db.execute('SELECT target FROM action_targets WHERE action_id=?',(row['id'],)).fetchone()
                target=json.loads(frozen[0]) if frozen else None
                if not target or not self._target_current(db,row,target):
                    db.execute("UPDATE actions SET state='stale',result=? WHERE id=?",(encode({'status':'stale','detail':'处置完整目标已失效，需要重新核验'}),row['id']))
                    continue
                db.execute("UPDATE actions SET state='running' WHERE id=? AND state='pending'",(row['id'],))
                result.append({**dict(row),**target,'state':'running'})
        return result

    def action_result(self, device, aid, result):
        if not isinstance(result, dict) or len(encode(result)) > 16000:
            raise ProtectionError('处置回执必须是有效对象且不能超过大小限制')
        if result.get("status") not in {"completed", "failed", "unsupported", "stale"}:
            raise ProtectionError("处置结果无效")
        with self.db(True) as db:
            row = db.execute("SELECT * FROM actions WHERE id=? AND device=?", (aid, device["id"])).fetchone()
            if not row:
                raise ProtectionError("处置不存在", 404)
            if row["state"] not in {"pending","running"}:
                return
            current_binding = db.execute('SELECT * FROM bindings WHERE id=? AND owner=?',
                                         (row['binding'], device['owner'])).fetchone()
            frozen=db.execute('SELECT target FROM action_targets WHERE action_id=?',(aid,)).fetchone()
            target=json.loads(frozen[0]) if frozen else None
            if not current_binding or not target or not self._target_current(db,row,target):
                result = {'status': 'stale', 'detail': '文件版本已变化，不接受旧版本成功回执'}
            actual=None
            if result['status']=='completed' and row['kind'] in {'quarantine','restore'}:
                from .validation import local_path,identity,sha256
                from .registration import _within_local
                import ntpath
                from pathlib import PureWindowsPath,PurePosixPath
                actual=result.get('actual')
                try:
                    if not isinstance(actual,dict) or actual.get('action_id')!=aid:raise ValueError('missing actual receipt')
                    actual_path=local_path(actual.get('local_path'));actual_identity=identity(actual.get('local_identity'))
                    if sha256(actual.get('sha256'))!=target['expected_sha256'] or actual_identity!=target['local_identity']:
                        raise ValueError('actual instance does not match frozen target')
                    if row['kind']=='quarantine':
                        path_type=PureWindowsPath if ntpath.isabs(target['local_path']) else PurePosixPath
                        quarantine_root=str(path_type(target['local_path']).parent/'.sjfx-quarantine')
                        if not _within_local(actual_path,quarantine_root):raise ValueError('quarantine destination is outside controlled directory')
                    else:
                        original=db.execute('SELECT target FROM action_targets WHERE action_id=?',(row['restore_action_id'],)).fetchone()
                        original_path=json.loads(original[0])['local_path'] if original else ''
                        if local_path(original_path)!=actual_path:raise ValueError('restore destination differs from original successful action')
                    actual={**actual,'local_path':actual_path,'local_identity':actual_identity}
                except (ProtectionError,ValueError,TypeError,KeyError):
                    actual=None
                    result={'status':'failed','detail':'成功回执缺少匹配的实际实例、内容版本或受控目标位置；不会猜测处置已完成','code':'action_receipt_incomplete'}
            db.execute("UPDATE actions SET state=?,result=? WHERE id=?",
                       ('failed' if result['status']=='unsupported' else result["status"], encode(result)[:16000], aid))
            if row['kind']=='restore' and result['status']=='completed':
                db.execute("UPDATE actions SET state='restored' WHERE id=? AND binding=?",
                           (row['restore_action_id'],row['binding']))
            if result['status'] == 'completed':
                if actual:
                    db.execute('UPDATE bindings SET local_path=?,local_identity=?,sha256=?,updated=? WHERE id=?',
                        (actual['local_path'],actual['local_identity'],actual['sha256'],time.time(),row['binding']))
                    if self._has_table(db,'file_instances'):
                        db.execute('UPDATE file_instances SET path=?,identity=?,updated=? WHERE id=?',
                            (actual['local_path'],actual['local_identity'],time.time(),row['binding']))
                db.execute('UPDATE bindings SET state=?,updated=? WHERE id=?',
                           ({'restore':'unverified','quarantine':'quarantined','deny_read':'read_locked','protect_delete':'delete_locked'}[row['kind']], time.time(), row['binding']))
                if self._has_table(db,'file_instances'):
                    db.execute('UPDATE file_instances SET state=?,updated=? WHERE id=?',
                        ({'restore':'unverified','quarantine':'quarantined','deny_read':'read_locked','protect_delete':'delete_locked'}[row['kind']],time.time(),row['binding']))
            binding = db.execute("SELECT file_id FROM bindings WHERE id=?", (row["binding"],)).fetchone()
            self._event(db, device["owner"], "action." + result["status"], device=device["id"],
                        file_id=binding[0], instance_id=row["binding"],
                        payload={"action_id": aid, "kind": row["kind"], "result": result})

    @staticmethod
    def _has_table(db,name):
        return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone())

    def _freeze_action(self,db,aid,binding):
        from .policy import read_policy
        target={'binding_id':binding['id'],'file_id':binding['file_id'],'device_id':binding['device'],
            'local_path':binding['local_path'],'local_identity':binding['local_identity'],
            'expected_sha256':binding['sha256'],'expected_version_id':binding['sha256'],
            'registration_id':'','registration_revision':0,'manifest_generation':0,'gap_epoch':0,
            'expires_at':time.time()+600,'policy_revision':read_policy(self.directory)['revision']}
        if self._has_table(db,'file_instances'):
            instance=db.execute('SELECT i.*,r.manifest_revision FROM file_instances i JOIN registrations r ON r.id=i.registration_id WHERE i.id=?',(binding['id'],)).fetchone()
            if instance:
                target.update(registration_id=instance['registration_id'],registration_revision=instance['manifest_revision'],expected_version_id=instance['version_id'])
                rd=db.execute('SELECT * FROM registration_devices WHERE registration_id=? AND device_id=?',(instance['registration_id'],binding['device'])).fetchone()
                if rd:target.update(manifest_generation=rd['manifest_generation'],gap_epoch=rd['gap_epoch'])
        db.execute('INSERT INTO action_targets VALUES(?,?)',(aid,encode(target)))

    def _target_current(self,db,action,target):
        from .policy import read_policy
        if action['kind']!='restore' and target.get('policy_revision')!=read_policy(self.directory)['revision']:return False
        binding=db.execute('SELECT * FROM bindings WHERE id=?',(action['binding'],)).fetchone()
        if action['kind']!='restore' and binding and binding['state'] in {'missing','unavailable','unverified'}:return False
        if not binding or target['expires_at']<time.time() or any(target[k]!=binding[v] for k,v in
            [('binding_id','id'),('device_id','device'),('local_path','local_path'),('local_identity','local_identity'),('expected_sha256','sha256')]):return False
        device=db.execute('SELECT 1 FROM devices WHERE id=? AND revoked IS NULL',(target['device_id'],)).fetchone()
        if not device:return False
        if target['registration_id']:
            instance=db.execute('SELECT * FROM file_instances WHERE id=?',(binding['id'],)).fetchone()
            registration=db.execute('SELECT * FROM registrations WHERE id=?',(target['registration_id'],)).fetchone()
            rd=db.execute('SELECT * FROM registration_devices WHERE registration_id=? AND device_id=?',(target['registration_id'],target['device_id'])).fetchone()
            if not instance or not registration or not rd or instance['version_id']!=target['expected_version_id']:return False
            if action['kind']!='restore' and (registration['state']!='ready' or registration['manifest_revision']!=target['registration_revision'] or rd['manifest_generation']!=target['manifest_generation'] or rd['gap_epoch']!=target['gap_epoch']):return False
        return True

    def _package_gap(self,db,owner,device,registration_id,reason):
        from .policy import protection_enabled
        if not protection_enabled(self.directory):return
        if not self._has_table(db,'registration_devices'):return
        where='r.owner=? AND rd.device_id=?';params=[owner,device]
        if registration_id:where+=' AND rd.registration_id=?';params.append(registration_id)
        rows=db.execute('SELECT rd.* FROM registration_devices rd JOIN registrations r ON r.id=rd.registration_id WHERE '+where,params).fetchall()
        for rd in rows:
            epoch=rd['gap_epoch']+1
            db.execute("UPDATE registration_devices SET gap_epoch=?,state='unverified' WHERE registration_id=? AND device_id=?",(epoch,rd['registration_id'],device))
            db.execute('INSERT INTO monitoring_epochs VALUES(?,?,?,?,?,NULL)',(rd['registration_id'],device,epoch,str(reason)[:1000],time.time()))
            db.execute("UPDATE file_instances SET state='unverified' WHERE registration_id=? AND device_id=?",(rd['registration_id'],device))
            db.execute("UPDATE local_manifest_tasks SET state='stale' WHERE registration_id=? AND device_id=? AND state='pending'",(rd['registration_id'],device))

    def _package_instance_event(self,db,device,binding,path,fsid,content,state,item):
        from .policy import protection_enabled
        if not protection_enabled(self.directory):return
        if not self._has_table(db,'file_instances'):return
        instance=db.execute('SELECT * FROM file_instances WHERE id=?',(binding['id'],)).fetchone()
        if not instance:return
        version=db.execute('SELECT id FROM file_versions WHERE sha256=?',(content,)).fetchone()
        if not version:
            version_id=content;db.execute('INSERT INTO file_versions VALUES(?,?,?,?)',(version_id,content,max(0,int(item.get('size',0))),time.time()))
        else:version_id=version[0]
        if item.get('action')=='file.renamed' and (fsid!=binding['local_identity'] or content!=binding['sha256']):state='unverified'
        rd=db.execute('SELECT * FROM registration_devices WHERE registration_id=? AND device_id=?',(instance['registration_id'],device['id'])).fetchone()
        if rd:
            from .registration import _within_local
            if not _within_local(path,rd['local_root']):state='unverified'
            if rd['verified_epoch']!=rd['gap_epoch']:state='unverified'
        db.execute('UPDATE file_instances SET path=?,identity=?,version_id=?,state=?,updated=? WHERE id=?',(path,fsid,version_id,state,time.time(),binding['id']))
        if state not in {'watching','read_locked','delete_locked'} or content!=binding['sha256'] or fsid!=binding['local_identity']:
            db.execute("UPDATE registration_devices SET state='unverified' WHERE registration_id=? AND device_id=?",(instance['registration_id'],device['id']))

    def _package_anomaly(self,db,device,binding,item):
        from .policy import read_policy
        policy=read_policy(self.directory)
        if not policy['enabled'] or not self._has_table(db,'protection_alerts') or item.get('action') not in {'file.modified','file.replaced','file.deleted'}:return
        instance=db.execute('SELECT * FROM file_instances WHERE id=?',(binding['id'],)).fetchone()
        if not instance:return
        rules=policy['operation_policy'];threshold=max(2,int(rules.get('anomaly_count',20)));window=max(1,int(rules.get('anomaly_window',60)))
        count=db.execute("SELECT COUNT(DISTINCT e.file_id) FROM events e JOIN logical_files f ON f.id=e.file_id WHERE e.device=? AND f.registration_id=? AND e.received>? AND e.action IN ('file.modified','file.replaced','file.deleted')",(device['id'],instance['registration_id'],time.time()-window)).fetchone()[0]
        if count<threshold or db.execute('SELECT 1 FROM protection_alerts WHERE registration_id=? AND device_id=? AND resolved IS NULL',(instance['registration_id'],device['id'])).fetchone():return
        reason='观察到 %s 秒内 %s 个文件批量改写或删除'%(window,count)
        db.execute('INSERT INTO protection_alerts VALUES(?,?,?,?,?,?,?,?,NULL)',(uid(),device['owner'],instance['registration_id'],device['id'],'batch_change',reason,encode({'count':count,'window':window,'source':'directory_change','timing':'observed_after_event','response':'pause','observation_only':rules.get('observation_only',True)}),time.time()))
        self._package_gap(db,device['owner'],device['id'],instance['registration_id'],reason)
        self._event(db,device['owner'],'operation.anomaly_observed',device=device['id'],payload={'registration_id':instance['registration_id'],'reason':reason,'source':'directory_change','timing':'observed_after_event','response':'pause'})

    def file_trace(self, owner, file_id):
        with self.db() as db:
            row = db.execute('SELECT f.*,c.state AS collection_state,c.root AS server_root FROM files f '
                             'JOIN collections c ON c.id=f.collection WHERE f.id=? AND f.owner=?',
                             (file_id, owner)).fetchone()
            if not row:
                raise ProtectionError('文件不存在或不属于当前账号', 404)
            bindings = db.execute('SELECT b.*,d.name AS device_name,d.heartbeat,d.health,d.revoked '
                                  'FROM bindings b JOIN devices d ON d.id=b.device '
                                  'WHERE b.file_id=? AND b.owner=? ORDER BY b.updated DESC LIMIT 500',
                                  (file_id, owner)).fetchall()
            instances = []
            for value in bindings:
                instance = dict(value)
                health = json.loads(instance.pop('health'))
                instance['online'] = instance['revoked'] is None and time.time()-instance['heartbeat'] <= 60
                instance['content_matches'] = instance['sha256'] == row['sha256']
                instance['verified'] = row['collection_state'] == 'ready' and instance['content_matches'] and instance['state'] in {'watching','protected','read_locked','delete_locked'}
                instance['monitor_healthy'] = instance['online'] and health.get('healthy') is True
                instances.append(instance)
            if self._has_table(db,'file_instances'):
                existing={i['id'] for i in instances}
                for value in db.execute("SELECT * FROM file_instances WHERE logical_file_id=? AND kind='local'",(file_id,)):
                    if value['id'] in existing:continue
                    instance=dict(value)
                    instance.update(online=False,monitor_healthy=False,content_matches=instance['version_id']==row['sha256'],
                                    verified=row['collection_state']=='ready' and instance['state']=='available',
                                    basis='native_local_reader_no_assistant_audit')
                    instances.append(instance)
        return {'file': dict(row), 'instances': instances,
                'events': self.list_rows('events', owner, file_id=file_id, limit=100),
                'basis': '本机实际读取的文件身份与 SHA-256 内容版本，以及已采集的助手或系统事件；访问事件未读取内容时仅关联采集时版本，不证明该次访问的实际字节或复制因果',
                'coverage': '只覆盖已登记文件、已关联设备及已启用的监控/访问审计；不保证所有操作系统操作均被采集或事前阻断'}

    def verify_chain(self):
        previous = "0" * 64
        count = 0
        with self.db() as db:
            for row in db.execute("SELECT * FROM events ORDER BY seq"):
                if row["previous_hash"] != previous or row["record_hash"] != digest(previous + row["payload"]):
                    return {"ok": False, "first_invalid_seq": row["seq"]}
                record=json.loads(row['payload'])
                if any(record[key]!=row[key] for key in ('event_id','owner','device','file_id','instance_id','action','occurred','received')):
                    return {"ok":False,"first_invalid_seq":row['seq']}
                previous = row["record_hash"]
                count += 1
        return {"ok": True, "count": count, "head": previous}
