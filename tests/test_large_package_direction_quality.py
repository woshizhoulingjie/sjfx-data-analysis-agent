from collections import Counter

from services.large_package_reporting import (
    _direction,
    _direction_evidence,
)


def _example(index, deep=False):
    return {
        "source_path": "evidence/source-{}.pdf".format(index),
        "source_sha256": "sha-{}".format(index),
        "text": (
            "独立来源{}记录相变材料在温度循环中的相态变化，测量温度、滞后区间与"
            "重复试验误差，并说明样品制备和实验条件。"
        ).format(index),
        "deep": deep,
    }


def _groups():
    return {
        "综合资料归纳": {
            "files": 2500,
            "text_files": 1,
            "models": 0,
            "keywords": Counter({"综合资料": 2400, "资料归纳": 2200}),
            "leaves": Counter(),
            "examples": [{
                "source_path": "bulk/one.pdf",
                "source_sha256": "bulk-one",
                "text": "资料包含多种内容，尚无足够证据提出稳定的研究主题。",
                "deep": False,
            }],
        },
        "相变材料热滞后与循环稳定性": {
            "files": 12,
            "text_files": 3,
            "models": 9,
            "keywords": Counter({"相变材料": 11, "热滞后": 9, "循环稳定性": 7}),
            "leaves": Counter({"相变温度": 8, "循环寿命": 4}),
            "examples": [_example(index, deep=True) for index in range(3)],
        },
    }


def test_independent_evidence_outranks_a_much_larger_weak_bucket():
    items = _direction_evidence(_groups(), limit_categories=2, per_category=2)
    assert [item["topic"] for item in items][0] == "相变材料热滞后与循环稳定性"
    assert items[0]["assessment"]["independent_sources"] == 3
    assert items[0]["assessment"]["evidence_quality"] == 1.0


def test_local_fallback_uses_evidence_metrics_instead_of_raw_file_count():
    groups = _groups()
    directions = [
        _direction(name, data, groups, total_files=2512)
        for name, data in groups.items()
    ]
    directions = sorted(filter(None, directions), key=lambda item: -item["score"])
    assert directions[0]["category"] == "相变材料热滞后与循环稳定性"


def test_placeholder_and_evidence_free_buckets_never_become_directions():
    placeholder = {
        "files": 5000,
        "models": 0,
        "keywords": Counter(),
        "leaves": Counter(),
        "examples": [_example(1)],
    }
    assert _direction("内容待归纳", placeholder) is None
    assert _direction_evidence({"可读主题": {**placeholder, "examples": []}}) == []
