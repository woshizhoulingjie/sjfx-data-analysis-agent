"""Saved conclusions remain complete and inert in a standalone HTML export."""
import csv
import importlib.util
from pathlib import Path
from html.parser import HTMLParser

source = Path(__file__).resolve().parents[1] / "services/mail/exporter.py"
spec = importlib.util.spec_from_file_location("mail_exporter_under_test", source)
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


class Repository:
    def __init__(self, result): self.result = result
    def get_result_set(self, *_): return self.result
    def get_message(self, *_): return {"subject_raw": "<script>邮件主题</script>", "sent_at_utc": "2026-10-07"}


class Text(HTMLParser):
    def __init__(self): super().__init__(); self.parts = []; self.tags = []
    def handle_data(self, data): self.parts.append(data)
    def handle_starttag(self, tag, attrs): self.tags.append(tag)


def result(items=None):
    return {"package_id": "p", "items": items or [],
            "definition": {"question": "核对 <条件> & 报价", "fields": [{"message_key": "m1", "value_raw": "<img onerror=alert(1)>"}]},
            "final": {"answer": "第一行：报价 < 100。\n第二行：条件未确认 & 需核查。" + "完整结论。" * 300,
                      "findings": [{"text_zh": "<script>alert(1)</script>", "evidence_id": "e1", "source_quote": "保持原文 & 条件"}],
                      "limitations": ["<img src=x onerror=alert(1)>", "缺少最终确认"],
                      "coverage": {"messages_checked": 2}, "complete": False},
            "coverage": {"logical_messages": 3}}


def test_html_preserves_complete_answer_limits_fields_and_coverage_safely(tmp_path):
    saved = result([{"item_type": "message", "item_id": "m1", "status": "matched"}])
    output = exporter.export_result_set(Repository(saved), "r1", "owner", tmp_path, "html").read_text(encoding="utf8")
    parsed = Text(); parsed.feed(output); text = "".join(parsed.parts)
    assert saved["final"]["answer"] in text
    assert saved["definition"]["question"] in text
    assert "缺少最终确认" in text and '"messages_checked": 2' in text and '"logical_messages": 3' in text
    assert "<img onerror=alert(1)>" in text and "保持原文 & 条件" in text
    assert "script" not in parsed.tags and "img" not in parsed.tags
    assert "<td>m1</td><td>matched</td>" in output


def test_html_empty_mail_result_and_csv_contract_are_preserved(tmp_path):
    saved = result([{"item_type": "attachment", "item_id": "a1", "status": "matched"}])
    repo = Repository(saved)
    output = exporter.export_result_set(repo, "r2", "owner", tmp_path, "html").read_text(encoding="utf8")
    assert "此结果集暂无研究结论" not in output and "完整结论。" in output
    assert "<td>a1</td>" not in output
    target = exporter.export_result_set(repo, "r2", "owner", tmp_path, "csv")
    with target.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle); rows = list(reader)
    assert reader.fieldnames == ["message_key", "status", "sent_at_utc", "subject", "rationale"]
    assert len(rows) == 2 and rows[0]["subject"] == "提取字段（含原文与证据）"
