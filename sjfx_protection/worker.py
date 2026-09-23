"""Dedicated inventory process; never starts an SJFX model/analysis job."""
from __future__ import annotations

import argparse
import os
import time
import tempfile
from pathlib import Path

from .scanner import inspect_file, stable_hash, bounded_read
from .store import Store, uid, encode


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
        root = Path(row["root"]).resolve(strict=True)
        if not root.is_dir() or not any(root.is_relative_to(Path(a).resolve()) for a in allowed_roots):
            raise ValueError("目录不在允许范围内")
        def fail_walk(error):
            raise error
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=fail_walk):
            # Skipped symlinks are explicit failures: no false complete inventory.
            for name in dirs + files:
                if (Path(directory) / name).is_symlink():
                    raise ValueError("所选目录含符号链接，请选择实际资料目录：" + name)
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
                st = path.stat()
                if st.st_size != identity["size"] or st.st_mtime_ns != identity["mtime_ns"]:
                    raise ValueError("检测期间文件发生变化：" + name)
                with store.db(True) as db:
                    db.execute("UPDATE files SET risk=?,findings=?,scan_status=? WHERE id=?",
                               (result["risk"],encode(result["findings"]),encode(result["engines"]),fid))
                    store._event(db,row["owner"],"file.inspected",file_id=fid,
                                 payload={"sha256":identity["sha256"],**result})
        with store.db(True) as db:
            db.execute("UPDATE collections SET state='ready',updated=? WHERE id=? AND state='running'",
                       (time.time(),collection))
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
        if root.is_symlink() or not root.is_dir():raise ValueError('资料目录已改变')
        seen=set()
        def fail(error):raise error
        for directory,dirs,files in os.walk(root,followlinks=False,onerror=fail):
            for name in dirs+files:
                if (Path(directory)/name).is_symlink():raise ValueError('目录出现符号链接')
            for name in files:
                path=str(Path(directory)/name)
                previous=records.get(path)
                if not previous:raise ValueError('服务器目录新增文件，请重新登记')
                current=stable_hash(path)
                if current['sha256']!=previous['sha256'] or current['identity']!=previous['identity']:
                    raise ValueError('服务器文件版本已变化，请重新登记')
                seen.add(path)
        if seen!=set(records):raise ValueError('服务器目录有文件删除或移动，请重新登记')
        with store.db(True) as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('verified:'+collection,str(time.time())))
    except Exception as exc:
        with store.db(True) as db:
            db.execute("UPDATE collections SET state='failed',error=?,updated=? WHERE id=? AND state='ready'",
                       (str(exc)[:1000],time.time(),collection))
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
