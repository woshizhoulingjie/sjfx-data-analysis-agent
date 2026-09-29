"""Cross-process admission for generation; embeddings never use this gate.

Default priority preserves FIFO for existing calls. Only optional catalog work
uses background priority. Leases survive client instances and detect dead owners.
"""
from contextlib import contextmanager
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


_schema_lock = threading.Lock()
_schema_ready = set()
_local = threading.local()


def _db_path():
    from config import Config
    return Path(Config.DATA_DIR) / 'model_dispatch.db'


def _ensure_schema_once(key):
    """建表/建索引/切 WAL 只在每个进程内、每个库第一次连接时做一次。

    这几条语句都要取写锁；而抢名额是 50ms 一次的轮询，如果把 DDL 放在每次
    connect 里，并发时空转的连接会互相顶掉写锁，出现「什么都没干却在排队」
    甚至 SQLITE_BUSY。schema 本身幂等，缓存结果既安全，也让轮询重新廉价。
    """
    with _schema_lock:
        if key in _schema_ready:
            return
        conn = sqlite3.connect(key, timeout=15, isolation_level=None)
        try:
            conn.execute('PRAGMA busy_timeout=15000')
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('''CREATE TABLE IF NOT EXISTS admission(
                id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, priority INTEGER NOT NULL,
                created REAL NOT NULL, heartbeat REAL NOT NULL, pid INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 0)''')
            conn.execute('CREATE TABLE IF NOT EXISTS admission_cooldown(endpoint TEXT PRIMARY KEY, until_at REAL NOT NULL)')
            conn.execute('CREATE INDEX IF NOT EXISTS admission_wait ON admission(endpoint,active,priority,created)')
        finally:
            conn.close()
        _schema_ready.add(key)


def _ensure_schema(key, timeout=15.0):
    """带锁重试的建表入口。刻意不走 _conn()，避免与连接缓存互相递归。"""
    deadline = time.monotonic() + timeout
    delay = 0.005
    while True:
        try:
            return _ensure_schema_once(key)
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if 'locked' not in message and 'busy' not in message:
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(0.2, delay * 2)


def _tune(conn):
    conn.execute('PRAGMA busy_timeout=15000')
    # 名额表只是「谁正在用模型」的临时账本，不是业务数据：进程/机器重启后
    # 重新排队即可。状态盘是机械盘时，WAL + synchronous=FULL 会给每一次
    # 提交都加一次 fsync（实测 69ms）；NORMAL 是 WAL 下的常规推荐档位，
    # 崩溃后仍能保证库一致，只在掉电时可能丢最后几个名额——对闸门无害。
    conn.execute('PRAGMA synchronous=NORMAL')
    return conn


def _connect():
    """每次返回一条新的连接（外部调用方与测试用，语义与历史一致）。"""
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path)
    if key not in _schema_ready:
        # 首次（或新库）才付建表成本；失败不写进缓存，下次继续尝试。
        _ensure_schema(key)
    return _tune(sqlite3.connect(key, timeout=15, isolation_level=None))


def _conn():
    """本线程复用的连接（内部热路径）。

    闸门每处理一次模型请求都要写三行（取号 / 置 active / 释放），而「开库
    +写事务+关库」在机械盘状态目录上实测要 150ms 以上——开销几乎全在关库
    时的 checkpoint+fsync，不在事务本身。按线程复用连接后，同样的写事务只
    剩 0.24ms。连接归属线程固定，绝不跨线程共享。
    """
    path = _db_path()
    cached = getattr(_local, 'entry', None)
    if cached is not None and cached[0] == path:
        return cached[1]
    _drop_conn()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path)
    if key not in _schema_ready:
        _ensure_schema(key)
    conn = _tune(sqlite3.connect(key, timeout=15, isolation_level=None))
    _local.entry = (path, conn)
    return conn


def _drop_conn():
    """丢弃本线程缓存的连接（库换路径、连接损坏或事务残留时用）。"""
    entry = getattr(_local, 'entry', None)
    _local.entry = None
    if entry is not None:
        try:
            entry[1].close()
        except sqlite3.Error:
            pass


def _run(work, timeout=15.0):
    """在复用连接上执行一次操作，逐层兜住 SQLite 的瞬时故障。

    - BUSY/LOCKED：busy handler 对「锁升级死锁」不会等待，必须显式重试；
    - 连接被关闭/线程错配：换一条连接重来一次；
    - 其它 sqlite 错误：换掉这条连接后原样抛出，下一次调用自然自愈。
    """
    deadline = time.monotonic() + timeout
    delay = 0.005
    reconnects = 0
    while True:
        conn = _conn()
        try:
            return work(conn)
        except sqlite3.ProgrammingError:
            _drop_conn()
            reconnects += 1
            if reconnects > 1:
                raise
        except sqlite3.OperationalError as exc:
            _rollback(conn)
            message = str(exc).lower()
            if 'locked' not in message and 'busy' not in message:
                _drop_conn()
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(0.2, delay * 2)
        except sqlite3.Error:
            _rollback(conn)
            _drop_conn()
            raise


def _rollback(conn):
    try:
        conn.rollback()
    except sqlite3.Error:
        pass


def _reset_after_fork():
    """fork 出来的子进程不能继承父进程的 sqlite 连接和锁。"""
    global _local, _schema_lock, _schema_ready
    _local = threading.local()
    _schema_lock = threading.Lock()
    _schema_ready = set()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_reset_after_fork)


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True


def _concurrency_limit():
    """允许并发生成的名额数。

    默认 1 —— 与历史上的"全局单飞"行为完全一致。
    放开并发时必须同时提高 vLLM 的 --max-num-seqs，否则服务端仍会串行。
    """
    try:
        value = int(os.getenv('SJFX_GENERATION_CONCURRENCY', '1'))
    except (TypeError, ValueError):
        value = 1
    return max(1, min(8, value))


def _borrow_idle_seconds():
    """方案 §5.1 方案 B：`SJFX_GENERATION_BG_BORROW_IDLE_SECONDS`，默认 0 = 关闭。

    打开后（例如 120），若最近这么多秒内闸门上**没有出现过前台请求**
    （priority≥100，无论在等还是在跑），后台就可以用满 N 个名额，而不是 N-1 个。
    只在拿不到 vLLM 重启窗口、又想先让命名两路并发时才用：它不需要改
    `--max-num-seqs`，也不需要重启 app.py。

    代价（必须知情）：前台请求到来时如果两个名额都被后台占着，它会排在闸门最前面，
    但要等其中一个后台请求结束（单组约 7 秒，批量约 24 秒）才能进场。
    后台一旦在闸门上看见前台请求，就立即退回 N-1，不会再借。
    """
    try:
        value = float(os.getenv('SJFX_GENERATION_BG_BORROW_IDLE_SECONDS', '0') or 0)
    except (TypeError, ValueError):
        value = 0.0
    return max(0.0, min(3600.0, value))


def _background_limit(c, endpoint, slot_limit):
    """后台此刻可用的名额数。必须在 `_claim` 的同一个事务里调用。

    默认（借用关闭）恒为 `max(1, N-1)`，与改动前逐字一致，而且一条 SQL 都不多执行。
    借用打开时：闸门上只要还有前台票（在等或在跑）就不借；否则看最近一次
    "看见前台"的时刻，距今超过空闲阈值才借。"看见前台"由后台自己在轮询时记下，
    所以前台进程（app.py）不需要任何改动、也不需要重启。
    """
    base = max(1, slot_limit - 1)
    idle = _borrow_idle_seconds()
    if idle <= 0 or slot_limit <= 1:
        return base
    c.execute('CREATE TABLE IF NOT EXISTS admission_frontend_seen('
              'endpoint TEXT PRIMARY KEY, seen_at REAL NOT NULL)')
    now = time.time()
    if c.execute('SELECT 1 FROM admission WHERE endpoint=? AND priority>=100 LIMIT 1',
                 (endpoint,)).fetchone():
        c.execute('INSERT OR REPLACE INTO admission_frontend_seen VALUES(?,?)', (endpoint, now))
        return base
    row = c.execute('SELECT seen_at FROM admission_frontend_seen WHERE endpoint=?',
                    (endpoint,)).fetchone()
    if row and now - float(row[0] or 0) < idle:
        return base
    return slot_limit


def _admission_wait_limit(requested_timeout):
    """抢占名额时最长等待多久。

    原实现把等待上限硬编码成 15 秒。对长请求这是自相矛盾的：一个
    500 输出 token 的深度摘要本身要 11~16 秒（实测 2026-09-16），并发时
    第二个请求光是排队就会超过 15 秒，于是被丢弃、还触发 30 秒隔离，
    结果是"开了并发反而更慢"。
    等待上限属于**准入**预算，与 HTTP 请求超时是两件事：等不到名额只会
    快速失败，不会让已经跑起来的请求变慢，所以放宽它是安全的。
    默认跟随请求本身的超时上限，封顶 300 秒；可用
    SJFX_GENERATION_GATE_TIMEOUT_SECONDS 覆盖。
    """
    try:
        requested = int(requested_timeout)
    except (TypeError, ValueError):
        requested = 15
    try:
        configured = int(os.getenv('SJFX_GENERATION_GATE_TIMEOUT_SECONDS', '0') or 0)
    except (TypeError, ValueError):
        configured = 0
    if configured > 0:
        return max(1, min(3600, configured))
    return max(15, min(300, requested))


@contextmanager
def generation_slot(endpoint, timeout=15):
    from services.ollama import LocalModelError
    if os.getenv('SJFX_SHARED_GENERATION_GATE', '1').lower() in {'0','false','off'}:
        yield
        return
    cancel = _background.get()
    priority = 10 if cancel is not None else 100
    # 前台保留名额：后台（可选/可取消的目录命名等）最多占用 N-1 个，
    # 交互式请求可用满 N 个。这样放开并发后，用户提问仍然不会被
    # 后台批量摘要排在后面。N=1 时前后台名额都是 1，行为与改动前一致。
    slot_limit = _concurrency_limit()
    limit = slot_limit if priority >= 100 else max(1, slot_limit - 1)
    ticket = uuid.uuid4().hex
    endpoint = hashlib.sha256(endpoint.rstrip('/').encode()).hexdigest()
    start = time.monotonic()
    stopped = threading.Event()

    def heartbeat():
        # 心跳线程用自己的连接（连接不跨线程共享）；它随闸门结束而消亡。
        while not stopped.wait(2):
            try:
                _run(lambda c: c.execute(
                    'UPDATE admission SET heartbeat=? WHERE id=?', (time.time(), ticket)))
            except sqlite3.Error:
                pass

    def _quarantine(c):
        c.execute('INSERT OR REPLACE INTO admission_cooldown VALUES(?,?)',
                  (endpoint, time.time()+30))

    def _release(c):
        c.execute('BEGIN IMMEDIATE')
        c.execute('DELETE FROM admission WHERE id=?', (ticket,))
        c.commit()

    def _claim(c):
        """一次抢占尝试，返回是否拿到名额。整个事务要么提交要么回滚。"""
        c.execute('BEGIN IMMEDIATE')
        for old, pid, beat in c.execute('SELECT id,pid,heartbeat FROM admission WHERE endpoint=?', (endpoint,)).fetchall():
            if not _alive(pid) or beat < time.time()-120:
                c.execute('DELETE FROM admission WHERE id=?', (old,))
        active_count = c.execute('SELECT COUNT(*) FROM admission WHERE endpoint=? AND active=1',(endpoint,)).fetchone()[0]
        # 方案 §5.1 方案 B：后台名额按"此刻"算（借用关闭时就是上面算好的 N-1）。
        claim_limit = limit if priority >= 100 else _background_limit(c, endpoint, slot_limit)
        first = c.execute('SELECT id FROM admission WHERE endpoint=? AND active=0 ORDER BY priority DESC,created,id LIMIT 1',(endpoint,)).fetchone()
        cooldown=c.execute('SELECT until_at FROM admission_cooldown WHERE endpoint=?',(endpoint,)).fetchone()
        # A client timeout may leave server work finishing. Only optional
        # background requests observe the quarantine; frontend stays usable.
        quarantined=priority<100 and cooldown and cooldown[0]>time.time()
        allowed = active_count < claim_limit and not quarantined and first and first[0] == ticket
        if allowed:
            c.execute('UPDATE admission SET active=1 WHERE id=?',(ticket,))
        c.commit()
        return bool(allowed)

    _run(lambda c: c.execute(
        'INSERT OR REPLACE INTO admission VALUES(?,?,?,?,?,?,0)',
        (ticket,endpoint,priority,time.time(),time.time(),os.getpid())))
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        while True:
            if cancel is not None and cancel():
                raise LocalModelError('目录命名已暂停或达到预算')
            if _run(_claim):
                break
            if time.monotonic()-start >= timeout:
                raise LocalModelError('模型正在处理其他请求，请稍后重试')
            time.sleep(.05)
        try:
            yield
        except Exception as exc:
            if priority<100 and any(x in str(exc).lower() for x in ('timeout','timed out','超时')):
                _run(_quarantine)
            raise
    finally:
        stopped.set()
        thread.join(timeout=.2)
        _run(_release)


def serialized_generation(func):
    @functools.wraps(func)
    def call(self, *args, **kwargs):
        requested = kwargs.get('timeout') or self.timeout
        with generation_slot(self.base_url, _admission_wait_limit(requested)):
            return func(self,*args,**kwargs)
    return call


