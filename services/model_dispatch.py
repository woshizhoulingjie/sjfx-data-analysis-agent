"""Cross-process admission for generation; embeddings never use this gate.

Default priority preserves FIFO for existing calls. Only optional catalog work
uses background priority. Leases survive client instances and detect dead owners.
"""
from contextlib import contextmanager, closing
from contextvars import ContextVar
import functools
import hashlib
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

_background = ContextVar('sjfx_background_generation', default=None)


@contextmanager
def background_generation(cancelled=lambda: False):
    token = _background.set(cancelled)
    try:
        yield
    finally:
        _background.reset(token)


def _connect():
    from config import Config
    path = Path(Config.DATA_DIR) / 'model_dispatch.db'
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5, isolation_level=None)
    conn.execute('PRAGMA busy_timeout=5000')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''CREATE TABLE IF NOT EXISTS admission(
        id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, priority INTEGER NOT NULL,
        created REAL NOT NULL, heartbeat REAL NOT NULL, pid INTEGER NOT NULL,
        active INTEGER NOT NULL DEFAULT 0)''')
    conn.execute('CREATE TABLE IF NOT EXISTS admission_cooldown(endpoint TEXT PRIMARY KEY, until_at REAL NOT NULL)')
    conn.execute('CREATE INDEX IF NOT EXISTS admission_wait ON admission(endpoint,active,priority,created)')
    return conn


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True


@contextmanager
def generation_slot(endpoint, timeout=15):
    from services.ollama import LocalModelError
    if os.getenv('SJFX_SHARED_GENERATION_GATE', '1').lower() in {'0','false','off'}:
        yield
        return
    cancel = _background.get()
    priority = 10 if cancel is not None else 100
    ticket = uuid.uuid4().hex
    endpoint = hashlib.sha256(endpoint.rstrip('/').encode()).hexdigest()
    start = time.monotonic()
    stopped = threading.Event()
    def heartbeat():
        while not stopped.wait(2):
            try:
                with closing(_connect()) as c:
                    c.execute('UPDATE admission SET heartbeat=? WHERE id=?', (time.time(),ticket))
            except sqlite3.Error:
                pass
    with closing(_connect()) as c:
        c.execute('INSERT INTO admission VALUES(?,?,?,?,?,?,0)',
                  (ticket,endpoint,priority,time.time(),time.time(),os.getpid()))
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        while True:
            if cancel is not None and cancel():
                raise LocalModelError('目录命名已暂停或达到预算')
            with closing(_connect()) as c:
                c.execute('BEGIN IMMEDIATE')
                for old, pid, beat in c.execute('SELECT id,pid,heartbeat FROM admission WHERE endpoint=?', (endpoint,)).fetchall():
                    if not _alive(pid) or beat < time.time()-120:
                        c.execute('DELETE FROM admission WHERE id=?', (old,))
                active = c.execute('SELECT id FROM admission WHERE endpoint=? AND active=1 LIMIT 1',(endpoint,)).fetchone()
                first = c.execute('SELECT id FROM admission WHERE endpoint=? AND active=0 ORDER BY priority DESC,created,id LIMIT 1',(endpoint,)).fetchone()
                cooldown=c.execute('SELECT until_at FROM admission_cooldown WHERE endpoint=?',(endpoint,)).fetchone()
                # A client timeout may leave server work finishing. Only optional
                # background requests observe the quarantine; frontend stays usable.
                quarantined=priority<100 and cooldown and cooldown[0]>time.time()
                granted = not active and not quarantined and first and first[0] == ticket
                if granted:
                    c.execute('UPDATE admission SET active=1 WHERE id=?',(ticket,))
                c.commit()
            if granted:
                break
            if time.monotonic()-start >= timeout:
                raise LocalModelError('模型正在处理其他请求，请稍后重试')
            time.sleep(.05)
        try:
            yield
        except Exception as exc:
            if priority<100 and any(x in str(exc).lower() for x in ('timeout','timed out','超时')):
                with closing(_connect()) as c:
                    c.execute('INSERT OR REPLACE INTO admission_cooldown VALUES(?,?)',(endpoint,time.time()+30))
            raise
    finally:
        stopped.set()
        thread.join(timeout=.2)
        with closing(_connect()) as c:
            c.execute('DELETE FROM admission WHERE id=?',(ticket,))


def serialized_generation(func):
    @functools.wraps(func)
    def call(self, *args, **kwargs):
        requested = kwargs.get('timeout') or self.timeout
        with generation_slot(self.base_url, min(15, max(1,int(requested)))):
            return func(self,*args,**kwargs)
    return call


