"""Real isolated SQLite publication checks; no app, model or server setup."""
from contextlib import contextmanager
import ast
import json
from pathlib import Path
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from services.storage import Storage
from services.parse_isolation import ParseIsolationCancelled
from services.retrieval import evidence_corpus


@pytest.fixture
def storage(tmp_path):
    # Same isolated Storage/temp-database pattern as storage lifecycle/safety tests.
    instance = Storage(tmp_path / "agent.db", tmp_path / "sidecars")
    connect = instance._connect

    @contextmanager
    def isolated_connect(*args, **kwargs):
        with connect(*args, **kwargs) as conn:
            # Avoid host temporary-file/path failures in SQLite sorting/FTS.
            # The tested database and WAL remain real files; no product setting changes.
            conn.execute("PRAGMA temp_store=MEMORY")
            yield conn

    instance._connect = isolated_connect
    return instance


@pytest.fixture
def fts_storage(storage):
    if not storage.evidence_fts_available:
        pytest.skip("this SQLite build lacks FTS5")
    return storage


def evidence(number, text=None):
    return {"evidence_id": "E%s" % number, "source_path": "papers/%s.txt" % number,
            "archive_source_path": "bundle.zip", "section": "Methods",
            "content_sha256": "source-%s" % number,
            "text": text or "publication evidence token%s" % number}


def translation(storage, scan_id="target"):
    payload = {"status": "completed", "source_fingerprint": "original-source",
               "source_language": "en", "target_language": "zh-CN", "units": [{
                   "unit_id": "u1", "status": "completed", "kind": "body",
                   "source_text": "Retained translated evidence", "target_text": "应保留的翻译证据",
                   "start": 0, "end": 28, "section": "Methods"}]}
    storage.save_translation(scan_id, "papers/1.txt", payload)
    return payload


def catalog_snapshot(storage, scan_id=None):
    where, values = (" WHERE scan_id=?", (scan_id,)) if scan_id else ("", ())
    with storage._connect() as conn:
        main = [tuple(row) for row in conn.execute(
            "SELECT scan_id,index_key,source_path,archive_source_path,payload FROM evidence_index"
            + where + " ORDER BY scan_id,index_key", values)]
        fts = [tuple(row) for row in conn.execute(
            "SELECT scan_id,index_key,source_path,archive_source_path,section,text FROM evidence_fts"
            + where + " ORDER BY scan_id,index_key", values)] if storage.evidence_fts_available else None
    return main, fts


@contextmanager
def traced_connections(storage, authorizer=None):
    original = storage._connect
    trace = []

    @contextmanager
    def connect(*args, **kwargs):
        with original(*args, **kwargs) as conn:
            conn.set_trace_callback(trace.append)
            if authorizer:
                conn.set_authorizer(authorizer)
            yield conn

    with patch.object(storage, "_connect", connect):
        yield trace


def fts_deletes(trace):
    return [sql for sql in trace if sql.lstrip().upper().startswith("DELETE FROM EVIDENCE_FTS WHERE")]


def assert_paired(storage, scan_id, count):
    main, fts = catalog_snapshot(storage, scan_id)
    assert len(main) == len(fts) == count
    assert {(row[0], row[1]) for row in main} == {(row[0], row[1]) for row in fts}
    assert len({row[1] for row in fts}) == count


def test_first_publication_never_deletes_fts_despite_large_historical_catalog(fts_storage):
    storage = fts_storage
    # Populate real FTS segments with many historical documents, not mock rows.
    history = [evidence(i, "historical " + ("shared catalog words " * 32) + str(i)) for i in range(4096)]
    assert storage.replace_evidence_index("historical-package", history) == len(history)
    before = catalog_snapshot(storage, "historical-package")
    denied = []

    def deny_fts_delete(action, table, _column, _database, _trigger):
        if action == sqlite3.SQLITE_DELETE and table == "evidence_fts":
            denied.append(table)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with traced_connections(storage, deny_fts_delete) as trace:
        assert storage.replace_evidence_index("target", [evidence(1)]) == 1
    assert not denied
    assert not fts_deletes(trace)
    assert catalog_snapshot(storage, "historical-package") == before
    assert_paired(storage, "target", 1)
    assert [item["evidence_id"] for item in storage.search_evidence_index("target", "token1")] == ["E1"]


def test_first_original_publication_preserves_existing_translation_without_fts_delete(fts_storage):
    storage = fts_storage
    payload = translation(storage)
    main_before, fts_before = catalog_snapshot(storage, "target")
    with traced_connections(storage) as trace:
        assert storage.replace_evidence_index("target", [evidence(1)]) == 1
    assert not fts_deletes(trace)
    main, fts = catalog_snapshot(storage, "target")
    assert main_before[0] in main and fts_before[0] in fts
    assert storage.get_translation("target", "papers/1.txt") == payload
    assert_paired(storage, "target", 2)


def test_rebuild_pairs_deletes_no_duplicates_and_preserves_translation_and_other_scan(fts_storage):
    storage = fts_storage
    translation(storage)
    storage.replace_evidence_index("target", [evidence(1), evidence(2)])
    storage.replace_evidence_index("other", [evidence(1, "other namespace survives")])
    other_before = catalog_snapshot(storage, "other")
    before = catalog_snapshot(storage, "target")
    translation_before = ([row for row in before[0] if row[1].startswith("translation:")],
                          [row for row in before[1] if row[1].startswith("translation:")])
    for _ in range(2):
        with traced_connections(storage) as trace:
            assert storage.replace_evidence_index("target", [evidence(1, "updated revised evidence"), evidence(3)]) == 2
        assert fts_deletes(trace)
        assert any(sql.startswith("DELETE FROM evidence_index") for sql in trace)
        assert_paired(storage, "target", 3)
        assert catalog_snapshot(storage, "other") == other_before
        current = catalog_snapshot(storage, "target")
        assert ([row for row in current[0] if row[1].startswith("translation:")],
                [row for row in current[1] if row[1].startswith("translation:")]) == translation_before
        assert {row[2] for row in current[0] if not row[1].startswith("translation:")} == {"papers/1.txt", "papers/3.txt"}
    assert not storage.search_evidence_index("target", "token2")


def test_empty_rebuild_removes_originals_once_and_keeps_translation(fts_storage):
    storage = fts_storage
    translation(storage)
    storage.replace_evidence_index("target", [evidence(1)])
    with traced_connections(storage) as first:
        assert storage.replace_evidence_index("target", []) == 0
    assert fts_deletes(first)
    assert_paired(storage, "target", 1)
    with traced_connections(storage) as second:
        assert storage.replace_evidence_index("target", []) == 0
    assert not fts_deletes(second)
    assert_paired(storage, "target", 1)


@pytest.mark.parametrize("existing", [False, True])
def test_main_insert_failure_rolls_back_both_catalogs(fts_storage, existing):
    storage = fts_storage
    translation(storage)
    if existing:
        storage.replace_evidence_index("target", [evidence(1)])
    storage.replace_evidence_index("other", [evidence(8)])
    before = catalog_snapshot(storage)
    with traced_connections(storage) as trace:
        with pytest.raises(sqlite3.IntegrityError):
            storage.replace_evidence_index("target", [evidence(2), evidence(2)])
    assert any(sql.upper() == "ROLLBACK" for sql in trace)
    assert catalog_snapshot(storage) == before
    assert storage.evidence_fts_available


def test_fts_insert_failure_rolls_back_main_and_fts_without_silent_fallback(fts_storage):
    storage = fts_storage
    translation(storage)
    storage.replace_evidence_index("target", [evidence(1)])
    storage.replace_evidence_index("other", [evidence(8)])
    before = catalog_snapshot(storage)

    def deny_fts_insert(action, table, _column, _database, _trigger):
        return (sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and table == "evidence_fts"
                else sqlite3.SQLITE_OK)

    with traced_connections(storage, deny_fts_insert) as trace:
        with pytest.raises(sqlite3.DatabaseError):
            storage.replace_evidence_index("target", [evidence(2)])
    assert fts_deletes(trace)
    assert any(sql.upper() == "ROLLBACK" for sql in trace)
    assert catalog_snapshot(storage) == before
    assert storage.evidence_fts_available
    assert storage.replace_evidence_index("target", [evidence(2)]) == 1
    assert_paired(storage, "target", 2)


def test_payload_preparation_does_not_decode_or_serialize_under_writer_lock(fts_storage):
    storage = fts_storage
    storage.replace_evidence_index("target", [evidence(1)])
    dumps, loads = json.dumps, json.loads

    def unlocked_dumps(*args, **kwargs):
        assert not storage.lock.locked(), "payload serialization must precede the writer transaction"
        return dumps(*args, **kwargs)

    def unlocked_loads(*args, **kwargs):
        assert not storage.lock.locked(), "FTS payload decoding must precede the writer transaction"
        return loads(*args, **kwargs)

    with patch("services.storage.json.dumps", unlocked_dumps), patch("services.storage.json.loads", unlocked_loads):
        assert storage.replace_evidence_index("target", [evidence(2)]) == 1
    assert_paired(storage, "target", 1)


def test_supported_no_fts_build_can_publish_and_rebuild_without_fts_sql(storage):
    # Match a SQLite build without FTS5 rather than swallowing a live FTS error.
    if storage.evidence_fts_available:
        with storage._connect() as conn:
            conn.execute("DROP TABLE evidence_fts")
    storage.evidence_fts_available = False
    translation(storage)
    with traced_connections(storage) as trace:
        assert storage.replace_evidence_index("target", [evidence(1)]) == 1
        assert storage.replace_evidence_index("target", [evidence(2)]) == 1
    assert not any("evidence_fts" in sql.lower() for sql in trace)
    assert len(storage.list_evidence_index("target")) == 2
    assert [item["evidence_id"] for item in storage.search_evidence_index("target", "token2")] == ["E2"]


@pytest.fixture
def full_index_helper():
    # Execute the actual small helper with the real corpus builder, avoiding
    # package-analysis runtime imports (app/model setup is outside this test).
    path = Path(__file__).resolve().parents[1] / "services" / "package_analysis.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "_index_chunks_from_full_documents")
    namespace = {"ParseIsolationCancelled": ParseIsolationCancelled, "evidence_corpus": evidence_corpus}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[function.name]


def test_full_document_helper_keeps_complete_evidence_file_coverage_and_reports_progress(storage, full_index_helper):
    storage.sidecar_threshold = 32 * 1024
    full_evidence = [{"evidence_id": "FULL-%s" % i, "source_path": "full.txt",
                      "section": "Results", "page": i + 1, "label": "paragraph",
                      "text": "采用对照方法确认第%s项结果，并保留来源与方法边界用于复核。" % i}
                     for i in range(12)]
    full = {"source": {"path": "full.txt", "sha256": "full-source"},
            "text": "全文保存在独立压缩侧文件中。" * 4000, "evidence": full_evidence}
    storage.save_document("target", "full.txt", full)
    projection = storage.project_document(full, text_limit=80, evidence_limit=2)
    assert len(projection["evidence"]) < len(full_evidence)
    fallback_text = "采用本地方法说明缺少持久副本时保留全文内容。" * 150 + "尾部结果仍完整保留。"
    error_text = "该文件使用fallback原文提供独立结果，不借用其他文件证据。" * 100
    fallback = {"full.txt": projection,
                "fallback.txt": {"source": {"path": "fallback.txt"}, "text": fallback_text},
                "read-error.txt": {"source": {"path": "read-error.txt"}, "text": error_text}}
    getter = storage.get_document
    visited = []

    def get_document(scan_id, path):
        assert not storage.lock.locked(), "hydration must not hold the evidence publication writer lock"
        visited.append((scan_id, path))
        if path == "read-error.txt":
            raise OSError("isolated unreadable document fixture")
        return getter(scan_id, path)

    progress = []
    before = catalog_snapshot(storage)
    with patch.object(storage, "get_document", side_effect=get_document):
        chunks = full_index_helper(storage, "target", fallback, progress=lambda done, total: progress.append((done, total)))
    assert visited == [("target", path) for path in fallback]
    assert progress == [(1, 3), (2, 3), (3, 3)]
    assert {chunk["source_path"] for chunk in chunks} == set(fallback)
    assert {chunk["evidence_id"] for chunk in chunks if chunk["source_path"] == "full.txt"} == {
        item["evidence_id"] for item in full_evidence}
    assert "".join(chunk["original_text"] for chunk in chunks if chunk["source_path"] == "fallback.txt") == fallback_text
    assert "".join(chunk["original_text"] for chunk in chunks if chunk["source_path"] == "read-error.txt") == error_text
    assert catalog_snapshot(storage) == before


def test_full_document_helper_cancels_before_hydration_or_writer_lock(storage, full_index_helper):
    lock = MagicMock()
    progress = MagicMock()
    with patch.object(storage, "get_document") as hydrate, patch.object(storage, "_connect") as connect, \
            patch.object(storage, "lock", lock):
        with pytest.raises(ParseIsolationCancelled):
            full_index_helper(storage, "target", {"never-hydrate.txt": {"text": "原文内容"}},
                              cancel_check=lambda: True, progress=progress)
    hydrate.assert_not_called()
    connect.assert_not_called()
    lock.__enter__.assert_not_called()
    progress.assert_not_called()
