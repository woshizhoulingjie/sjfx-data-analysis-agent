"""Dedicated inventory process; never starts an SJFX model/analysis job."""
from __future__ import annotations

import argparse
import os
import time
import tempfile
from pathlib import Path

from .scanner import inspect_file, stable_hash, bounded_read
from .store import Store, uid, encode


def _directory_indirection(path):
    # Windows junctions are not symlinks, and os.walk(followlinks=False) still
    # traverses them. Never certify an inventory containing either kind.
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


def inventory(store, collection, allowed_roots, max_files=200000, deadline=7200):
    with store.db(True) as db:
        row = db.execute("SELECT * FROM collections WHERE id=? AND state='queued'", (collection,)).fetchone()
        if not row:
            return
        db.execute("UPDATE collections SET state='running',worker_pid=?,updated=? WHERE id=?",
                   (os.getpid(), time.time(), collection))
    count = 0
    started = time.monotonic()
    try:
        selected_root = Path(row["root"])
        if _directory_indirection(selected_root):
            raise ValueError("所选目录是符号链接或目录联接，请选择实际资料目录")
        root = selected_root.resolve(strict=True)
        if not root.is_dir() or not any(root.is_relative_to(Path(a).resolve()) for a in allowed_roots):
            raise ValueError("目录不在允许范围内")
        def fail_walk(error):
            raise error
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=fail_walk):
            # Skipped symlinks are explicit failures: no false complete inventory.
            for name in dirs + files:
                if _directory_indirection(Path(directory) / name):
                    raise ValueError("所选目录含符号链接或目录联接，请选择实际资料目录：" + name)
            for name in sorted(files):
                if count >= max_files or time.monotonic() - started > deadline:
                    raise ValueError("清单达到数量或时间限制，请分批登记")
                with store.db() as db:
                    state = db.execute("SELECT state FROM collections WHERE id=?", (collection,)).fetchone()
                if state[0] == "cancelled":
                    return
                path = Path(directory) / name
                if not path.resolve().is_relative_to(root):
                    raise ValueError("文件路径超出资料目录")
                identity = stable_hash(path)
                fid = uid()
                with store.db(True) as db:
                    db.execute("INSERT INTO files(id,collection,owner,path,relative_path,size,sha256,mtime_ns,identity,updated) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)", (fid, collection, row["owner"], str(path), str(path.relative_to(root)),
                        identity["size"], identity["sha256"], identity["mtime_ns"], identity["identity"], time.time()))
                    count += 1
                    db.execute("UPDATE collections SET file_count=?,updated=? WHERE id=?", (count,time.time(),collection))
                if identity['size']<=20*1024*1024:
                    # Inspect a bounded verified snapshot: external engines never follow a swapped source path.
                    raw=bounded_read(path,expected_sha256=identity['sha256'])
                    with tempfile.TemporaryDirectory(prefix='inspect-',dir=store.directory) as tmp:
                        snapshot=Path(tmp)/path.name
                        snapshot.write_bytes(raw)
                        result = inspect_file(snapshot, rspamd=os.getenv("SJFX_PROTECTION_RSPAMD"),
                                              yara=os.getenv("SJFX_PROTECTION_YARA"),
                                              yara_rules=os.getenv("SJFX_PROTECTION_YARA_RULES"))
                else:
                    result={'risk':'unchecked','findings':[{'rule':'size_limit','detail':'超过静态检查大小限制'}],
                            'engines':{'static':'skipped_size_limit'}}
                # Invalidate a verdict if the underlying file changed during inspection.
                after = stable_hash(path)
                if after != identity:
                    raise ValueError("检测期间文件发生变化：" + name)
                with store.db(True) as db:
                    db.execute("UPDATE files SET risk=?,findings=?,scan_status=? WHERE id=?",
                               (result["risk"],encode(result["findings"]),encode(result["engines"]),fid))
                    store._event(db,row["owner"],"file.inspected",file_id=fid,
                                 payload={"sha256":identity["sha256"],**result})
        with store.db(True) as db:
            ready = db.execute("UPDATE collections SET state='ready',updated=? WHERE id=? AND state='running'",
                               (time.time(),collection))
            if ready.rowcount != 1:
                return
            db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", ('verified:'+collection,str(time.time())))
            store._event(db,row["owner"],"collection.ready",payload={"collection_id":collection,"count":count})
    except Exception as exc:
        with store.db(True) as db:
            db.execute("UPDATE collections SET state='failed',error=?,updated=? WHERE id=? AND state!='cancelled'",
                       (str(exc)[:1000],time.time(),collection))
            store._event(db,row["owner"],"collection.failed",payload={"collection_id":collection,"error":str(exc)[:1000]})


def verify_inventory(store, collection):
    """Rehash in a separate process: NAS latency never blocks the web request."""
    row=None
    try:
        with store.db() as db:
            row=db.execute("SELECT * FROM collections WHERE id=? AND state='ready'",(collection,)).fetchone()
            records={r['path']:dict(r) for r in db.execute('SELECT * FROM files WHERE collection=?',(collection,))}
        if not row:return
        root=Path(row['root'])
        if _directory_indirection(root) or not root.is_dir():raise ValueError('资料目录已改变')
        seen=set()
        changes=[]
        renamed=set()
        by_identity = {}
        for original in records.values():
            by_identity.setdefault(original['identity'], []).append(original)
        def fail(error):raise error
        for directory,dirs,files in os.walk(root,followlinks=False,onerror=fail):
            for name in dirs+files:
                if _directory_indirection(Path(directory)/name):raise ValueError('目录出现符号链接或目录联接')
            for name in files:
                path=str(Path(directory)/name)
                previous=records.get(path)
                current=stable_hash(path)
                if not previous:
                    candidates = [p for p in by_identity.get(current['identity'], [])
                                  if p['sha256'] == current['sha256'] and not Path(p['path']).exists()]
                    if len(candidates) == 1:
                        previous = candidates[0]
                        renamed.add(previous['path'])
                        changes.append(('server.file.renamed', previous['id'],
                                        {'path': path, 'old_path': previous['path'], 'sha256': current['sha256']}))
                    else:
                        changes.append(('server.file.created', '', {'path': path, 'sha256': current['sha256']}))
                elif current['sha256']!=previous['sha256'] or current['identity']!=previous['identity']:
                    action = 'server.file.replaced' if current['identity'] != previous['identity'] else 'server.file.modified'
                    changes.append((action, previous['id'], {'path': path, 'old_sha256': previous['sha256'],
                                    'sha256': current['sha256'], 'identity': current['identity']}))
                seen.add(path)
        for path in set(records) - seen - renamed:
            changes.append(('server.file.deleted', records[path]['id'], {'path': path, 'sha256': records[path]['sha256']}))
        with store.db(True) as db:
            # Concurrent cancellation/re-registration must never get a fresh verification timestamp.
            active = db.execute("SELECT 1 FROM collections WHERE id=? AND state='ready'", (collection,)).fetchone()
            if not active:
                return
            if changes:
                for action, file_id, payload in changes:
                    store._event(db, row['owner'], action, file_id=file_id,
                                 payload={'collection_id': collection, 'server_root': row['root'], **payload})
                    if file_id:
                        db.execute('DELETE FROM risk_reviews WHERE file_id=?', (file_id,))
                db.execute("UPDATE collections SET state='failed',error=?,updated=? WHERE id=?",
                           ('服务器文件发生变化，旧清单已失效；请重新登记', time.time(), collection))
                db.execute('DELETE FROM settings WHERE key=?', ('verified:'+collection,))
                store._event(db, row['owner'], 'collection.changed',
                             payload={'collection_id': collection, 'change_count': len(changes)})
                return
            db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('verified:'+collection,str(time.time())))
    except Exception as exc:
        with store.db(True) as db:
            db.execute("UPDATE collections SET state='failed',error=?,updated=? WHERE id=? AND state='ready'",
                       (str(exc)[:1000],time.time(),collection))
            db.execute('DELETE FROM settings WHERE key=?', ('verified:'+collection,))
            if row:store._event(db,row['owner'],'collection.changed',payload={'collection_id':collection,'error':str(exc)[:1000]})


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--state", required=True)
    p.add_argument("--collection", required=True)
    p.add_argument("--allowed-root", action="append", default=[])
    p.add_argument("--verify", action="store_true")
    args = p.parse_args()
    if args.verify:verify_inventory(Store(args.state),args.collection)
    else:inventory(Store(args.state),args.collection,args.allowed_root)
