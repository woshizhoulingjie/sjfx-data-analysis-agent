"""Live connection references must not pin a stopped Windows instance DB."""
import sqlite3

import pytest

from services.mail.bootstrap import build_mail_services
from services.mail import network_service as graph
from services.storage import Storage


def test_storage_transactions_close_live_connection_on_success_and_failure(tmp_path):
    store = Storage(tmp_path / 'state.db')
    with store._connect() as connection:
        connection.execute('CREATE TABLE handle_probe(value TEXT)')
        connection.execute("INSERT INTO handle_probe VALUES('committed')")
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connection.execute('SELECT 1')
    with pytest.raises(RuntimeError, match='rollback'):
        with store._connect() as failed_connection:
            failed_connection.execute("INSERT INTO handle_probe VALUES('discarded')")
            raise RuntimeError('rollback')
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        failed_connection.execute('SELECT 1')
    with store._connect() as check:
        assert [r[0] for r in check.execute('SELECT value FROM handle_probe')] == ['committed']
    # Strong references above remain alive. No garbage collection or deletion
    # retries may be needed to release the actual database handle.
    moved = tmp_path / 'renamed.db'
    (tmp_path / 'state.db').replace(moved)
    moved.unlink()


def test_mail_shutdown_releases_watcher_and_views_only_for_its_database(tmp_path):
    services = build_mail_services(Storage(tmp_path / 'mail.db'))
    other = build_mail_services(Storage(tmp_path / 'other.db'))
    with graph._LOCK:
        revision = graph._watch(services.repository)
        other_revision = graph._watch(other.repository)
        connection = graph._WATCHERS[revision[0]][0]
        other_connection = graph._WATCHERS[other_revision[0]][0]
        graph._CACHE[(revision[0], 'owner', 'package')] = (revision, {})
        graph._CACHE[(other_revision[0], 'owner', 'package')] = (other_revision, {})
    try:
        services.close()
        services.close()  # Shutdown is idempotent, including absent watchers.
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            connection.execute('SELECT 1')
        assert revision[0] not in graph._WATCHERS
        assert not any(k[0] == revision[0] for k in graph._CACHE)
        assert other_connection.execute('SELECT 1').fetchone()[0] == 1
        assert any(k[0] == other_revision[0] for k in graph._CACHE)
        moved = tmp_path / 'mail-renamed.db'
        (tmp_path / 'mail.db').replace(moved)
        moved.unlink()
    finally:
        services.close()
        other.close()


def test_mail_repository_transaction_closes_after_rollback(tmp_path):
    services = build_mail_services(Storage(tmp_path / 'mail.db'))
    services.repository.migrate()
    with pytest.raises(RuntimeError, match='rollback'):
        with services.repository._connection() as connection:
            connection.execute("INSERT INTO mail_packages(package_id, owner_id, display_name, source_kind, source_path, source_revision, status, config_json) VALUES('rolled-back','owner','test','directory','/fixture','v1','inventory','{}')")
            raise RuntimeError('rollback')
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connection.execute('SELECT 1')
    with services.repository._connection() as check:
        assert check.execute("SELECT COUNT(*) FROM mail_packages WHERE package_id='rolled-back'").fetchone()[0] == 0
    services.close()
