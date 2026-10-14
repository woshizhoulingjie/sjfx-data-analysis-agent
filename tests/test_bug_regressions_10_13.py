"""Focused synthetic regressions for BUG-10 through BUG-13.

These tests use only in-memory/temporary SQLite state and generated text; no
production data, model, or external service is involved.
"""

import csv
import io
import tempfile
from pathlib import Path

from services.logical_units import structured_text_units
from services.retrieval import _tokens, retrieve_evidence
from services.storage import Storage


def test_unicode_tokens_keep_large_corpus_retrievable():
    assert "оплаты" in _tokens("Срок оплаты договора")
    assert "café" in _tokens("Café")
    assert "abc_123" in _tokens("ABC_123")
    chunks = [
        {
            "evidence_id": "E{}".format(index),
            "source_path": "file-{}.txt".format(index),
            "text": "Срок оплаты договора наступает в октябре.",
        }
        for index in range(251)
    ]
    result = retrieve_evidence(
        [], "оплаты", indexed_chunks=chunks, use_tfidf=True, top_k=8
    )
    assert result["result_count"] > 0


def test_archive_member_scope_uses_logical_path_in_rerank():
    chunk = {
        "evidence_id": "member-1",
        "source_path": "bundle.zip::inside.txt",
        "archive_source_path": "bundle.zip",
        "text": "payment deadline",
    }
    result = retrieve_evidence(
        [], "payment", scope="bundle.zip::inside.txt",
        indexed_chunks=[chunk], use_tfidf=False,
    )
    assert [item["evidence_id"] for item in result["results"]] == ["member-1"]


def test_candidate_ids_are_applied_before_sql_limit():
    with tempfile.TemporaryDirectory() as folder:
        storage = Storage(str(Path(folder) / "state.db"))
        chunks = [
            {"evidence_id": "id{}".format(index),
             "source_path": "file-{}.txt".format(index), "text": "payment"}
            for index in range(2501)
        ]
        storage.replace_evidence_index("scan", chunks)
        result = storage.search_evidence_index(
            "scan", "payment", candidate_evidence_ids=["id2500"]
        )
        assert [item["evidence_id"] for item in result] == ["id2500"]
        assert storage.search_evidence_index(
            "scan", "payment", candidate_evidence_ids=[]
        ) == []
        assert len(storage.search_evidence_index(
            "scan", "payment", candidate_evidence_ids=[item['evidence_id'] for item in chunks], limit=5000
        )) == 2501


def test_csv_partition_keeps_multiline_quoted_record_whole():
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "records.csv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            stream.write(
                "id,note\n1,\"first line\nsecond line\nthird line\"\n2,normal\n"
            )
        stat = path.stat()
        container = {
            "path": "records.csv", "name": "records.csv", "extension": ".csv",
            "size": stat.st_size, "modified_at_ns": stat.st_mtime_ns,
        }
        units = list(structured_text_units(Path(folder), container, partition_bytes=25))
        assert len(units) == 2
        raw = path.read_bytes()
        header = raw[:raw.find(b"\n") + 1]
        parsed = []
        for unit in units:
            payload = (header if unit["include_header"] else b"") + raw[
                unit["byte_start"]:unit["byte_end"]
            ]
            rows = list(csv.reader(io.StringIO(payload.decode("utf-8"))))
            parsed.extend(rows[1:] if unit["include_header"] else rows)
        assert parsed == [
            ["id", "note"],
            ["1", "first line\nsecond line\nthird line"],
            ["2", "normal"],
        ]


def test_exact_candidate_ids_handle_punctuation_and_large_sets(tmp_path):
    storage = Storage(tmp_path / 'candidates.db')
    ids = ['item_%"\\', 'id_second', '合同证据']
    storage.replace_evidence_index('scan', [
        {'evidence_id': value, 'source_path': 'source.txt', 'text': 'payment'}
        for value in ids
    ])
    for value in ids:
        result = storage.search_evidence_index('scan', 'payment', candidate_evidence_ids=[value])
        assert [row['evidence_id'] for row in result] == [value]


def test_tsv_multiline_header_and_records_are_consistent(tmp_path):
    path = tmp_path / 'table.tsv'
    path.write_bytes(b'id\t"multiline\nheader"\n1\t"first\nsecond"\n2\tplain\n3\tlast\n')
    container = {'path': path.name, 'name': path.name, 'extension': '.tsv',
                 'size': path.stat().st_size, 'modified_at_ns': path.stat().st_mtime_ns}
    units = list(structured_text_units(tmp_path, container, partition_bytes=10))
    raw = path.read_bytes()
    expected = list(csv.reader(io.StringIO(raw.decode()), delimiter='\t'))
    actual = []
    for unit in units:
        assert unit['header_byte_end'] == len(b'id\t"multiline\nheader"\n')
        data = raw[unit['byte_start']:unit['byte_end']]
        if unit['include_header']:
            data = raw[:unit['header_byte_end']] + data
        rows = list(csv.reader(io.StringIO(data.decode()), delimiter='\t'))
        actual.extend(rows[1:] if unit['include_header'] else rows)
    assert actual == expected


def test_csv_unquoted_quote_does_not_merge_records(tmp_path):
    path = tmp_path / 'quote.csv'
    path.write_bytes(b'id,note\n1,unquoted"value\n2,normal\n3,end\n')
    container = {'path': path.name, 'name': path.name, 'extension': '.csv', 'size': path.stat().st_size}
    units = list(structured_text_units(tmp_path, container, partition_bytes=5))
    assert len(units) == 3


def test_jsonl_partition_ignores_string_quotes(tmp_path):
    path = tmp_path / 'rows.jsonl'
    path.write_bytes(b'{"x": "hello"}\n{"x": "world"}\n')
    container = {'path': path.name, 'name': path.name, 'extension': '.jsonl', 'size': path.stat().st_size}
    units = list(structured_text_units(tmp_path, container, partition_bytes=4))
    assert len(units) == 2
    assert all(not unit['include_header'] for unit in units)
