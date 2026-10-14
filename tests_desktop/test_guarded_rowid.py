"""Protected views retain SQLite row IDs for real queue ordering and joins."""
import sqlite3
from unittest.mock import Mock

import pytest

from sjfx_protection import business, cache
from sjfx_protection.sql_cache import GuardedConnection, checked_connection
from sjfx_protection.store import ProtectionError


@pytest.fixture
def guarded(monkeypatch):
    monkeypatch.setattr(business, 'enabled', lambda: True)
    def require(_key, payload, **_kwargs):
        if payload['body'] == 'unapproved body':
            raise ProtectionError('unapproved body', 423, 'cache_provenance_missing')
        return []
    validator = Mock(side_effect=require)
    monkeypatch.setattr(cache, 'require_payload', validator)
    raw = sqlite3.connect(':memory:')
    raw.row_factory = sqlite3.Row
    raw.executescript('CREATE TABLE docs(id TEXT PRIMARY KEY,owner TEXT,body TEXT,status TEXT);'
                      "INSERT INTO docs(rowid,id,owner,body,status) VALUES(12,'z','alice','first','queued');"
                      "INSERT INTO docs(rowid,id,owner,body,status) VALUES(71,'a','alice','second','queued');"
                      "INSERT INTO docs(rowid,id,owner,body,status) VALUES(3,'foreign','bob','private','queued');")
    def wrap(db):
        return GuardedConnection(db, 'isolated-rowid', {'docs': {'content': ('body',), 'identity': ('owner',)}},
                                 lambda _db, _table, row: [{'owner': 'alice', 'scope': 'scan'}]
                                 if row['owner'] == 'alice' else [])
    guard = wrap(raw)
    yield guard, validator, wrap
    guard.rollback()
    guard.cleanup()
    raw.close()


def test_queue_select_star_orders_by_actual_rowid_and_keeps_original_columns(guarded):
    guard, validator, _wrap = guarded
    rows = guard.execute('SELECT * FROM docs WHERE status=? ORDER BY rowid', ('queued',)).fetchall()
    assert [row['id'] for row in rows] == ['z', 'a']
    assert rows[0].keys()[:4] == ['id', 'owner', 'body', 'status']
    assert set(rows[0].keys()[4:]) <= {'rowid', '_rowid_', 'oid'}
    assert rows[0]['rowid'] == 12 and rows[1]['rowid'] == 71
    assert validator.call_count == 2


@pytest.mark.parametrize('alias', ['rowid', '_rowid_', 'oid'])
def test_qualified_rowid_alias_ordering(guarded, alias):
    guard, _validator, _wrap = guarded
    rows = guard.execute(f'SELECT d.id,d.body,d.{alias} AS sequence FROM docs d ORDER BY d.{alias} DESC').fetchall()
    assert [(row['id'], row['sequence']) for row in rows] == [('a', 71), ('z', 12)]


def test_correlated_active_rowid_join_preserves_queue_semantics(guarded):
    guard, _validator, _wrap = guarded
    row = guard.execute('SELECT d.id,d.body,active.id AS active_id FROM docs d JOIN docs active '
                        'ON active.rowid=(SELECT j.rowid FROM docs j WHERE j.owner=d.owner '
                        "ORDER BY j.rowid DESC LIMIT 1) WHERE d.id='z'").fetchone()
    assert dict(row) == {'id': 'z', 'body': 'first', 'active_id': 'a'}


def test_rowid_query_keeps_content_provenance_gate(guarded):
    guard, _validator, _wrap = guarded
    guard.raw.execute("UPDATE docs SET body='unapproved body' WHERE id='z'")
    guard.raw.execute('DELETE FROM _sjfx_content_changes')
    guard.raw.commit()
    with pytest.raises(ProtectionError, match='unapproved body'):
        guard.execute('SELECT * FROM docs ORDER BY rowid').fetchall()


@pytest.mark.parametrize('declared', ['rowid', '_rowid_', 'oid'])
def test_declared_rowid_alias_columns_are_not_overwritten(guarded, declared):
    _guard, _validator, wrap = guarded
    raw = sqlite3.connect(':memory:')
    raw.row_factory = sqlite3.Row
    raw.execute(f'CREATE TABLE docs(id TEXT PRIMARY KEY,owner TEXT,body TEXT,status TEXT,"{declared}" TEXT)')
    raw.execute('INSERT INTO docs VALUES(?,?,?,?,?)', ('id', 'alice', 'approved', 'queued', 'declared-value'))
    guard = wrap(raw)
    try:
        row = guard.execute('SELECT * FROM docs').fetchone()
        assert row[declared] == 'declared-value'
        assert row.keys()[:5] == ['id', 'owner', 'body', 'status', declared]
        for alias in {'rowid', '_rowid_', 'oid'} - {declared}:
            assert row[alias] == 1
    finally:
        guard.rollback()
        guard.cleanup()
        raw.close()


@pytest.mark.parametrize('declared', ['rowid', '_rowid_', 'oid'])
def test_shadowed_alias_triggers_journal_the_physical_row(guarded, declared):
    _guard, _validator, wrap = guarded
    raw = sqlite3.connect(':memory:')
    raw.row_factory = sqlite3.Row
    raw.execute(f'CREATE TABLE docs(id TEXT PRIMARY KEY,owner TEXT,body TEXT,status TEXT,"{declared}" TEXT)')
    guard = wrap(raw)
    try:
        raw.execute('INSERT INTO docs VALUES(?,?,?,?,?)', ('id', 'alice', 'approved', 'queued', 'declared-value'))
        assert raw.execute('SELECT row_id FROM _sjfx_content_changes').fetchone()[0] == 1
        raw.execute('DELETE FROM _sjfx_content_changes')
        raw.execute("UPDATE docs SET body='updated' WHERE id='id'")
        assert raw.execute('SELECT row_id FROM _sjfx_content_changes').fetchone()[0] == 1
        assert guard._candidate_rows('docs')[0]['__rowid__'] == 1
    finally:
        guard.rollback()
        guard.cleanup()
        raw.close()


def test_inaccessible_row_identity_fails_closed(guarded):
    _guard, _validator, wrap = guarded
    raw = sqlite3.connect(':memory:')
    raw.execute('CREATE TABLE docs(id TEXT PRIMARY KEY,owner TEXT,body TEXT,rowid TEXT,_rowid_ TEXT,oid TEXT)')
    try:
        with pytest.raises(ProtectionError, match='SQLite 行标识'):
            wrap(raw)
    finally:
        raw.close()


@pytest.mark.parametrize('populated', [False, True])
def test_without_rowid_never_admits_another_owners_actual_body(guarded, populated):
    _guard, validator, _wrap = guarded
    raw = sqlite3.connect(':memory:')
    raw.row_factory = sqlite3.Row
    raw.execute('CREATE TABLE docs(id TEXT PRIMARY KEY,owner TEXT,body TEXT) WITHOUT ROWID')
    if populated:
        raw.executemany('INSERT INTO docs VALUES(?,?,?)', [('a', 'alice', 'approved'), ('b', 'bob', 'private')])
    raw.commit()
    validator.reset_mock()
    try:
        with pytest.raises(ProtectionError, match='SQLite 行标识'):
            with checked_connection(raw, 'isolated-rowid', {'docs': {'content': ('body',), 'identity': ('owner',)}},
                                    lambda _db, _table, row: [{'owner': 'alice', 'scope': 'scan'}]
                                    if row['owner'] == 'alice' else []) as guard:
                guard.execute('SELECT id,owner,body FROM docs').fetchall()
        validator.assert_not_called()
    finally:
        raw.close()


@pytest.mark.parametrize('declared', ['rowid', '_rowid_', 'oid'])
def test_generated_rowid_alias_gates_each_owners_actual_body(guarded, declared):
    _guard, validator, wrap = guarded
    raw = sqlite3.connect(':memory:')
    raw.row_factory = sqlite3.Row
    raw.execute(f'CREATE TABLE docs(id TEXT PRIMARY KEY,owner TEXT,body TEXT,"{declared}" TEXT '
                "GENERATED ALWAYS AS ('same') VIRTUAL)")
    raw.executemany('INSERT INTO docs(id,owner,body) VALUES(?,?,?)',
                    [('a', 'alice', 'approved'), ('b', 'bob', 'private')])
    raw.commit()
    validator.reset_mock()
    guard = wrap(raw)
    try:
        rows = guard.execute('SELECT * FROM docs ORDER BY id').fetchall()
        assert [(row['id'], row['owner'], row['body']) for row in rows] == [('a', 'alice', 'approved')]
        assert rows[0][declared] == 'same'
        assert [call.args[1]['body'] for call in validator.call_args_list] == ['approved']
    finally:
        guard.rollback()
        guard.cleanup()
        raw.close()
