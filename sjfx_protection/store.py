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
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
            if time.time() - result["heartbeat"] > 60 or not health.get("healthy"):
                raise ProtectionError("本地助手离线或监视异常", 423, "device_unhealthy")
        return result

    def heartbeat(self, device, health):
        allowed = {k: health.get(k) for k in ("healthy", "watch_roots", "queue_depth", "errors",
                   "lost_events", "agent_version", "capabilities", "binding_count", "last_error")}
        with self.db(True) as db:
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
            if not row or row['revoked'] or time.time()-row['heartbeat']>60 or not json.loads(row['health']).get('healthy'):
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
                    where.append("(file_id=? OR (file_id='' AND json_extract(payload,'$.payload.collection_id') IN "
                                 "(SELECT collection FROM files WHERE id=? AND owner=?)))")
                    params.extend([value,value,owner])
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
        if not isinstance(records, list) or len(records) > 500:
            raise ProtectionError("每批最多绑定 500 个文件")
        out = []
        with self.db(True) as db:
            for rec in records:
                file = db.execute("SELECT * FROM files WHERE id=? AND owner=?",
                                  (rec.get("file_id"), device["owner"])).fetchone()
                if not file or rec.get("sha256") != file["sha256"]:
                    raise ProtectionError("文件不存在或内容与服务器版本不一致", 409)
                path, identity = str(rec.get("local_path", "")), str(rec.get("local_identity", ""))
                if not path or len(path) > 32760 or not identity or len(identity) > 256:
                    raise ProtectionError("缺少本地路径或文件身份")
                existing = db.execute("SELECT id FROM bindings WHERE device=? AND file_id=? AND local_path=?",
                                      (device["id"], file["id"], path)).fetchone()
                bid = existing[0] if existing else uid()
                db.execute("INSERT INTO bindings VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(device,file_id,local_path) "
                           "DO UPDATE SET local_identity=excluded.local_identity,sha256=excluded.sha256,"
                           "state='watching',updated=excluded.updated",
                           (bid, file["id"], device["id"], device["owner"], path, identity, file["sha256"],
                            "watching", time.time()))
                self._event(db, device["owner"], "file.bound", device=device["id"], file_id=file["id"],
                            instance_id=bid, payload={"local_path": path, "identity": identity,
                                                     "sha256": file["sha256"], "basis": "content_equal"})
                out.append({"binding_id": bid, "file_id": file["id"], "local_path": path})
                if file['risk']=='dangerous' and not db.execute("SELECT 1 FROM actions WHERE binding=? AND kind='quarantine' AND state IN ('pending','completed')",(bid,)).fetchone():
                    aid=uid()
                    db.execute("INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                               (aid,device['owner'],device['id'],bid,'quarantine',file['sha256'],
                                '自动处置：服务器静态检测命中危险文件','pending',time.time(),'{}',''))
                    self._event(db,device['owner'],'action.requested',device=device['id'],file_id=file['id'],instance_id=bid,
                                payload={'action_id':aid,'kind':'quarantine','automatic':True})
        return out

    def related_copy(self, device, data):
        with self.db(True) as db:
            source=db.execute('SELECT * FROM bindings WHERE id=? AND device=? AND owner=?',
                              (data.get('source_binding'),device['id'],device['owner'])).fetchone()
            if not source or data.get('sha256')!=source['sha256']:
                raise ProtectionError('对应文件版本已经变化，请重新核对',409)
            path=str(data.get('local_path',''));identity=str(data.get('local_identity',''))
            if not path or len(path)>32760 or not identity or len(identity)>256:
                raise ProtectionError('本地文件身份无效')
            existing=db.execute('SELECT id FROM bindings WHERE device=? AND file_id=? AND local_path=?',
                                (device['id'],source['file_id'],path)).fetchone()
            if existing:return {'binding_id':existing[0]}
            bid=uid()
            db.execute('INSERT INTO bindings VALUES(?,?,?,?,?,?,?,?,?)',(bid,source['file_id'],device['id'],device['owner'],
                       path,identity,source['sha256'],'watching',time.time()))
            self._event(db,device['owner'],'file.identical_instance',device=device['id'],file_id=source['file_id'],instance_id=bid,
                        payload={'source_binding':source['id'],'local_path':path,'identity':identity,'sha256':source['sha256'],
                                 'basis':'content_equal_not_proven_copy'})
            return {'binding_id':bid}

    def ingest(self, device, events):
        if not isinstance(events, list) or len(events) > 500:
            raise ProtectionError("每批最多 500 条事件")
        accepted = []
        with self.db(True) as db:
            for item in events:
                event_id = str(item.get("event_id", ""))
                if not 8 <= len(event_id) <= 100:
                    raise ProtectionError("事件编号无效")
                bid = str(item.get("binding_id", ""))
                binding = db.execute("SELECT * FROM bindings WHERE id=? AND device=? AND owner=?",
                                     (bid, device["id"], device["owner"])).fetchone() if bid else None
                action = str(item.get("action", ""))
                if binding is None and action not in {"monitor.gap", "monitor.health", "monitor.error"}:
                    raise ProtectionError("事件不属于已关联文件", 403)
                if len(encode(item)) > 16000:
                    raise ProtectionError("单条事件过大")
                inserted = self._event(db, device["owner"], action[:100], device=device["id"],
                    file_id=binding["file_id"] if binding else "", instance_id=bid,
                    event_id=device["id"] + ":" + event_id,
                    occurred=float(item.get("occurred", time.time())), payload=item)
                if inserted and binding and action in {'file.modified','file.replaced','file.renamed','file.deleted',
                                                       'file.reappeared','file.unavailable','file.protected','file.restored'}:
                    state = "missing" if action == "file.deleted" else "watching"
                    if action in {"file.unavailable", "monitor.gap"}:
                        state = "unavailable"
                    if action == 'file.protected':
                        state = 'protected'
                    path = str(item.get("path") or binding["local_path"])
                    sha = str(item.get("sha256") or binding["sha256"])
                    identity = str(item.get("identity") or binding["local_identity"])
                    db.execute("UPDATE bindings SET local_path=?,local_identity=?,sha256=?,state=?,updated=? WHERE id=?",
                               (path, identity, sha, state, time.time(), bid))
                accepted.append(event_id)
        return accepted

    def require_root(self, owner, device, root):
        # Match a finalized inventory, not a browser-supplied file count.
        root = os.path.abspath(os.path.expanduser(root))
        with self.db() as db:
            collection = db.execute("SELECT * FROM collections WHERE owner=? AND root=? AND state='ready' "
                                    "ORDER BY created DESC LIMIT 1", (owner, root)).fetchone()
            if not collection:
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
            if kind == "restore":
                previous = db.execute("SELECT * FROM actions WHERE id=? AND binding=? AND state='completed'",
                                      (restore_action_id, binding_id)).fetchone()
                if not previous or previous["kind"] == "restore":
                    raise ProtectionError("不存在可撤销的成功处置")
            aid = uid()
            db.execute("INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (aid, owner, binding["device"], binding_id, kind, binding["sha256"], reason[:1000],
                        "pending", time.time(), "{}", restore_action_id))
            self._event(db, owner, "action.requested", device=binding["device"], file_id=binding["file_id"],
                        instance_id=binding_id, payload={"action_id": aid, "kind": kind, "reason": reason[:1000]})
        return aid

    def pending_actions(self, device):
        with self.db() as db:
            rows = db.execute("SELECT a.*,b.local_path,b.local_identity FROM actions a JOIN bindings b "
                              "ON a.binding=b.id WHERE a.device=? AND a.state='pending' ORDER BY a.created LIMIT 100",
                              (device,)).fetchall()
        return [dict(row) for row in rows]

    def action_result(self, device, aid, result):
        if result.get("status") not in {"completed", "failed", "unsupported", "stale"}:
            raise ProtectionError("处置结果无效")
        with self.db(True) as db:
            row = db.execute("SELECT * FROM actions WHERE id=? AND device=?", (aid, device["id"])).fetchone()
            if not row:
                raise ProtectionError("处置不存在", 404)
            if row["state"] != "pending":
                return
            db.execute("UPDATE actions SET state=?,result=? WHERE id=?",
                       (result["status"], encode(result)[:16000], aid))
            if row['kind']=='restore' and result['status']=='completed':
                db.execute("UPDATE actions SET state='restored' WHERE id=? AND binding=?",
                           (row['restore_action_id'],row['binding']))
            binding = db.execute("SELECT file_id FROM bindings WHERE id=?", (row["binding"],)).fetchone()
            self._event(db, device["owner"], "action." + result["status"], device=device["id"],
                        file_id=binding[0], instance_id=row["binding"],
                        payload={"action_id": aid, "kind": row["kind"], "result": result})

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
