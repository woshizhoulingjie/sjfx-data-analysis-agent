#!/usr/bin/env python3
"""Restartable, single-writer large-package QA sidecar; never changes source data."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Config
from services.large_qa_index import LargeQAIndex, enabled
from services.large_qa_extract import extract_document
from services.large_qa_topics import refresh_maps, refine_one

LOGGER = logging.getLogger('large_qa_worker')
STOP = False


class ReadOnlyStore:
    """Avoid constructor migrations when auditing or indexing a source database."""
    def __init__(self, path):
        self.path = Path(path).resolve()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        try:
            yield db
        finally:
            db.close()

    def get(self, package_id):
        with self._connect() as db:
            row = db.execute('SELECT * FROM packages WHERE id=?', (package_id,)).fetchone()
            return dict(row) if row else None

    def packages(self):
        with self._connect() as db:
            return [dict(r) for r in db.execute('SELECT id,root_path,owner_id FROM packages ORDER BY created_at DESC')]


def foreground_busy():
    path = Path(Config.DB_PATH)
    if not path.exists():
        return True
    try:
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2) as db:
            jobs = db.execute("SELECT 1 FROM analysis_jobs WHERE status='running' LIMIT 1").fetchone()
            turns = db.execute("SELECT 1 FROM conversation_turns WHERE status IN ('running','queued','waiting_for_deep_analysis') LIMIT 1").fetchone()
            return bool(jobs or turns)
    except sqlite3.Error:
        return True


def heartbeat(base, states, phase):
    target = base/'worker_status.json'
    tmp = target.with_suffix('.tmp')
    tmp.write_text(json.dumps({'pid':os.getpid(),'at':time.time(),'phase':phase,'packages':states},ensure_ascii=False),encoding='utf-8')
    os.chmod(tmp,0o600)
    os.replace(tmp,target)


def snapshot(index):
    with index.connect() as db:
        status = {r[0]:r[1] for r in db.execute('SELECT parse_status,COUNT(*) FROM documents GROUP BY parse_status')}
        meta = index.meta(db)
        return {'files':meta.get('source_files',0),'parse':status,'generation':meta.get('generation'),
                'synced_at':meta.get('synced_at'),'summary_generation':meta.get('summary_generation')}


def run(args):
    import fcntl
    base = Path(args.base).expanduser().resolve()
    base.mkdir(mode=0o700,parents=True,exist_ok=True)
    lock = (base/'worker.lock').open('a+')
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Another large QA worker owns this index directory')
    store = ReadOnlyStore(args.source_db)
    states = {}
    last_sync = {}
    last_refine = 0
    model = None
    while not STOP and enabled():
        packages = store.packages()
        if args.package:
            packages = [p for p in packages if p['id']==args.package]
        active = {p['id'] for p in packages}
        states = {k:v for k,v in states.items() if k in active}
        did_work = False
        for package in packages:
            if STOP:
                break
            package_id = package['id']
            index = LargeQAIndex(package_id,base,create=True)
            try:
                if shutil.disk_usage(base).free < 1024**3:
                    raise RuntimeError('Less than 1 GiB free; extraction paused')
                if time.monotonic()-last_sync.get(package_id,0)>120:
                    heartbeat(base,states,'syncing_summaries')
                    changed = index.sync_store(store)
                    if changed or package_id not in last_sync:
                        refresh_maps(index)
                    last_sync[package_id] = time.monotonic()
                    states[package_id] = snapshot(index)
                    LOGGER.info('summary sync package=%s changed=%s files=%s',package_id,changed,states[package_id]['files'])
                    did_work |= bool(changed)
                if not args.summaries_only:
                    doc = index.next_parse()
                    if doc:
                        heartbeat(base,states,'indexing_originals')
                        try:
                            records,complete,note = extract_document(package['root_path'],doc)
                        except Exception as exc:
                            records,complete,note = [],False,str(exc)
                        index.put_original(doc['path'],doc['source_version'],records,complete,note)
                        did_work = True
                        states[package_id] = snapshot(index)
                if (not args.summaries_only and not STOP and getattr(Config,'LARGE_QA_REFINE_TOPICS',False)
                        and time.monotonic()-last_refine>60 and not foreground_busy()):
                    heartbeat(base,states,'refining_topic')
                    if model is None:
                        from services.vllm import VLLMClient
                        model = VLLMClient(Config.VLLM_BASE_URL,Config.VLLM_MODEL,timeout=90,api_key=Config.VLLM_API_KEY)
                    from services.model_dispatch import background_generation
                    cancelled=lambda: STOP or foreground_busy()
                    with background_generation(cancelled):
                        refined=refine_one(index,model,cancel_check=cancelled)
                    if refined:
                        refresh_maps(index)
                        did_work = True
                    last_refine = time.monotonic()
                states[package_id].pop('error',None)
            except Exception as exc:
                LOGGER.exception('large QA package=%s failed',package_id)
                states.setdefault(package_id,{})['error'] = str(exc)[:300]
            heartbeat(base,states,'running')
        if args.once:
            break
        for _ in range(1 if did_work else 15):
            if STOP:
                break
            time.sleep(1)
    heartbeat(base,states,'stopped' if STOP else 'idle')
    print(json.dumps(states,ensure_ascii=False),flush=True)
    lock.close()
    if args.once and any(x.get('error') for x in states.values()):
        raise SystemExit(1)


def stop(_signum,_frame):
    global STOP
    STOP = True


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-db',default=str(Config.DATA_DIR/'large_package.db'))
    parser.add_argument('--base',default=str(Config.LARGE_QA_INDEX_DIR))
    parser.add_argument('--package')
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--summaries-only',action='store_true')
    options = parser.parse_args()
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    try:
        os.nice(8)
    except (AttributeError,OSError):
        pass
    run(options)
