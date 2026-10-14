"""Runtime SQLite operations release handles deterministically on every exit.

Keep strong references to every connection so garbage collection cannot hide a
leak. Database rename/removal also exercises the native Windows file locks.
"""
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from config import Config
from services.large_package_isolated import LargePackageStore
from services.large_qa_index import LargeQAIndex
from services.large_qa_search import current_paths
from services.mail.translation_quality import SegmentMemory
from services.storage import Storage
from scripts.large_qa_worker import ReadOnlyStore, foreground_busy


@pytest.fixture
def tracked_connections(monkeypatch):
    original = sqlite3.connect
    connections = []
    fail = {"sql": None}

    def connect(*args, **kwargs):
        base = kwargs.pop("factory", sqlite3.Connection)

        class TrackedConnection(base):
            def execute(self, sql, *args, **kwargs):
                if fail["sql"] and sql.startswith(fail["sql"]):
                    raise sqlite3.OperationalError("injected setup failure")
                return super().execute(sql, *args, **kwargs)

        conn = original(*args, factory=TrackedConnection, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    return connections, fail


def assert_closed(connections):
    assert connections
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def make_store(kind, root):
    return (Storage(root / "state.db", root / "payloads") if kind == "ordinary"
            else LargePackageStore(root / "large.db"))


@pytest.mark.parametrize("kind", ["ordinary", "large"])
@pytest.mark.parametrize("failure", [None, RuntimeError, KeyboardInterrupt])
def test_transactions_keep_commit_rollback_and_close_semantics(tmp_path, tracked_connections, kind, failure):
    connections, _ = tracked_connections
    store = make_store(kind, tmp_path)
    assert_closed(connections)  # Includes all initialization/migration connections.
    with store._connect() as conn:
        conn.execute("CREATE TABLE lifecycle_test(value INTEGER)")
    try:
        with store._connect() as conn:
            conn.execute("BEGIN")
            conn.execute("INSERT INTO lifecycle_test VALUES(1)")
            if failure:
                raise failure("injected transaction failure")
    except BaseException as exc:
        assert failure and isinstance(exc, failure)
    assert_closed(connections)
    with store._connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM lifecycle_test").fetchone()[0]
    assert count == (0 if failure else 1)
    assert_closed(connections)
    path = Path(store.db_path)
    moved = path.with_suffix(".released")
    path.rename(moved)
    moved.unlink()


def test_large_package_http_reads_and_worker_updates_leave_no_open_connections(tmp_path, tracked_connections, monkeypatch):
    connections, _ = tracked_connections
    monkeypatch.setattr(Config, "SCAN_ALLOWED_ROOTS", ())
    store = LargePackageStore(tmp_path / "large.db")
    package = store.create(tmp_path, "owner")
    package_id = package["id"]
    store.upsert_files(package_id, [{"path": "a.txt", "name": "a.txt", "size": 8}])
    for _ in range(10):
        assert store.get(package_id, "owner")["id"] == package_id
        assert store.list_history("owner")["items"]
        assert current_paths(store, package_id, {"a.txt"}) == {"a.txt"}
        assert_closed(connections)
    store.claim("quick", package_id=package_id)
    assert_closed(connections)
    moved = tmp_path / "released.db"
    Path(store.db_path).rename(moved)
    moved.unlink()


@pytest.mark.parametrize("kind", ["ordinary", "large"])
def test_commit_failure_rolls_back_and_closes_connection(tmp_path, tracked_connections, kind):
    connections, _ = tracked_connections
    store = make_store(kind, tmp_path)
    with store._connect() as conn:
        conn.execute("CREATE TABLE lifecycle_parent(id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE lifecycle_child(parent INTEGER REFERENCES lifecycle_parent(id) "
                     "DEFERRABLE INITIALLY DEFERRED)")
    with pytest.raises(sqlite3.IntegrityError):
        with store._connect() as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN")
            conn.execute("INSERT INTO lifecycle_child VALUES(123)")
    assert_closed(connections)
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM lifecycle_child").fetchone()[0] == 0
    assert_closed(connections)


@pytest.mark.parametrize("kind", ["ordinary", "large", "qa", "memory"])
def test_connection_setup_failure_also_releases_handle(tmp_path, tracked_connections, kind):
    connections, fail = tracked_connections
    if kind in {"ordinary", "large"}:
        store = make_store(kind, tmp_path)
        open_context = store._connect
    elif kind == "qa":
        store = LargeQAIndex("package", base=tmp_path / "qa", create=True)
        open_context = store.connect
    else:
        store = SegmentMemory(tmp_path / "memory.db", "test", "offline-model")
        open_context = None
    if connections:
        assert_closed(connections)
    fail["sql"] = ("CREATE TABLE IF NOT EXISTS translations" if kind == "memory"
                   else "PRAGMA busy_timeout")
    try:
        with pytest.raises(sqlite3.OperationalError, match="injected setup failure"):
            if kind == "memory":
                store.connection()
            else:
                with open_context():
                    pytest.fail("failed connection setup must not enter the body")
    finally:
        fail["sql"] = None
    assert_closed(connections)


def qa_source(path):
    with closing(sqlite3.connect(str(path))) as conn, conn:
        conn.executescript("""
            CREATE TABLE packages(id TEXT PRIMARY KEY,root_path TEXT,owner_id TEXT,created_at REAL);
            INSERT INTO packages VALUES('package','/synthetic','owner',0);
            CREATE TABLE analysis_jobs(status TEXT);
            CREATE TABLE conversation_turns(status TEXT);
        """)


@pytest.mark.parametrize("failure", [None, "body", "setup"])
def test_read_only_qa_source_closes_on_all_exit_paths(tmp_path, tracked_connections, failure):
    connections, fail = tracked_connections
    path = tmp_path / "只读 源.db"
    qa_source(path)
    store = ReadOnlyStore(path)
    if failure == "setup":
        fail["sql"] = "PRAGMA query_only"
    try:
        try:
            with store._connect() as conn:
                assert conn.in_transaction
                assert conn.execute("SELECT id FROM packages").fetchone()[0] == "package"
                if failure == "body":
                    raise RuntimeError("injected read failure")
                with pytest.raises(sqlite3.OperationalError, match="readonly"):
                    conn.execute("DELETE FROM packages")
        except (RuntimeError, sqlite3.OperationalError) as exc:
            assert failure and "injected" in str(exc)
    finally:
        fail["sql"] = None
    assert_closed(connections)
    moved = path.with_suffix(".released")
    path.rename(moved)
    moved.unlink()


@pytest.mark.parametrize("state", ["idle", "running", "failed_read"])
def test_qa_foreground_poll_closes_every_connection(tmp_path, tracked_connections, monkeypatch, state):
    connections, fail = tracked_connections
    path = tmp_path / "foreground.db"
    qa_source(path)
    monkeypatch.setattr(Config, "DB_PATH", path)
    if state == "running":
        with closing(sqlite3.connect(str(path))) as conn, conn:
            conn.execute("INSERT INTO analysis_jobs VALUES('running')")
    if state == "failed_read":
        fail["sql"] = "SELECT 1 FROM analysis_jobs"
    try:
        for _ in range(10):
            assert foreground_busy() == (state != "idle")
            assert_closed(connections)
    finally:
        fail["sql"] = None
    assert_closed(connections)
