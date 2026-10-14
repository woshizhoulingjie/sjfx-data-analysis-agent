"""Translation checkpoints and publication against isolated, real SQLite/FTS."""
from contextlib import contextmanager
from copy import deepcopy
import json
import re
import sqlite3
from unittest.mock import patch

import pytest

from services.storage import Storage


@pytest.fixture
def storage(tmp_path):
    instance = Storage(tmp_path / "state.db", sidecar_dir=tmp_path / "sidecars")
    original = instance._connect

    @contextmanager
    def connect(*args, **kwargs):
        with original(*args, **kwargs) as conn:
            conn.execute("PRAGMA temp_store=MEMORY")
            yield conn

    instance._connect = connect
    return instance


@pytest.fixture
def fts_storage(storage):
    if not storage.evidence_fts_available:
        pytest.skip("this SQLite build lacks FTS5")
    return storage


def translation(*sources, status="completed"):
    sources = sources or ("The original delivery decision was approved.",)
    return {
        "status": status, "source_fingerprint": "source-v1",
        "source_language": "en", "target_language": "zh-CN",
        "units": [{
            "unit_id": "U%s" % number, "status": "completed", "kind": "body",
            "source_text": source, "target_text": "第%s项交付决定已获批准。" % number,
            "section": "Decision", "start": number * 100, "end": number * 100 + len(source),
        } for number, source in enumerate(sources, 1)],
    }


def original_evidence(number, path=None, text=None):
    return {
        "evidence_id": "E%s" % number,
        "source_path": path or "history/%s.txt" % number,
        "section": "Methods", "text": text or "historical shared catalog evidence %s" % number,
    }


def catalog_snapshot(storage, scan_id=None):
    where, values = (" WHERE scan_id=?", (scan_id,)) if scan_id else ("", ())
    with storage._connect() as conn:
        main = [tuple(row) for row in conn.execute(
            "SELECT rowid,scan_id,index_key,source_path,archive_source_path,payload FROM evidence_index"
            + where + " ORDER BY scan_id,index_key", values)]
        fts = [tuple(row) for row in conn.execute(
            "SELECT rowid,scan_id,index_key,source_path,archive_source_path,section,text FROM evidence_fts"
            + where + " ORDER BY scan_id,index_key", values)] if storage.evidence_fts_available else None
    return main, fts


def assert_paired(storage, scan_id, count):
    main, fts = catalog_snapshot(storage, scan_id)
    assert len(main) == len(fts) == count
    assert {(row[1], row[2]) for row in main} == {(row[1], row[2]) for row in fts}
    assert len({row[2] for row in fts}) == count
    fts_by_key = {row[2]: row for row in fts}
    for row in main:
        payload = json.loads(row[5])
        if payload.get("index_kind") == "translation":
            assert fts_by_key[row[2]][6] == payload["original_text"] + "\n" + payload["translated_text"]


@contextmanager
def traced_connections(storage, authorizer=None):
    original = storage._connect
    trace = []

    @contextmanager
    def connect(*args, **kwargs):
        with original(*args, **kwargs) as conn:
            # Older Python/sqlite wrappers require a hashable callback.
            conn.set_trace_callback(lambda sql: trace.append(sql))
            if authorizer is not None:
                conn.set_authorizer(authorizer)
            yield conn

    with patch.object(storage, "_connect", connect):
        yield trace


def fts_lookups(trace):
    return [sql for sql in trace if re.match(
        r"SELECT\s+rowid(?:\s*,\s*index_key)?\s+FROM\s+evidence_fts\b", sql.strip(), re.I)]


def deleted_fts_rowids(trace):
    deletes = [sql for sql in trace if sql.lstrip().upper().startswith("DELETE FROM EVIDENCE_FTS")]
    rowids = []
    for sql in deletes:
        match = re.fullmatch(r"DELETE FROM evidence_fts WHERE rowid\s*=\s*(\d+)\s*", sql.strip(), re.I)
        assert match is not None, "FTS deletion must use the previously resolved rowid: " + sql
        rowids.append(int(match.group(1)))
    return rowids


@pytest.mark.parametrize("published", [False, True])
def test_checkpoint_saves_state_without_reading_or_mutating_evidence(storage, published):
    storage.replace_evidence_index("history", [original_evidence(1)])
    initial = translation()
    if published:
        storage.save_translation("target", "papers/decision.txt", initial)
    before = catalog_snapshot(storage)
    checkpoint = translation("The checkpoint has a new source.", status="partial")
    checkpoint["units"].append({"unit_id": "U2", "status": "pending", "source_text": "Still pending."})
    checkpoint["progress"] = {"completed_units": 1, "required_units": 2}
    denied = []

    def deny_evidence_access(action, table, _column, _database, _trigger):
        if table in {"evidence_index", "evidence_fts"}:
            denied.append((action, table))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with traced_connections(storage, deny_evidence_access) as trace:
        storage.save_translation("target", "papers/decision.txt", checkpoint, index_evidence=False)
    assert not denied
    assert not any("evidence_index" in sql.lower() or "evidence_fts" in sql.lower() for sql in trace)
    assert storage.get_translation("target", "papers/decision.txt") == checkpoint
    assert storage.translation_counts("target") == {"partial": 1}
    assert catalog_snapshot(storage) == before


def test_first_translation_publication_does_not_read_or_delete_historical_fts(fts_storage):
    storage = fts_storage
    history = [original_evidence(i, text=("historical catalog words " * 32) + str(i)) for i in range(1024)]
    storage.replace_evidence_index("history", history)
    storage.save_translation("history", "papers/decision.txt", translation())
    storage.replace_evidence_index("target", [original_evidence(1, "papers/decision.txt")])
    before = catalog_snapshot(storage)
    denied = []

    def deny_fts_read_or_delete(action, table, _column, _database, _trigger):
        if table == "evidence_fts" and action in {sqlite3.SQLITE_READ, sqlite3.SQLITE_DELETE}:
            denied.append(action)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    payload = translation()
    with traced_connections(storage, deny_fts_read_or_delete) as trace:
        storage.save_translation("target", "papers/decision.txt", payload)
    assert not denied
    assert not fts_lookups(trace)
    assert not deleted_fts_rowids(trace)
    after = catalog_snapshot(storage)
    assert all(row in after[0] for row in before[0])
    assert all(row in after[1] for row in before[1])
    assert_paired(storage, "target", 2)
    assert storage.get_translation("target", "papers/decision.txt") == payload
    hits = storage.search_evidence_index("target", "交付决定", limit=20)
    assert any(hit.get("translated_text") == payload["units"][0]["target_text"] for hit in hits)


def test_same_indexed_content_updates_state_without_rebuilding_fts(fts_storage):
    storage = fts_storage
    payload = translation(status="partial")
    storage.save_translation("target", "papers/decision.txt", payload)
    before = catalog_snapshot(storage)
    final = deepcopy(payload)
    final.update(status="completed", provider_id="local", progress={"completed_units": 1, "required_units": 1})
    denied = []

    def deny_index_mutation(action, table, _column, _database, _trigger):
        if (table in {"evidence_index", "evidence_fts"} and
                action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}):
            denied.append((action, table))
            return sqlite3.SQLITE_DENY
        if table == "evidence_fts" and action == sqlite3.SQLITE_READ:
            denied.append((action, table))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with traced_connections(storage, deny_index_mutation) as trace:
        storage.save_translation("target", "papers/decision.txt", final)
    assert not denied
    assert not any("evidence_fts" in sql.lower() for sql in trace)
    assert catalog_snapshot(storage) == before
    assert storage.get_translation("target", "papers/decision.txt") == final
    assert storage.translation_counts("target") == {"completed": 1}


def test_changed_quoted_original_uses_match_and_deletes_only_exact_translation_rowids(fts_storage):
    storage = fts_storage
    path = "papers/decision.txt"
    quoted = 'Alice said "approved" for the obsolete delivery decision.'
    old = translation(quoted, "The unchanged second paragraph retains context.", "...?!")
    storage.replace_evidence_index("target", [original_evidence(1, path, quoted)])
    storage.save_translation("target", path, old)
    storage.save_translation("target", "papers/other.txt", old)
    storage.save_translation("other-scan", path, old)
    before = catalog_snapshot(storage)
    changed_keys = {row[2] for row in before[0] if row[1] == "target" and row[3] == path
                    and json.loads(row[5]).get("evidence_id") == "TR-U1"}
    removed_rows = [row for row in before[1] if row[1] == "target" and row[2] in changed_keys]
    retained_main = [row for row in before[0] if not (row[1] == "target" and row[2] in changed_keys)]
    retained_fts = [row for row in before[1] if row not in removed_rows]
    new = deepcopy(old)
    new["units"][0].update(source_text='Alice said "rejected" for the revised delivery decision.',
                           target_text="爱丽丝拒绝了修订后的交付决定。")
    with traced_connections(storage) as trace:
        storage.save_translation("target", path, new)
    lookups = fts_lookups(trace)
    assert len(lookups) == len(removed_rows)
    assert all("evidence_fts MATCH" in sql and "AND scan_id='target'" in sql
               and "AND source_path='papers/decision.txt'" in sql and "AND index_key=" in sql for sql in lookups)
    assert any('"Alice said ""approved"" for the obsolete delivery decision."' in sql for sql in lookups)
    assert sorted(deleted_fts_rowids(trace)) == sorted(row[0] for row in removed_rows)
    after = catalog_snapshot(storage)
    assert all(row in after[0] for row in retained_main)
    assert all(row in after[1] for row in retained_fts)
    assert_paired(storage, "target", 7)
    assert_paired(storage, "other-scan", 3)
    target_translations = [json.loads(row[5]) for row in after[0] if row[1] == "target"
                           and row[3] == path and row[2].startswith("translation:")]
    assert {row["original_text"] for row in target_translations} == {unit["source_text"] for unit in new["units"]}
    assert storage.get_translation("target", path) == new


def test_two_legacy_punctuation_units_need_only_one_file_level_fts_lookup(fts_storage):
    storage = fts_storage
    path = "papers/punctuation.txt"
    # Older versions could publish these units even though their originals have
    # no FTS tokens; the translated content still makes the FTS rows nonempty.
    old = translation("...?!", '"()[]?!"')
    storage.replace_evidence_index("target", [original_evidence(1, path, "Original context survives.")])
    storage.save_translation("target", path, old)
    storage.save_translation("other-scan", path, old)
    before = catalog_snapshot(storage)
    removed_rows = [row for row in before[1] if row[1] == "target" and row[2].startswith("translation:")]
    new = translation("The punctuation source is now corrected.")
    with traced_connections(storage) as trace:
        storage.save_translation("target", path, new)
    lookups = fts_lookups(trace)
    assert len(lookups) == 1
    assert "MATCH" not in lookups[0]
    assert "scan_id='target'" in lookups[0] and "source_path='papers/punctuation.txt'" in lookups[0]
    assert "index_key LIKE 'translation:%'" in lookups[0]
    assert sorted(deleted_fts_rowids(trace)) == sorted(row[0] for row in removed_rows)
    after = catalog_snapshot(storage)
    assert all(row in after[0] for row in before[0] if row[1] == "other-scan" or not row[2].startswith("translation:"))
    assert all(row in after[1] for row in before[1] if row not in removed_rows)
    assert_paired(storage, "target", 2)
    assert_paired(storage, "other-scan", 2)


def test_resumed_partial_adds_three_units_without_reading_or_deleting_fts(fts_storage):
    storage = fts_storage
    path = "papers/resumed.txt"
    final = translation("...?!", "The earlier translated unit remains unchanged.",
                        "The first resumed unit adds a result.",
                        "The second resumed unit adds a method.",
                        "The third resumed unit adds a conclusion.")
    partial = deepcopy(final)
    partial["status"] = "partial"
    for unit in partial["units"][2:]:
        unit["status"] = "pending"
    storage.replace_evidence_index("target", [original_evidence(1, path)])
    storage.save_translation("target", path, partial)
    before = catalog_snapshot(storage)
    denied = []

    def deny_existing_index_read_or_delete(action, table, _column, _database, _trigger):
        if ((table == "evidence_fts" and action in {sqlite3.SQLITE_READ, sqlite3.SQLITE_DELETE}) or
                (table == "evidence_index" and action in {sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE})):
            denied.append((action, table))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with traced_connections(storage, deny_existing_index_read_or_delete) as trace:
        storage.save_translation("target", path, final)
    assert not denied
    assert not fts_lookups(trace)
    assert not deleted_fts_rowids(trace)
    assert len([sql for sql in trace if sql.startswith("INSERT INTO evidence_index(")]) == 3
    assert len([sql for sql in trace if sql.startswith("INSERT INTO evidence_fts(")]) == 3
    after = catalog_snapshot(storage)
    assert all(row in after[0] for row in before[0])
    assert all(row in after[1] for row in before[1])
    assert_paired(storage, "target", 6)
    assert storage.get_translation("target", path) == final


def test_changed_punctuation_fallback_retains_unchanged_translation_rowids(fts_storage):
    storage = fts_storage
    path = "papers/punctuation.txt"
    old = translation("...?!", '"()[]?!"', "The unchanged source remains available.")
    storage.replace_evidence_index("target", [original_evidence(1, path)])
    storage.save_translation("target", path, old)
    storage.save_translation("other-scan", path, old)
    before = catalog_snapshot(storage)
    changed_keys = {row[2] for row in before[0] if row[1] == "target" and row[3] == path
                    and json.loads(row[5]).get("evidence_id") == "TR-U1"}
    removed_rows = [row for row in before[1] if row[1] == "target" and row[2] in changed_keys]
    retained_main = [row for row in before[0] if not (row[1] == "target" and row[2] in changed_keys)]
    retained_fts = [row for row in before[1] if row not in removed_rows]
    new = deepcopy(old)
    new["units"][0]["target_text"] = "第一项标点译文已修订。"
    with traced_connections(storage) as trace:
        storage.save_translation("target", path, new)
    lookups = fts_lookups(trace)
    assert len(lookups) == 1
    assert lookups[0].startswith("SELECT rowid,index_key FROM evidence_fts")
    assert "MATCH" not in lookups[0]
    assert sorted(deleted_fts_rowids(trace)) == sorted(row[0] for row in removed_rows)
    main_deletes = [sql for sql in trace if sql.startswith("DELETE FROM evidence_index")]
    assert len(main_deletes) == 1
    assert "AND index_key='" in main_deletes[0] and "LIKE" not in main_deletes[0]
    after = catalog_snapshot(storage)
    assert all(row in after[0] for row in retained_main)
    assert all(row in after[1] for row in retained_fts)
    assert_paired(storage, "target", 4)
    assert_paired(storage, "other-scan", 3)
    assert storage.get_translation("target", path) == new


@pytest.mark.parametrize("append_only", [False, True])
def test_incremental_fts_insert_failure_rolls_back_state_and_both_indexes(fts_storage, append_only):
    storage = fts_storage
    path = "papers/decision.txt"
    old = translation("...?!", "The original unit has a stable decision.", status="partial")
    storage.save_translation("target", path, old)
    before = catalog_snapshot(storage)
    if append_only:
        new = translation("...?!", "The original unit has a stable decision.", "The resumed unit adds evidence.")
    else:
        new = deepcopy(old)
        new["status"] = "completed"
        new["units"][1]["target_text"] = "已修订第二项决定。"

    def deny_fts_insert(action, table, _column, _database, _trigger):
        return (sqlite3.SQLITE_DENY if table == "evidence_fts" and action == sqlite3.SQLITE_INSERT
                else sqlite3.SQLITE_OK)

    with traced_connections(storage, deny_fts_insert) as trace:
        with pytest.raises(sqlite3.DatabaseError):
            storage.save_translation("target", path, new)
    assert any(sql.upper() == "ROLLBACK" for sql in trace)
    assert catalog_snapshot(storage) == before
    assert storage.get_translation("target", path) == old
    assert storage.evidence_fts_available
    assert_paired(storage, "target", 2)


def test_no_fts_checkpoint_publish_reuse_and_replace_never_issue_fts_sql(storage):
    if storage.evidence_fts_available:
        with storage._connect() as conn:
            conn.execute("DROP TABLE evidence_fts")
    storage.evidence_fts_available = False
    storage.replace_evidence_index("history", [original_evidence(1)])
    before_history = catalog_snapshot(storage, "history")
    path = "papers/decision.txt"
    partial = translation(status="partial")
    final = deepcopy(partial)
    final["status"] = "completed"
    changed = translation('A revised "quoted" source has a different outcome.')
    changed["units"][0]["target_text"] = "修改后的结论得到确认。"
    with traced_connections(storage) as trace:
        storage.save_translation("target", path, partial, index_evidence=False)
        assert storage.list_evidence_index("target") == []
        storage.save_translation("target", path, final)
        published = catalog_snapshot(storage, "target")
        storage.save_translation("target", path, deepcopy(final))
        assert catalog_snapshot(storage, "target") == published
        storage.save_translation("target", path, changed)
    assert not any("evidence_fts" in sql.lower() for sql in trace)
    assert catalog_snapshot(storage, "history") == before_history
    assert storage.get_translation("target", path) == changed
    entries = storage.list_evidence_index("target")
    assert len(entries) == 1
    assert entries[0]["original_text"] == changed["units"][0]["source_text"]
    assert entries[0]["translated_text"] == changed["units"][0]["target_text"]
    assert storage.search_evidence_index("target", "修改后的结论", limit=20)
