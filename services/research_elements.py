"""Local, source-preserving research signals; no model, storage or network."""
import math
import re
from collections import Counter

ELEMENT_VERSION = "research-elements/1.0"
PATTERNS = {
    "problem": re.compile(r"研究问题|研究目标|旨在|目的|问题在于|需求|research question|objective|aim(?:s|ed)? to", re.I),
    "method": re.compile(r"方法|采用|算法|流程|机制|对照|基线|method|approach|baseline|algorithm", re.I),
    "result": re.compile(r"结果|发现|表明|指标|准确率|成本|result|finding|accuracy|performance|latency", re.I),
    "limitation": re.compile(r"局限|尚未|未验证|未覆盖|仅在|仅限|不足|未来工作|有待|limitation|future work|not (?:tested|validated)|only (?:on|in)|remains unclear", re.I),
    "condition": re.compile(r"条件|当.+时|如果|适用|样本|数据集|温度|噪声|condition|dataset|sample|noise|under|provided that", re.I),
    "resource": re.compile(r"开源|代码|可复现|数据可用|附件|github|repository|source code|available data|released", re.I),
    "counter": re.compile(r"不适用|不能改善|未改善|无显著|失败|相反|不一致|未能|下降|not applicable|no (?:significant|improvement)|failed|contradict|degrad", re.I),
    "evaluation": re.compile(r"缺乏.{0,12}(?:基线|对照|评价|消融)|未.{0,10}(?:比较|评估|消融)|单一数据|without.{0,20}(?:baseline|ablation)|lack.{0,15}evaluat", re.I),
}
LABELS = {"problem": "问题原文", "method": "方法原文", "result": "结果原文",
          "limitation": "局限原文", "condition": "条件原文", "resource": "资源原文"}


def clean(value):
    return " ".join(str(value or "").split())


def complete_excerpt(text, budget=650):
    """Keep whole sentences; never manufacture an ellipsis inside a number."""
    text = clean(text)
    if len(text) <= budget:
        return text
    sentences = re.split(r"(?<=[。！？!?])|(?<=\.)\s+", text)
    output = ""
    for sentence in sentences:
        if len(output) + len(sentence) > budget:
            break
        output += sentence
    return output.strip()


def research_passages(document, per_role=2, max_chars=650):
    """Scan all parsed units before bounded selection, retaining original IDs.

    A unit is selected by its section or content. Adjacent sentences in a raw
    paragraph stay together so a negation, unit or experiment condition is not
    reduced to a keyword. Evidence units remain authoritative when available.
    """
    units = [dict(e) for e in document.get("evidence") or []
             if isinstance(e, dict) and clean(e.get("text"))]
    if not units:
        raw = str(document.get("text") or "")
        paragraphs = [p for p in re.split(r"\n\s*\n|\n", raw) if clean(p)]
        if len(paragraphs) <= 1:
            sentences = re.split(r"(?<=[。！？!?])|(?<=\.)\s+", raw)
            paragraphs = ["".join(sentences[max(0, i - 1):i + 2])
                          for i, s in enumerate(sentences) if any(p.search(s) for p in PATTERNS.values())]
        units = [{"text": p, "section": "正文", "char_start": i} for i, p in enumerate(paragraphs)]
    output = {}
    for role in LABELS:
        ranked = []
        for index, unit in enumerate(units):
            text = clean(unit.get("text"))
            section = clean(unit.get("section"))
            if PATTERNS[role].search(section + " " + text):
                ranked.append((bool(PATTERNS[role].search(section)), len(PATTERNS[role].findall(text)), -index, unit))
        selected, seen = [], set()
        for _, _, _, unit in sorted(ranked, key=lambda row: row[:3], reverse=True):
            text = clean(unit.get("text"))
            if len(text) > max_chars:
                sentences = re.split(r"(?<=[。！？!?])|(?<=\.)\s+", text)
                at = next((i for i, s in enumerate(sentences) if PATTERNS[role].search(s)), 0)
                text = complete_excerpt("".join(sentences[max(0, at - 1):at + 2]), max_chars)
                if not PATTERNS[role].search(text):
                    # The preceding sentence may have spent the entire budget.
                    # Keep the whole signal sentence, or omit an oversized one;
                    # never return a heading-only/irrelevant prefix as the role.
                    text = complete_excerpt("".join(sentences[at:at + 2]), max_chars)
            if text and text not in seen and PATTERNS[role].search(clean(unit.get("section")) + " " + text):
                seen.add(text)
                selected.append(dict(unit, text=text, element_role=role))
            if len(selected) >= per_role:
                break
        output[role] = selected
    return output


def distinguishing_terms(clusters, documents, limit=6):
    """Small c-TF-IDF inspired expression aid; not a new clustering model."""
    counts = []
    stop = {"研究", "方法", "结果", "资料", "文件", "分析", "系统", "the", "and", "with", "study"}
    for cluster in clusters:
        counter = Counter()
        for path in cluster.get("members") or []:
            doc = documents.get(path) or {}
            structure = doc.get("structure") or {}
            text = clean(structure.get("title")) + " " + " ".join(map(str, structure.get("headings") or []))
            for rows in research_passages(doc, per_role=1, max_chars=240).values():
                text += " " + " ".join(row["text"] for row in rows)
            for run in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", text.lower()):
                terms = [run] if run.isascii() else [run[i:i + n] for n in (2, 3, 4) for i in range(len(run) - n + 1)]
                counter.update(t for t in terms if t not in stop)
        counts.append(counter)
    df = Counter(t for counter in counts for t in counter)
    result = []
    for counter in counts:
        ranked = sorted(counter, key=lambda t: (-counter[t] / max(1, sum(counter.values())) * math.log(1 + len(counts) / df[t]), -len(t), t))
        chosen = []
        for term in ranked:
            if not any(term in previous for previous in chosen):
                chosen.append(term)
            if len(chosen) >= limit:
                break
        result.append(chosen)
    return result
