"""Package-local, crash-recoverable topic windows; never wait under the DB lock."""
from contextlib import closing, contextmanager
import json
import os
import threading
import time
import uuid

_local = threading.local()


def _ensure_schema(c):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='lp_catalog_windows'").fetchone():
        init(c)
        c.commit()


def init(c):
    c.execute('''CREATE TABLE IF NOT EXISTS lp_catalog_windows(
      package_id TEXT PRIMARY KEY, token TEXT NOT NULL, expires REAL NOT NULL,
      retry_at REAL NOT NULL DEFAULT 0, outcome TEXT, detail TEXT)''')
    columns = {row[1] for row in c.execute('PRAGMA table_info(lp_catalog_windows)')}
    for name, definition in [('owner_pid','INTEGER NOT NULL DEFAULT 0'), ('owner_started','REAL NOT NULL DEFAULT 0'),
                             ('active','INTEGER NOT NULL DEFAULT 0')]:
        if name not in columns:
            c.execute('ALTER TABLE lp_catalog_windows ADD COLUMN '+name+' '+definition)
    c.execute('''CREATE TABLE IF NOT EXISTS lp_catalog_mutations(
      token TEXT PRIMARY KEY, package_id TEXT NOT NULL, kind TEXT NOT NULL, expires REAL NOT NULL)''')
    c.execute('CREATE INDEX IF NOT EXISTS lp_catalog_mutation_package ON lp_catalog_mutations(package_id,expires)')
    c.execute('''CREATE TRIGGER IF NOT EXISTS lp_catalog_window_delete AFTER DELETE ON packages BEGIN
      DELETE FROM lp_catalog_windows WHERE package_id=OLD.id;
      DELETE FROM lp_catalog_mutations WHERE package_id=OLD.id; END''')


def blocked(c, package_id, now=None):
    """Check inside a leaf claim's existing transaction, closing the claim race."""
    now = time.time() if now is None else now
    row = c.execute('SELECT expires FROM lp_catalog_windows WHERE package_id=?', (package_id,)).fetchone()
    return bool(row and row[0] > now)


def _owner_alive(pid, started):
    if not pid:
        return False
    import psutil
    try:
        process = psutil.Process(pid)
        return process.is_running() and abs(process.create_time()-started)<1.
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True


@contextmanager
def leaf_batch(store, package_id, kind='leaf', cancelled=lambda: False):
    """Yield False when a window requested a pause; already-started work can finish.

    Nested maintenance on the same thread shares its outer lease. A stale lease
    expires after restart; workers must still apply their revision/control guards.
    """
    key = (id(store), package_id)
    active = getattr(_local, 'leaves', {})
    if key in active:
        yield True
        return
    token = uuid.uuid4().hex
    allowed = False
    with store._lock, closing(store._connect()) as c:
        _ensure_schema(c)
        c.execute('BEGIN IMMEDIATE')
        if not cancelled() and not blocked(c, package_id):
            c.execute('INSERT INTO lp_catalog_mutations VALUES(?,?,?,?)',
                      (token, package_id, kind, time.time()+120))
            allowed = True
        c.commit()
    if allowed:
        active[key] = token
        _local.leaves = active
    stopped = threading.Event()
    thread = None
    if allowed:
        def heartbeat():
            while not stopped.wait(30):
                with store._lock, closing(store._connect()) as c:
                    c.execute('UPDATE lp_catalog_mutations SET expires=? WHERE token=?', (time.time()+120, token))
                    c.commit()
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
    try:
        yield allowed
    finally:
        if allowed:
            stopped.set()
            thread.join(timeout=.5)
            active.pop(key, None)
            with store._lock, closing(store._connect()) as c:
                c.execute('DELETE FROM lp_catalog_mutations WHERE token=?', (token,))
                c.commit()


@contextmanager
def topic_window(store, package_id, round_state, cancelled=lambda: False):
    """Single flight, bounded drain and build; lease TTL releases a dead process."""
    now = time.time()
    wait = max(0., min(60., float(os.getenv('SJFX_LP_TOPIC_DRAIN_SECONDS', '45'))))
    duration = max(1., min(240., float(os.getenv('SJFX_LP_TOPIC_WINDOW_SECONDS', '180'))))
    deadline = min(float(round_state['deadline']), now+duration)
    token = uuid.uuid4().hex
    context = None
    with store._lock, closing(store._connect()) as c:
        _ensure_schema(c)
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT expires,retry_at,owner_pid,owner_started,active FROM lp_catalog_windows WHERE package_id=?', (package_id,)).fetchone()
        if not cancelled() and deadline > now and not (row and (max(row[0], row[1]) > now or (row[4] and _owner_alive(row[2],row[3])))):
            import psutil
            c.execute('INSERT OR REPLACE INTO lp_catalog_windows(package_id,token,expires,retry_at,outcome,detail,owner_pid,owner_started,active) VALUES(?,?,?,?,?,?,?,?,?)',
                      (package_id, token, deadline, 0, 'draining', '{}',os.getpid(),psutil.Process().create_time(),1))
            context = {'token': token, 'deadline': deadline, 'outcome': 'failed',
                       'timings': {}, 'requested_at': now}
        c.commit()
    if context is None:
        yield None
        return
    started = time.monotonic()
    def stop():
        return cancelled() or time.time() >= deadline
    context['stop'] = stop
    ready = False
    try:
        drain_deadline = min(deadline, now+wait)
        while not stop():
            with closing(store._connect()) as c:
                package = c.execute('SELECT status,control_state FROM packages WHERE id=?', (package_id,)).fetchone()
                mutations = c.execute('SELECT 1 FROM lp_catalog_mutations WHERE package_id=? AND expires>? LIMIT 1',
                                      (package_id, time.time())).fetchone()
                jobs = c.execute("SELECT 1 FROM lp_catalog_jobs WHERE package_id=? AND status='running' LIMIT 1",
                                 (package_id,)).fetchone()
            if not package or any(v in {'paused', 'cancelled'} for v in package):
                context['outcome'] = 'cancelled'
                break
            if not mutations and not jobs:
                ready = True
                break
            if time.time() >= drain_deadline:
                context['outcome'] = 'drain_timeout'
                break
            time.sleep(min(.05, max(0, drain_deadline-time.time())))
        context['timings']['coordination_wait_seconds'] = time.monotonic()-started
        yield context if ready and not stop() else None
    finally:
        if stop() and context['outcome'] != 'completed':
            context['outcome'] = 'cancelled' if cancelled() else 'timeout'
        detail = {k: v for k, v in context.items() if k != 'stop'}
        retry = 0 if context['outcome'] in {'completed', 'unchanged'} else time.time()+max(1., min(60., float(os.getenv('SJFX_LP_TOPIC_RETRY_SECONDS', '10'))))
        with store._lock, closing(store._connect()) as c:
            c.execute('UPDATE lp_catalog_windows SET expires=0,active=0,retry_at=?,outcome=?,detail=? WHERE package_id=? AND token=?',
                      (retry, context['outcome'], json.dumps(detail, ensure_ascii=False), package_id, token))
            c.commit()


def owned(c, package_id, token):
    row = c.execute('SELECT token,expires FROM lp_catalog_windows WHERE package_id=?', (package_id,)).fetchone()
    return bool(row and row[0] == token and row[1] > time.time())
