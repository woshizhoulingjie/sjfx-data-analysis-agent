#!/usr/bin/env python3
"""Restartable, single-writer large-package QA sidecar; never changes source data."""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import json
import logging
import os
from pathlib import Path
import signal
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Config
from services.large_qa_index import LargeQAIndex, enabled
from services import large_qa_demand as demand
from services.large_qa_extract import extract_document
from services.large_qa_topics import refresh_maps, refine_one
from services.large_qa_resources import DiskGuard, resource_code
from services.process_liveness import try_lock_file
from services.model_backend import create_model_client

LOGGER = logging.getLogger('large_qa_worker')
STOP = False
RESOURCE_RETRY_SECONDS = 30


class ReadOnlyStore:
    """Avoid constructor migrations when auditing or indexing a source database."""
    def __init__(self, path):
        self.path = Path(path).resolve()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True, timeout=20)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            yield db
        finally:
            db.close()

    def get(self, package_id):
        with self._connect() as db:
            row = db.execute('SELECT * FROM packages WHERE id=?', (package_id,)).fetchone()
            return dict(row) if row else None

    def packages(self):
        with self._connect() as db:
            # Preserve the cheap package stamp. Without these fields, an empty
            # first inventory snapshot appears unchanged until the long forced
            # sync interval, even after preview has populated the source rows.
            return [dict(r) for r in db.execute(
                'SELECT id,root_path,owner_id,updated_at,progress,counts,phase,status '
                'FROM packages ORDER BY created_at DESC')]


def foreground_busy():
    path = Path(Config.DB_PATH)
    if not path.exists():
        return True
    try:
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
            jobs = db.execute("SELECT 1 FROM analysis_jobs WHERE status='running' LIMIT 1").fetchone()
            turns = db.execute("SELECT 1 FROM conversation_turns WHERE status IN ('running','queued','waiting_for_deep_analysis') LIMIT 1").fetchone()
            return bool(jobs or turns)
    except sqlite3.Error:
        return True


def heartbeat(base, states, phase):
    target = base/'worker_status.json'
    tmp = target.with_suffix('.tmp')
    try:
        tmp.write_text(json.dumps({'pid':os.getpid(),'at':time.time(),'phase':phase,'packages':states},ensure_ascii=False),encoding='utf-8')
        os.chmod(tmp,0o600)
        os.replace(tmp,target)
    except OSError:
        # A full state volume must not kill the only worker capable of resuming.
        # Keep the last good status file (its stale timestamp remains visible).
        LOGGER.exception('Unable to publish large QA heartbeat; will retry')
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def state_phase(states, default='running'):
    if states and all(item.get('resource_pause') for item in states.values()):
        return 'waiting_for_resources'
    return default


def snapshot(index):
    with index.connect() as db:
        status = {r[0]:r[1] for r in db.execute('SELECT parse_status,COUNT(*) FROM documents GROUP BY parse_status')}
        meta = index.meta(db)
        return {'files':meta.get('source_files',0),'parse':status,'generation':meta.get('generation'),
                'synced_at':meta.get('synced_at'),'summary_generation':meta.get('summary_generation')}


def run(args):
    base = Path(args.base).expanduser().resolve()
    base.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (base/'worker.lock').open('a+b') as lock:
        if not try_lock_file(lock):
            raise SystemExit('Another large QA worker owns this index directory')
        return _run_locked(args, base)


def _run_locked(args, base):
    store = ReadOnlyStore(args.source_db)
    states = {}
    last_sync = {}
    last_refine = 0
    model = None
    stamps = {}
    retry_after = {}
    disk = DiskGuard()
    agent_db = str(getattr(Config,'DB_PATH',''))
    from services.managed_entry import mark_role_ready
    store.packages()  # Report readiness only after the source schema is readable.
    mark_role_ready()
    while not STOP and enabled():
        packages = store.packages()
        if args.package:
            packages = [p for p in packages if p['id']==args.package]
        elif demand.demand_only():
            # 按需：只做"最近被问答过 / 问答时点名要过原文 / 人工钉住"的包。
            wanted = demand.demanded(agent_db, base, [p['id'] for p in packages])
            packages = [p for p in packages if p['id'] in wanted]
            if not packages:
                states = {}
                last_sync.clear()
                stamps.clear()
                retry_after.clear()
                heartbeat(base, states, 'idle')
                if args.once:
                    break
                time.sleep(demand.idle_sleep())
                continue
        active = {p['id'] for p in packages}
        states = {k:v for k,v in states.items() if k in active}
        last_sync = {k:v for k,v in last_sync.items() if k in active}
        stamps = {k:v for k,v in stamps.items() if k in active}
        retry_after = {k:v for k,v in retry_after.items() if k in active}
        did_work = False
        extracted = 0
        budget = demand.extract_budget()
        pause = demand.extract_pause()
        for package in packages:
            if STOP:
                break
            package_id = package['id']
            if time.monotonic() < retry_after.get(package_id, 0):
                continue
            try:
                # Check capacity before constructor migrations or any package writes.
                disk.check([('问答索引磁盘', base)])
                index = LargeQAIndex(package_id,base,create=True)
                stamp = demand.package_stamp(package)
                if demand.should_sync(time.monotonic(),last_sync.get(package_id),
                                      stamps.get(package_id),stamp):
                    heartbeat(base,states,'syncing_summaries')
                    changed = index.sync_store(store)
                    if changed or package_id not in last_sync:
                        refresh_maps(index)
                    last_sync[package_id] = time.monotonic()
                    stamps[package_id] = stamp
                    states[package_id] = snapshot(index)
                    LOGGER.info('summary sync package=%s changed=%s files=%s',package_id,changed,states[package_id]['files'])
                    did_work |= bool(changed)
                if not args.summaries_only and extracted<budget and not foreground_busy():
                    doc = index.next_parse()
                    if doc:
                        disk.check([('问答索引磁盘', base),
                                    ('原文解析结果磁盘', Config.DATA_DIR/'large_qa'/'parse_artifacts'),
                                    ('解析临时磁盘', os.getenv('SJFX_PARSE_TEMP_DIR', str(Config.PARSE_TEMP_DIR)))])
                        heartbeat(base,states,'indexing_originals')
                        try:
                            records,complete,note = extract_document(package['root_path'],doc)
                        except Exception as exc:
                            if resource_code(exc):
                                raise
                            records,complete,note = [],False,str(exc)
                        index.put_original(doc['path'],doc['source_version'],records,complete,note)
                        did_work = True
                        extracted += 1
                        states[package_id] = snapshot(index)
                        if pause:
                            time.sleep(pause)
                if (not args.summaries_only and not STOP and getattr(Config,'LARGE_QA_REFINE_TOPICS',False)
                        and time.monotonic()-last_refine>60 and not foreground_busy()):
                    if model is None:
                        model = create_model_client(Config, timeout=90, max_concurrency=1)
                    if model is not None:
                        heartbeat(base,states,'refining_topic')
                        from services.model_dispatch import background_generation
                        cancelled=lambda: STOP or foreground_busy()
                        with background_generation(cancelled):
                            refined=refine_one(index,model,cancel_check=cancelled)
                        if refined:
                            refresh_maps(index)
                            did_work = True
                    last_refine = time.monotonic()
                states.setdefault(package_id,{})
                for key in ('error', 'resource_pause', 'retry_at'):
                    states[package_id].pop(key, None)
                retry_after.pop(package_id, None)
            except Exception as exc:
                state = states.setdefault(package_id,{})
                code = resource_code(exc)
                if code:
                    state['error'] = '资源暂不可用，任务保留，稍后自动重试：' + str(exc)[:260]
                    state['resource_pause'] = {'code': code, 'details': getattr(exc, 'details', [])}
                    state['retry_at'] = time.time() + RESOURCE_RETRY_SECONDS
                    retry_after[package_id] = time.monotonic() + RESOURCE_RETRY_SECONDS
                    LOGGER.warning('large QA package=%s paused resource=%s: %s', package_id, code, exc)
                else:
                    LOGGER.exception('large QA package=%s failed',package_id)
                    state['error'] = str(exc)[:300]
                    state.pop('resource_pause', None)
                    state.pop('retry_at', None)
                    retry_after.pop(package_id, None)
            heartbeat(base,states,state_phase(states))
        heartbeat(base,states,state_phase(states))
        if args.once:
            break
        for _ in range(1 if did_work else 15):
            if STOP:
                break
            time.sleep(1)
    heartbeat_ok = heartbeat(base,states,'stopped' if STOP else state_phase(states, 'idle'))
    print(json.dumps(states,ensure_ascii=False),flush=True)
    if args.once and (not heartbeat_ok or any(x.get('error') for x in states.values())):
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
