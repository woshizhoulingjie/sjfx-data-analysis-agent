"""Create a durable claim-to-evidence ledger from one generated answer."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Mapping, Sequence


CLAIM_SPLIT_RE = re.compile(r"(?<=[。！？!?])\s*|\n+")
CITATION_RE = re.compile(r"\[(\d{1,4})\]")
ADVISORY_RE = re.compile(r"建议|可以考虑|可能|推测|假设|下一步|方法|分析判断")


def build_evidence_ledger(answer: str, citations: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    citations = list(citations or [])
    by_index = {
        int(item.get("citation_index") or index): item
        for index, item in enumerate(citations, 1)
    }
    claims: List[Dict[str, Any]] = []
    unsupported = 0
    source = re.sub(r"([。！？!?])[ \t]*((?:\[\d{1,4}\][ \t]*)+)", r" \2\1", str(answer or ""))
    cursor = 0
    for raw in CLAIM_SPLIT_RE.split(source):
        start = source.find(raw, cursor)
        cursor = start + len(raw)
        paragraph_index = source.count("\n", 0, start)
        text = raw.strip().lstrip("-•*# ").strip()
        heading = bool(re.fullmatch(r"\s*(?:#{1,6}\s+.{1,40}|\*\*.{1,40}\*\*)\s*", raw))
        if len(text) < 4 and not heading:
            continue
        indexes = [int(value) for value in CITATION_RE.findall(text)]
        evidence_ids = [
            str(by_index[index].get("evidence_id"))
            for index in indexes if index in by_index and by_index[index].get("evidence_id")
        ]
        if heading or re.fullmatch(r"[| :\-]+", raw):
            status = "section_label"
        elif evidence_ids:
            status = "supported"
        elif (re.match(r"^(?:建议|可以考虑|可考虑|分析判断[:：]|推测[:：]|假设[:：]|下一步(?:可以|建议))", text)
              and not re.search(r"已经|明确记载|资料显示|(?:预算|金额|收入|支出).{0,8}(?:为|是|达到)", text)):
            status = "analysis_or_advice"
        else:
            status = "unsupported"
            unsupported += 1
        claim_id = "CL-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        claims.append({
            "claim_id": claim_id,
            "text": text,
            "raw_text": raw.strip(),
            "paragraph_index": paragraph_index,
            "status": status,
            "evidence_ids": list(dict.fromkeys(evidence_ids)),
            "citation_indexes": indexes,
        })
    supported = sum(1 for item in claims if item["status"] == "supported")
    factual = supported + unsupported
    return {
        "schema_version": "evidence-ledger/1.0",
        "claims": claims,
        "claim_count": len(claims),
        "supported_claim_count": supported,
        "unsupported_claim_count": unsupported,
        "citation_count": len(citations),
        "claim_support_ratio": round(supported / float(factual or 1), 6),
    }
