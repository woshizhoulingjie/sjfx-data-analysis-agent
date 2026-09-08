import tempfile
from pathlib import Path

from services.storage import Storage


def test_selection_snapshot_and_file_level_search():
    root = Path(tempfile.mkdtemp())
    storage = Storage(root / "state.sqlite3")
    selection = storage.save_scan_selection(
        "scan-1", ["docs/a.txt", "docs/b.txt"], ["docs/dup.txt"],
        {"default": "parseable_only"},
    )
    assert selection["version"] == 1
    assert selection["included_count"] == 2
    confirmed = storage.save_scan_selection(
        "scan-1", selection["included_paths"], selection["excluded_paths"],
        selection["rules"], status="confirmed", expected_version=1,
    )
    assert confirmed["status"] == "confirmed"
    assert confirmed["version"] == 2
    storage.replace_evidence_index("scan-1", [
        {"source_path": "docs/a.txt", "text": "足球比赛结果", "evidence_id": "e1"},
        {"source_path": "docs/b.txt", "text": "足球联赛", "evidence_id": "e2"},
    ])
    assert storage.search_evidence_file_paths("scan-1", "足球") == ["docs/a.txt", "docs/b.txt"]
