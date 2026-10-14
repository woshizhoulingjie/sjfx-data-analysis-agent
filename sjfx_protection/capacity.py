"""One bounded storage budget for snapshots, trees, staging and derived bytes."""
from __future__ import annotations
import contextlib
import os
import time
from pathlib import Path
from .store import ProtectionError, encode, uid
import json

AREAS=('snapshots','snapshot-trees','staging','derived')

def assert_managed_path(store,path):
    state=store.directory.absolute();path=Path(path).absolute()
    if '..' in path.parts or (os.name=='nt' and any(':' in part for part in path.parts[1:])):
        raise ProtectionError('受控存储路径含父目录或备用数据流',423,'storage_invalid')
    for item in {state,path,*state.parents,*path.parents}:
        if item.is_symlink() or (hasattr(item,'is_junction') and item.is_junction()):
            raise ProtectionError('受控存储路径经过链接或目录联接',423,'storage_invalid')
    area=next((state/a for a in AREAS if path==state/a or path.is_relative_to(state/a)),None)
    if area is None:
        raise ProtectionError('受控存储路径越界',423,'storage_invalid')
    if not path.resolve().is_relative_to(area.resolve()):
        raise ProtectionError('受控存储解析到状态目录以外',423,'storage_invalid')
    return path

def managed_files(store):
    for area in AREAS:
        root=store.directory/area
        assert_managed_path(store,root)
        if not root.exists():continue
        for directory,dirs,files in os.walk(root,followlinks=False):
            for name in dirs+files:
                value=Path(directory)/name
                if value.is_symlink() or (hasattr(value,'is_junction') and value.is_junction()):
                    raise ProtectionError('受控存储出现链接',423,'storage_invalid')
            for name in files:yield assert_managed_path(store,Path(directory)/name)

@contextlib.contextmanager
def reserve(store,target,size,profile):
    target=assert_managed_path(store,target)
    key='capacity:'+uid();now=time.time()
    with store.db(True) as db:
        outstanding=0
        for row in db.execute("SELECT * FROM settings WHERE key LIKE 'capacity:%' OR key LIKE 'snapshot-reservation:%'"):
            value=json.loads(row['value'])
            if value['expires']<now:
                db.execute('DELETE FROM settings WHERE key=?',(row['key'],));continue
            path=Path(value['path']);written=path.stat().st_size if path.is_file() else 0
            outstanding+=max(0,value['bytes']-written)
        occupied=sum(p.stat().st_size for p in managed_files(store))
        if occupied+outstanding+size>profile['snapshot_quota_bytes']:
            raise ProtectionError('快照、暂存和派生内容总配额不足',423,'snapshot_quota')
        db.execute('INSERT INTO settings VALUES(?,?)',(key,encode({'path':str(target),'bytes':size,'expires':now+profile.get('inventory_timeout_seconds',7200)+600})))
    try:yield
    finally:
        with store.db(True) as db:db.execute('DELETE FROM settings WHERE key=?',(key,))

def prune(store,profile):
    """Remove only expired, unreferenced files under our four managed areas.

    Never deletes a business output or an input file. Missing evidence remains
    fail closed; history and audit records are retained.
    """
    now=time.time();retention=profile['snapshot_retention_seconds'];removed=0;freed=0
    with store.db(True) as db:
        db.execute('DELETE FROM version_snapshots WHERE expires<?',(now,))
        db.execute('DELETE FROM artifact_snapshots WHERE expires<?',(now,))
        db.execute('DELETE FROM package_download_tickets WHERE expires<?',(now,))
        live={r[0] for r in db.execute('SELECT path FROM version_snapshots UNION SELECT path FROM artifact_snapshots')}
        # An artifact path can itself be an inspected source snapshot.
        live.update(r[0] for r in db.execute('SELECT a.path FROM protection_artifacts a JOIN artifact_snapshots s ON s.artifact_id=a.id'))
        reservations=[json.loads(r[0]) for r in db.execute("SELECT value FROM settings WHERE key LIKE 'capacity:%' OR key LIKE 'snapshot-reservation:%'")]
        live.update(r['path'] for r in reservations if r['expires']>=now)
        for path in managed_files(store):
            if str(path) in live or now-path.stat().st_mtime<retention:continue
            if 'snapshot-trees' in path.parts:
                relative=path.relative_to(store.directory/'snapshot-trees')
                if len(relative.parts)<3:continue
                rid,revision=relative.parts[:2]
                if db.execute('SELECT 1 FROM registrations WHERE id=? AND manifest_revision=?',(rid,revision)).fetchone():continue
            assert_managed_path(store,path)
            size=path.stat().st_size
            try:path.unlink()
            except OSError:continue
            removed+=1;freed+=size
        db.execute('DELETE FROM inspections WHERE expires<?',(now,))
        if removed:store._event(db,'system','storage.pruned',payload={'files':removed,'bytes':freed,'areas':AREAS})
    return {'removed':removed,'bytes':freed}
