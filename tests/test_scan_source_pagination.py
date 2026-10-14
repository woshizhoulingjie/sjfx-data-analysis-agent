"""History pagination bounds job lookups without changing ordering or fields."""
from contextlib import contextmanager
import json
import time

import pytest

from services.storage import Storage


class RecordingStorage(Storage):
    def __init__(self, *args, **kwargs):
        self.history_sql = None
        self.history_params = None
        super().__init__(*args, **kwargs)

    @contextmanager
    def _connect(self, *args, **kwargs):
        with super()._connect(*args, **kwargs) as connection:
            outer = self

            class Recorder:
                def execute(self, sql, parameters=()):
                    if "LEFT JOIN analysis_jobs active" in sql:
                        outer.history_sql, outer.history_params = sql, parameters
                    return connection.execute(sql, parameters)

                def __getattr__(self, key):
                    return getattr(connection, key)

            yield Recorder()


@pytest.fixture
def history(tmp_path):
    storage = RecordingStorage(tmp_path / "history.db", tmp_path / "sidecars")
    with storage._connect() as conn:
        for number in range(120):
            scan_id = "scan-{:03d}".format(number)
            owner = "owner-a" if number % 2 == 0 else "owner-b"
            created = "2026-10-{:02d}".format(number // 10 + 1)
            overview = {"root": "/source/" + scan_id, "tree_root": {"name": "group-" + owner},
                        "file_count": number, "total_size": number * 10}
            conn.execute("INSERT INTO scans(id,root_path,payload,owner_id,created_at) VALUES (?,?,?,?,?)",
                         (scan_id, overview["root"], "not a loaded scan payload", owner, created))
            conn.execute("INSERT INTO scan_overviews(scan_id,payload) VALUES (?,?)",
                         (scan_id, json.dumps(overview)))
            conn.execute("INSERT INTO analysis_progress(scan_id,payload) VALUES (?,?)",
                         (scan_id, json.dumps({"status": "running", "progress": number % 100})))
            conn.execute(
                "INSERT INTO analysis_jobs(id,scan_id,owner_id,task_type,status,result) "
                "VALUES (?,?,?,'analyze_package','completed',?)",
                ("job-" + scan_id, scan_id, owner, json.dumps({"padding": "X" * 32768})),
            )
    return storage


def ids(page):
    return [row["scan_id"] for row in page["items"]]


def test_second_page_is_stable_and_keeps_counts_and_cursor(history):
    first = history.list_scan_sources(limit=50, offset=0)
    second = history.list_scan_sources(limit=50, offset=50)
    last = history.list_scan_sources(limit=50, offset=100)
    expected = ["scan-{:03d}".format(number) for number in range(119, -1, -1)]
    assert ids(first) + ids(second) + ids(last) == expected
    assert [page["total"] for page in (first, second, last)] == [120] * 3
    assert [page["next_offset"] for page in (first, second, last)] == [50, 100, None]
    assert history.list_scan_sources(offset=120)["items"] == []
    assert second["items"][0]["file_count"] == 69
    assert second["items"][0]["analysis_progress"]["progress"] == 69


def test_owner_search_and_exact_scan_filter_before_pagination(history):
    page = history.list_scan_sources(owner_id="owner-a", limit=10, offset=10)
    assert ids(page) == ["scan-{:03d}".format(n) for n in range(98, 78, -2)]
    assert page["total"] == 60
    searched = history.list_scan_sources(query="group-owner-b", limit=10, offset=50)
    assert ids(searched) == ["scan-{:03d}".format(n) for n in range(19, -1, -2)]
    assert searched["total"] == 60
    assert searched["next_offset"] is None
    assert ids(history.list_scan_sources(scan_id="scan-055")) == ["scan-055"]
    assert history.list_scan_sources(owner_id="owner-a", scan_id="scan-055")["total"] == 0
    assert history.list_scan_sources(scan_id="scan-055", offset=1)["items"] == []


def test_active_priority_and_last_rowid_semantics_are_unchanged(history):
    with history._connect() as conn:
        for job_id, status, priority, created in [
            ("queued-high", "queued", 99, "2030-01-01"),
            ("running-low", "running", 1, "2020-01-01"),
            ("cancelling-high", "cancelling", 2, "2010-01-01"),
            ("last-failed", "failed", 0, "2000-01-01"),
        ]:
            conn.execute(
                "INSERT INTO analysis_jobs(id,scan_id,owner_id,task_type,status,priority,created_at,error) "
                "VALUES (?,'scan-055','owner-b','scan_and_analyze',?,?,?,'last error')",
                (job_id, status, priority, created),
            )
    item = history.list_scan_sources(scan_id="scan-055")["items"][0]
    assert item["processing"]["active_job_id"] == "cancelling-high"
    assert item["processing"]["active_job_status"] == "cancelling"
    assert item["status"] == "processing"
    assert item["last_error"] == "last error"


def old_sql(storage):
    """Reconstruct the former joins-before-page query for equivalence checks."""
    sql = storage.history_sql
    start = sql.index("FROM (")
    end = sql.index(") s LEFT JOIN scan_overviews", start)
    page = sql[start + len("FROM ("):end]
    suffix = page[page.index(" WHERE "):] if " WHERE " in page else page[page.index(" ORDER BY "):]
    sql = sql[:start] + "FROM scans s " + sql[end + len(") s "):]
    sql = sql[:sql.rindex(" ORDER BY COALESCE(s.created_at")] + suffix
    return sql.replace("active.rowid=(SELECT j.rowid", "active.id=(SELECT j.id").replace(
        "last_job.rowid=(SELECT j.rowid", "last_job.id=(SELECT j.id"
    )


def test_page_plan_uses_covering_job_lookups_and_matches_old_sql(history, tmp_path):
    with history._connect() as conn:
        # A few sizeable results exercise the record layout without a large DB.
        conn.execute("UPDATE analysis_jobs SET result=? WHERE scan_id IN ('scan-055','scan-060')",
                     (json.dumps({"padding": "X" * (512 * 1024)}),))
    report = {}
    for offset in (0, 50):
        history.list_scan_sources(limit=50, offset=offset)
        current_sql, params = history.history_sql, history.history_params
        previous_sql = old_sql(history)
        with history._connect() as conn:
            before_plan = [row["detail"] for row in conn.execute("EXPLAIN QUERY PLAN " + previous_sql, params)]
            after_plan = [row["detail"] for row in conn.execute("EXPLAIN QUERY PLAN " + current_sql, params)]
            started = time.perf_counter()
            before = [dict(row) for row in conn.execute(previous_sql, params)]
            before_seconds = time.perf_counter() - started
            started = time.perf_counter()
            after = [dict(row) for row in conn.execute(current_sql, params)]
            after_seconds = time.perf_counter() - started
        assert before == after
        assert sum("SEARCH j USING COVERING INDEX" in entry for entry in after_plan) == 2
        assert any("CO-ROUTINE s" in entry or "MATERIALIZE s" in entry for entry in after_plan)
        assert not any("SEARCH j USING COVERING INDEX" in entry for entry in before_plan)
        report[str(offset)] = {"before_seconds": before_seconds, "after_seconds": after_seconds,
                               "before_plan": before_plan, "after_plan": after_plan}
    # Test evidence is optional for local review and never forms a speed gate.
    (tmp_path / "query-plan.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


def test_missing_overview_progress_and_empty_page_stay_compatible(history):
    with history._connect() as conn:
        conn.execute("DELETE FROM scan_overviews WHERE scan_id='scan-055'")
        conn.execute("DELETE FROM analysis_progress WHERE scan_id='scan-055'")
    item = history.list_scan_sources(scan_id="scan-055")["items"][0]
    assert item["name"] == "scan-055"
    assert item["analysis_progress"] == {}
    assert item["file_count"] == 0
    assert item["status"] == "pending"
    assert history.list_scan_sources(query="no-such-package")["total"] == 0
