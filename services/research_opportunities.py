"""Bounded ordinary-package opportunity discovery and review.

Only imported body evidence can seed a question. Scores compare research
plans, never novelty probabilities. Products stay inside the report job.
"""
import hashlib
import json
import re
from array import array

from services.evidence import evidence_quality, verify_claim_evidence, verify_inference_evidence
from services.research_elements import PATTERNS, clean, complete_excerpt
from services.retrieval import retrieve_evidence, _in_scope, _tokens

RESEARCH_VERSION = "ordinary-opportunities/1.4"
ROLES = ("support", "prior_work", "conditions", "counterevidence", "resources")
KINDS = {
    "boundary": "适用边界验证", "evaluation": "补全评价对照",
    "disagreement": "条件与结果分歧复核", "transfer": "方法迁移验证",
    "replication": "关键结论复核", "synthesis": "证据比较综合",
}
_GLOBAL = re.compile(r"全球首次|世界首次|无人研究|填补.{0,12}空白|首创|world.first|never studied", re.I)
_UNVALIDATED = re.compile(r"尚未|未验证|未覆盖|没有证明|未证明|有待|需(?:要)?确认|需要核查|not (?:tested|validated)|unverified|remains unclear", re.I)
_RESOURCE_NEGATION = re.compile(r"(?:未|不|没有|无).{0,8}(?:开源|提供|发布|代码|数据|可复现)|(?:代码|数据|资源).{0,8}(?:不可用|缺失|未提供|未发布)|not.{0,12}(?:available|released|provided)|unavailable", re.I)
_ASSERTION = re.compile(r"已(?:经|被|证明|完成|实现)|材料(?:表明|显示)|研究发现|结果(?:表明|显示)|显著(?:提高|改善)|是|具有|能够|可以|导致|提升|降低|提高|达到|有效|优于|改善|证明|shows?|improves?|achieves?|outperforms?|effective|\bis\b|\bare\b|\bcan\b", re.I)


def _path(value):
    path = str(value or "").replace("\\", "/").strip()
    while path.startswith("./"):
        path = path[2:]
    if any(part == ".." for part in path.replace("::", "/").split("/")):
        return ""
    return path


def _evidence_path(item):
    """Resolve archive aliases only when their physical/member paths agree."""
    source, archive = _path(item.get("source_path")), _path(item.get("archive_source_path"))
    if not source:
        return archive if not item.get("source_path") else ""
    if not archive:
        return source if not item.get("archive_source_path") else ""
    if source == archive or source.startswith(archive + "::"):
        return source
    member = _path(item.get("archive_member"))
    if member and source == archive.rsplit("/", 1)[-1] + "::" + member:
        return archive + "::" + member
    return ""


def _belongs(item, members):
    path = _evidence_path(item)
    return bool(path) and any(path == _path(m) or path.startswith(_path(m) + "::") for m in members if _path(m))


def _identity(item, documents):
    path = _evidence_path(item)
    source = (documents.get(path) or documents.get(_path(item.get("archive_source_path"))) or {}).get("source") or {}
    # DOI and source hashes establish relationships. Uncertain titles alone do
    # not certify independent studies; reported counts remain source identities.
    member_source = bool(item.get("archive_source_path") and "::" in path)
    doi = clean((item.get("doi") if member_source else source.get("doi")) or item.get("doi")).lower()
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", doi)
    return doi or str((item.get("source_sha256") if member_source else source.get("sha256")) or item.get("source_sha256") or path)


def _pool(analysis, clusters):
    documents = {_path((d.get("source") or {}).get("path") or d.get("path")): d
                 for d in analysis.get("document_index") or []}
    rows = list(analysis.get("research_evidence") or [])
    for document in documents.values():
        rows.extend(document.get("evidence") or [])
    for cluster in clusters:
        rows.extend(cluster.get("evidence_chain") or [])
    for key in ("research_retrieval", "retrieval"):
        for query in (analysis.get(key) or {}).get("queries") or []:
            rows.extend(query.get("results") or [])
    declared = {_path(p) for cluster in clusters for p in cluster.get("members") or [] if _path(p)}
    selected = {_path(p) for p in analysis.get("selected_scope_paths") or [] if _path(p)}
    allowed = selected if analysis.get("selected_scope_only") else declared | set(documents)
    scope = _path(analysis.get("research_scope") or ".")
    if not scope:
        return [], documents
    output, seen, id_texts = [], set(), {}
    # Conflicting bodies sharing an ID cannot safely become citable candidates.
    for raw in rows:
        if (isinstance(raw, dict) and raw.get("evidence_id") and _belongs(raw, allowed)
                and _in_scope(_evidence_path(raw), scope) and not raw.get("preview_only")
                and evidence_quality(raw).get("eligible")
                and (raw.get("coverage") or {}).get("formal_evidence_ready") is not False):
            id_texts.setdefault(str(raw["evidence_id"]), set()).add(clean(raw.get("text")))
    for raw in sorted((r for r in rows if isinstance(r, dict)), key=lambda r: (_evidence_path(r), str(r.get("evidence_id")))):
        if not isinstance(raw, dict) or not raw.get("evidence_id"):
            continue
        if len(id_texts.get(str(raw["evidence_id"]), ())) != 1:
            continue
        if not _belongs(raw, allowed) or not _in_scope(_evidence_path(raw), scope):
            continue
        if not evidence_quality(raw).get("eligible") or raw.get("preview_only"):
            continue
        if re.search(r"^(?:references|bibliography|参考文献|引用文献)\b", clean(raw.get("section")), re.I):
            continue
        path = _evidence_path(raw)
        role = ((documents.get(path) or documents.get(_path(raw.get("archive_source_path"))) or {}).get("classification") or {}).get("document_role")
        if role in {"要求与说明材料", "派生概览材料"}:
            continue
        item = dict(raw, text=clean(raw.get("text")))
        if re.search(r"没有可复核的共同发现|尚未形成稳定主题|(?:没有|缺少|无)(?:可用)?正文(?:证据)?|仅(?:为|有)(?:文件)?目录", item["text"]):
            continue
        if (item.get("coverage") or {}).get("formal_evidence_ready") is False:
            continue
        # Identical passage copies cannot add source diversity or value, even
        # when copied with a new path/hash. This is intentionally conservative.
        key = clean(item["text"]).casefold()
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return sorted(output, key=lambda e: (str(e.get("source_path")), str(e.get("evidence_id")))), documents


def _roles(items):
    result = {role: [] for role in ROLES}
    for item in items:
        text = clean(item.get("translated_text") or item.get("text"))
        result["support"].append(item)
        if PATTERNS["method"].search(text) or PATTERNS["result"].search(text):
            result["prior_work"].append(item)
        if PATTERNS["condition"].search(text) or PATTERNS["limitation"].search(text):
            result["conditions"].append(item)
        sentences = re.split(r"[。；;.!?]", text)
        if any(PATTERNS["counter"].search(re.sub(r"(?:成本|费用|延迟|时延|耗时|失败率|错误率|误差).{0,8}下降", "", s)) for s in sentences):
            result["counterevidence"].append(item)
        if any(PATTERNS["resource"].search(s) and not _RESOURCE_NEGATION.search(s) for s in sentences):
            result["resources"].append(item)
    return result


def _signal(items, kind):
    pattern = PATTERNS[kind]
    for item in items:
        text = clean(item.get("translated_text") or item.get("text"))
        sentences = re.split(r"(?<=[。！？!?])|(?<=\.)\s+", text)
        for i, sentence in enumerate(sentences):
            if pattern.search(sentence):
                # Retain the neighbouring condition rather than a bare cue.
                quote = complete_excerpt("".join(sentences[max(0, i - 1):i + 2]), 360)
                if quote:
                    return quote, item
    return "", None


def _terms(text):
    stop = {"研究", "方法", "结果", "数据", "系统", "分析", "验证", "条件", "可靠性", "研究目标", "实验结果", "the", "and", "method", "result", "baseline", "dataset", "condition", "research", "problem", "task", "under", "with"}
    text = clean(text)
    # Relations are a lightweight inventory, not an in-memory second full
    # token index. Retain signal sentences as well as beginning/middle/end.
    if len(text) > 1600:
        sentences = re.split(r"(?<=[。！？!?])|(?<=\.)\s+", text)
        signals = [s for s in sentences if any(PATTERNS[r].search(s) for r in ("problem", "method", "limitation", "counter"))]
        text = " ".join([text[:240], text[len(text) // 2:len(text) // 2 + 240], text[-240:]] + signals[:4])[:2400]
    values = set(_tokens(text))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        values.update(run[i:i + n] for n in (4, 5, 6) for i in range(max(0, len(run) - n + 1)))
    values = {t for t in values if t not in stop and len(t) <= 40 and (len(t) >= 4 or (t.isascii() and len(t) >= 3))}
    return set(sorted(values, key=lambda t: (-len(t), t))[:192])


def _shared(left, right):
    a, b = _terms(left), _terms(right)
    return sorted(a & b, key=lambda t: (-len(t), t))


def _task_labels(text):
    labels = []
    for sentence in re.split(r"[。；;.!?]", clean(text)):
        matches = re.findall(r"(?=(?:研究(?:目标|问题)?(?:是|为)?|需求(?:是)?|旨在|解决|用于|针对|面向|task (?:is|of)|objective is|aims? to)\s*([^，。；;]{4,100}))", sentence, re.I)
        matches += re.findall(r"^([^，。；;]{4,80}?)(?:任务|的复核结果|的研究)", sentence)
        for value in matches:
            value = re.split(r"采用|使用|通过|作为|任务|在|条件|\busing\b|\bunder\b|\bwith\b", value, 1, flags=re.I)[0]
            value = re.sub(r"^(?:本文|本研究|是|评估|解决|验证|the |a |an )+", "", value, flags=re.I).strip(" ，,:：")
            if len(value) >= 4 and value not in {"不同来源", "预测可靠性", "可靠性问题"}:
                labels.append(value)
    return list(dict.fromkeys(labels))


def _method_label(text):
    for value in re.findall(r"(?:采用|使用|应用|using|uses?|apply|applying)\s*([^，。；;.!?]{3,90})", clean(text), re.I):
        value = re.split(r"作为|对|评估|整理|比较|研究|进行|完成", value, 1)[0].strip()
        if len(value) >= 4 and value not in {"逐项比较方法", "比较方法", "研究方法"}:
            return value
    return ""


def _common_task(left, right):
    for a in _task_labels(left):
        for b in _task_labels(right):
            if a in b or b in a:
                return a if len(a) <= len(b) else b
    return ""


def _counter_relevant(seed, item):
    """A shared method name alone is not a counterexample for another task."""
    text = clean(item.get("translated_text") or item.get("text"))
    context = seed["methods"] + " " + seed["anchor"]
    left, right = _task_labels(context), _task_labels(text)
    if left and right:
        return any(a in b or b in a for a in left for b in right)
    if left:
        return any(task.casefold() in text.casefold() for task in left)
    return bool(_shared(seed["topic"] + " " + seed["anchor"], text))


def _focus(anchor, role):
    return next((s.strip(" 。") for s in re.split(r"[。；;]", anchor) if PATTERNS[role].search(s)), anchor.rstrip("。"))


def _recall_window(item, terms):
    """Bound only the retrieval input; the authoritative chain stays intact."""
    text = clean(item.get("text"))
    sentences = re.split(r"(?<=[。！？!?])|(?<=\.)\s+", text)
    if len(text) > 900:
        at = max(range(len(sentences)), key=lambda i: (len(terms & _terms(sentences[i])),
                                                       bool(PATTERNS["counter"].search(sentences[i])), -i))
        text = complete_excerpt("".join(sentences[max(0, at - 1):at + 2]), 900)
    translated = complete_excerpt(item.get("translated_text"), 900)
    return dict(item, text=text, original_text=text, translated_text=translated or None) if text else None


def _selection_reason(item, alternative):
    """Explain actual ranking tradeoffs, without manufacturing superiority."""
    if not alternative:
        return "当前仅此候选通过本次精查闸门，聚焦“{}”；仍需核对原文条件并确认资源。".format(item["title"])
    labels = {"gap_specificity": "缺口具体性", "evidence_support": "原文背景支撑",
              "feasibility": "可行性", "goal_relevance": "已知目标相关性", "local_difference": "局部工作差异"}
    left = item.get("score_breakdown", {}).get("dimensions", {})
    right = alternative.get("score_breakdown", {}).get("dimensions", {})
    weights = item.get("score_breakdown", {}).get("weights", {})
    differences = [(key, float(left[key]) - float(right[key])) for key in labels
                   if left.get(key) is not None and right.get(key) is not None]
    positives = sorted(((k, d) for k, d in differences if d > .01), key=lambda pair: (-pair[1] * weights.get(pair[0], 1), pair[0]))
    negatives = sorted(((k, d) for k, d in differences if d < -.01), key=lambda pair: (pair[1] * weights.get(pair[0], 1), pair[0]))
    def describe(rows, direction):
        return "、".join("{}评分{}（{:.2f} 对 {:.2f}）".format(labels[k], direction, left[k], right[k]) for k, _ in rows[:2])
    def focus(candidate):
        gap = clean(candidate.get("research_gap"))
        if gap and not gap.startswith("先核对已有判断"):
            detail = _focus(gap, "evaluation" if candidate.get("opportunity_kind") == "evaluation" else "limitation")
            if len(detail) <= 130 and detail not in candidate["title"]:
                return candidate["title"] + "（原文限制：" + detail + "）"
        return candidate["title"]
    text = "本项聚焦“{}”，最强备选聚焦“{}”。".format(focus(item), focus(alternative))
    if positives:
        text += "本项{}。".format(describe(positives, "较高"))
    if negatives:
        text += "限制是{}。".format(describe(negatives, "较低"))
    left_score, right_score = float(item.get("score") or 0), float(alternative.get("score") or 0)
    if not positives or left_score <= right_score:
        text += "当前依据不能证明本项明显优于该备选。"
        if left_score == right_score:
            text += "两者总分同为{:.1f}，按候选ID稳定顺序暂选；需结合研究目标与资源取舍。".format(left_score)
        else:
            text += "本项总分{:.1f}、备选{:.1f}；需结合研究目标与资源取舍。".format(left_score, right_score)
    else:
        text += "这些维度使当前规则总分较高（{:.1f} 对 {:.1f}），不代表专家已确认研究价值更高；先按上述限制核验原文与资源。".format(left_score, right_score)
    return text


def discover_opportunities(analysis, clusters, limit=8):
    """Scan every topic cheaply, then refine a bounded set of concrete plans."""
    pool, documents = _pool(analysis, clusters)
    seeds, rejected, topic_records = [], [], []
    token_cache = {}
    goal = clean(analysis.get("research_goal") or analysis.get("user_research_goal"))
    scope_key = [_path(analysis.get("research_scope") or "."),
                 sorted(_path(p) for p in analysis.get("selected_scope_paths") or [])
                 if analysis.get("selected_scope_only") else None]
    # Share vocabulary strings once, keeping compact integer term IDs per unit.
    # Thousands of Python string sets would be too costly beside the parser.
    vocabulary, term_cache = {}, {}
    for e in pool:
        indices = []
        for term in _terms(e.get("translated_text") or e.get("text")):
            if term not in vocabulary:
                vocabulary[term] = len(vocabulary)
            indices.append(vocabulary[term])
        term_cache[str(e["evidence_id"])] = array("I", indices)
    by_source, tasks = {}, {}
    for item in pool:
        path = _evidence_path(item)
        by_source.setdefault(path, []).append(item)
        if "::" in path:
            by_source.setdefault(path.split("::", 1)[0], []).append(item)
        text = clean(item.get("translated_text") or item.get("text"))
        if PATTERNS["condition"].search(text) and _UNVALIDATED.search(text):
            for task in _task_labels(text):
                tasks.setdefault(task, []).append(item)
    for cluster in sorted(clusters, key=lambda c: (str(c.get("topic") or c.get("name") or ""), sorted(map(str, c.get("members") or [])))):
        topic = clean(cluster.get("topic") or cluster.get("name"))
        members = {_path(p) for p in cluster.get("members") or [] if _path(p)}
        own = list({str(e["evidence_id"]): e for path in sorted(members) for e in by_source.get(path, [])}.values())
        topic_records.append({"topic": topic, "member_count": len(members), "eligible_units": len(own)})
        if not topic or not own:
            continue
        roles = _roles(own)
        gap, gap_item = _signal(own, "limitation")
        evaluation, evaluation_item = _signal(own, "evaluation")
        methods, method_item = _signal(own, "method")
        result, result_item = _signal(own, "result")
        definitions = []
        if gap:
            definitions.append(("boundary", gap, gap_item, None, ""))
        if evaluation:
            definitions.append(("evaluation", evaluation, evaluation_item, None, ""))
        if result_item:
            opposite = next((e for e in roles["counterevidence"]
                             if e["evidence_id"] != result_item["evidence_id"]
                             and (_common_task(result, e.get("text")) or _shared(result, e.get("text")))), None)
            if opposite:
                definitions.append(("disagreement", result, result_item, opposite, ""))
        method_name = _method_label(methods)
        if method_item and method_name:
            for task in _task_labels(methods):
                target = next((e for e in tasks.get(task, [])
                               if _identity(e, documents) != _identity(method_item, documents)
                               and not _belongs(e, members)), None)
                if target:
                    definitions.append(("transfer", methods, method_item, target, task))
                    break
        factual_item = next((e for e in own if evidence_quality(e).get("factual") or evidence_quality(e).get("numeric")), None)
        fallback = result or methods or (complete_excerpt(factual_item.get("text"), 360) if factual_item else "")
        if not definitions and fallback:
            source_count = len({_identity(e, documents) for e in own})
            definitions.append(("synthesis" if source_count > 1 else "replication",
                                fallback, result_item or method_item or factual_item, None, ""))
        for kind, anchor, anchor_item, target, task in definitions:
            fingerprint = [kind, topic, _identity(anchor_item, documents), anchor, scope_key,
                           [_identity(target, documents), clean(target.get("text"))] if target else None]
            candidate_id = "RO-" + hashlib.sha256(json.dumps(fingerprint, ensure_ascii=False).encode()).hexdigest()[:16]
            cheap_score = {"boundary": 4, "evaluation": 4, "disagreement": 3, "transfer": 3,
                           "synthesis": 1, "replication": 1}[kind]
            cheap_score += int(bool(roles["resources"])) - int(bool(roles["counterevidence"]))
            if goal:
                cheap_score += min(2, len(_shared(goal, topic + " " + anchor)))
            seeds.append({"kind": kind, "topic": topic, "anchor": anchor, "anchor_item": anchor_item,
                          "target": target, "task": task, "members": members, "methods": methods,
                          "method_name": method_name, "result": result, "gap": gap,
                          "evaluation": evaluation, "candidate_id": candidate_id, "cheap_score": cheap_score})
    # A job may inventory hundreds of topics; it must not issue six full-corpus
    # BM25 queries per candidate. Preserve the deferred IDs in the audit.
    unique_seeds = {s["candidate_id"]: s for s in seeds}
    ordered = sorted(unique_seeds.values(), key=lambda s: (-s["cheap_score"], s["candidate_id"]))
    budget = min(24, max(8, max(1, min(10, int(limit))) * 3))
    first, rest, seen_topics = [], [], set()
    for seed in ordered:
        if seed["topic"] in seen_topics:
            rest.append(seed)
        else:
            first.append(seed)
            seen_topics.add(seed["topic"])
    refinement = (first[:budget] + rest[:max(0, budget - len(first))])[:budget]
    opportunities = []
    for seed in refinement:
        kind, topic, anchor, anchor_item = (seed[k] for k in ("kind", "topic", "anchor", "anchor_item"))
        target, task, members = seed["target"], seed["task"], seed["members"]
        candidate_id = seed["candidate_id"]
        # A limitation excerpt can omit the method/task from earlier sentences.
        # Use the source-bound research context for recall, keeping each part
        # bounded rather than tokenizing another full-corpus query per role.
        anchor_words = (_terms(anchor) | _terms(topic) | _terms(seed["methods"])
                        | _terms(seed["method_name"]) | _terms(task))
        anchor_terms = {vocabulary[t] for t in anchor_words if t in vocabulary}
        overlaps = {str(e["evidence_id"]): sum(t in anchor_terms for t in term_cache[str(e["evidence_id"])]) for e in pool}
        anchor_identity = _identity(anchor_item, documents)
        related = [e for e in pool if _identity(e, documents) == anchor_identity
                   or overlaps[str(e["evidence_id"])]
                   or (kind == "synthesis" and _belongs(e, members))]
        # Rare limiting/failure/resource evidence gets reserved space before
        # ordinary background passages. All available related units are scanned.
        related_roles = _roles(related)
        related_roles["counterevidence"] = [e for e in related_roles["counterevidence"] if _counter_relevant(seed, e)]
        reserved = [anchor_item] + ([target] if target else [])
        for role in ("counterevidence", "conditions", "resources", "prior_work"):
            ranked = sorted(related_roles[role], key=lambda e: (-overlaps[str(e["evidence_id"])], str(e["evidence_id"])))
            reserved.extend(ranked[:24])
        ranked = sorted(related, key=lambda e: (-overlaps[str(e["evidence_id"])], str(e["evidence_id"])))
        bounded = list({str(e["evidence_id"]): e for e in reserved + ranked}.values())[:160]
        query = topic + " " + anchor
        windows = [window for window in (_recall_window(e, anchor_words) for e in bounded) if window]
        recalled = retrieve_evidence({}, query, indexed_chunks=windows, use_tfidf=False,
                                     top_k=24, per_source_limit=4, token_cache=token_cache)
        # A bounded cache avoids retaining token lists for every refined topic.
        if len(token_cache) > 160:
            token_cache = {k: token_cache[k] for k in list(token_cache)[-80:]}
        recalled_roles = _roles(recalled.get("results") or [])
        buckets, role_audit = {}, {}
        for role in ROLES:
            candidates = related_roles[role]
            role_ids = {str(e["evidence_id"]) for e in candidates}
            ordered_role = ([anchor_item] if anchor_item in candidates else [])
            ordered_role += ([target] if target and target in candidates else [])
            ordered_role += [next(e for e in bounded if e["evidence_id"] == hit["evidence_id"])
                             for hit in recalled_roles[role] if str(hit["evidence_id"]) in role_ids]
            ordered_role += sorted(candidates, key=lambda e: (-overlaps[str(e["evidence_id"])], str(e["evidence_id"])))
            buckets[role] = list({str(e["evidence_id"]): e for e in ordered_role}.values())[:3]
            role_audit[role] = {"query": query, "selection": "role_partition_of_bounded_recall_and_inventory",
                                "corpus_chunks": len(candidates), "evidence_ids": [e["evidence_id"] for e in buckets[role]]}
        title = "{}：{}".format(KINDS[kind], topic)
        if kind in {"boundary", "evaluation"}:
            focus = _focus(anchor, "limitation" if kind == "boundary" else "evaluation")
            title += " — " + focus[:110]
            question = "针对“{}”，如何检验“{}”并保留原文条件？".format(topic, focus)
        elif kind == "transfer":
            title = "方法迁移验证：{}用于{}的未验证条件".format(seed["method_name"], task)
            question = "“{}”用于“{}”时，在目标材料尚未确认的条件下是否有效；哪些条件会使迁移失败？".format(seed["method_name"], task)
        elif kind == "disagreement":
            question = "“{}”在统一对象、结果口径与输入条件后，所引不同结果是否仍存在分歧？".format(topic)
        else:
            question = "如何{}“{}”中的判断“{}”，并保留原文条件？".format("交叉核对" if kind == "synthesis" else "复核", topic, complete_excerpt(anchor, 180) or anchor[:180])
        counter = bool(buckets["counterevidence"])
        method_core = re.sub(r"方法|algorithm|method", "", seed["method_name"], flags=re.I).strip()
        blocking = [e for e in related_roles["counterevidence"] if kind == "transfer" and method_core
                    and method_core.casefold() in clean(e.get("translated_text") or e.get("text")).casefold()
                    and (_common_task(anchor, e.get("text")) or task in clean(e.get("text")))
                    and re.search(r"不适用|不能.{0,12}迁移|not applicable|failed.{0,20}transfer", clean(e.get("text")), re.I)]
        if blocking:
            rejected.append({"candidate_id": candidate_id, "title": title,
                             "quality_gate": {"status": "withdrawn", "reason": "direct_transfer_counterevidence"},
                             "counterevidence_ids": [e["evidence_id"] for e in blocking]})
            continue
        chain_by_id = {}
        for role in ("counterevidence", "conditions", "prior_work", "resources", "support"):
            for e in buckets[role]:
                chain_by_id[str(e["evidence_id"])] = dict(e, support_status="supported")
        chain_by_id[str(anchor_item["evidence_id"])] = dict(anchor_item, support_status="supported")
        if target:
            chain_by_id[str(target["evidence_id"])] = dict(target, support_status="supported")
        chain = list(chain_by_id.values())
        identities = {_identity(e, documents) for e in chain}
        warnings = list((analysis.get("coverage") or {}).get("limitations") or [])
        warnings += ["比较仅覆盖当前导入资料，不能证明全球首次或领域无人研究。",
                     "来源身份去重不等于独立实验；版本、综述转引及资源可用性需人工核验。"]
        if len(ordered) > len(refinement):
            warnings.append("已盘点全部可用主题；本次仅精查{}个候选，另有{}个等待精查，当前排序受该预算限制。".format(len(refinement), len(ordered) - len(refinement)))
        if not buckets["resources"]:
            warnings.append("材料未确认可用数据、代码与运行条件；首步先核验资源，再决定能否实验。")
        if not counter:
            warnings.append("当前资料未检出直接反例，不代表不存在反证。")
        else:
            warnings.append("检出否定或失败结果；先核对主体、条件与口径，有效性不得写为事实。")
        dimensions = {"gap_specificity": .9 if kind in {"boundary", "evaluation"} else (.75 if kind == "disagreement" else .5),
                      "evidence_support": .8 if len(identities) > 1 else .55,
                      "feasibility": .8 if buckets["resources"] and buckets["prior_work"] else .5,
                      "goal_relevance": min(1, len(_shared(goal, question)) / 3) if goal else None,
                      "local_difference": .7 if seed["gap"] or seed["evaluation"] else .2}
        if counter:
            dimensions["evidence_support"] -= .35
            if kind == "transfer":
                dimensions["feasibility"] -= .3
        weights = {"gap_specificity": .25, "evidence_support": .25, "feasibility": .2, "goal_relevance": .2, "local_difference": .1}
        active = [k for k in weights if dimensions[k] is not None]
        score = round(100 * sum(weights[k] * dimensions[k] for k in active) / sum(weights[k] for k in active), 1)
        gate = {"status": "passed", "checks": {"body_evidence": bool(chain), "question": bool(anchor),
                "scope_bound": all(_evidence_path(e) for e in chain),
                "single_source_plan": kind in {"replication", "boundary", "evaluation"} or len(identities) > 1},
                "resources": "material_described" if buckets["resources"] else "confirmation_first"}
        if not all(gate["checks"].values()):
            rejected.append({"candidate_id": candidate_id, "title": title, "quality_gate": dict(gate, status="rejected")})
            continue
        original_method = complete_excerpt(seed["methods"], 220) or "按原文已描述的判断及流程复核"
        target_excerpt = complete_excerpt(target.get("text"), 220) if target else ""
        methods = ["核对原文[{}]及相关限制，提取对象、方法与条件：{} → 带证据编号的条件对照表".format(anchor_item["evidence_id"], complete_excerpt(anchor, 220) or _focus(anchor, "limitation")),
                   "先确认“{}”所需数据、代码、权限及运行条件，登记已确认与缺失项 → 资源核验清单及可执行性判断".format(topic),
                   "在资源确认后，以原文方法“{}”按原指标和原条件复核，并逐项核对限制与反例；缺资源时先比较所引证据 → {}记录".format(original_method, KINDS[kind])]
        if kind == "transfer":
            methods[2] = "确认资源后，将“{}”用于同一任务“{}”，以来源方法/基线与目标条件[{}]逐项比较：{} → 迁移成功与失败条件记录".format(seed["method_name"], task, target["evidence_id"], target_excerpt)
        opportunities.append({"candidate_id": candidate_id, "title": title, "direction": title, "opportunity_kind": kind,
            "candidate_source": "server_research_opportunity", "type": "推论", "score": score,
            "priority": "中" if score >= 45 else "低", "confidence": "低" if counter or len(identities) < 2 else "中",
            "confidence_score": .35 if counter or len(identities) < 2 else .6,
            "rationale": "材料明确提供判断或局限：{}；因此先开展{}，有效性仍待验证。".format(anchor, KINDS[kind]),
            "basis": "明确问题、原文条件与局部已有工作比较；文件数和修改时间不参与排序。",
            "research_questions": [question, "哪些已有工作与上述具体问题重叠；数据、方法和比较口径是否可复核？"],
            "methods": methods, "known_work": complete_excerpt(seed["methods"] or seed["result"] or anchor, 360),
            "research_gap": seed["gap"] or seed["evaluation"] or "先核对已有判断的条件及来源，不宣称已有全球研究缺口。",
            "hypothesis": question, "hypothesis_status": "unverified", "evidence_roles": {role: [e["evidence_id"] for e in buckets[role]] for role in ROLES},
            "evidence_chain": chain, "evidence_ids": list(chain_by_id), "evidence_status": "supported",
            "representative_documents": list(dict.fromkeys(e.get("archive_source_path") or e.get("source_path") for e in chain))[:5],
            "source_paths": sorted(set(e.get("archive_source_path") or e.get("source_path") for e in chain)),
            "independent_source_count": len(identities), "unique_evidence_count": len(chain),
            "quality_gate": gate, "limitations": warnings, "counterevidence": [dict(e) for e in buckets["counterevidence"]],
            "score_breakdown": {"dimensions": dimensions, "weights": weights, "goal_known": bool(goal), "ranking_only": True},
            "retrieval_audit": {"method": recalled["method"], "corpus_chunks": len(windows), "available_corpus_chunks": len(pool),
                                "related_chunks": len(related), "bounded_chunks": len(bounded), "query": query,
                                "bm25_queries": 1, "role_queries": role_audit}})
    opportunities.sort(key=lambda c: (-c["score"], c["candidate_id"]))
    selected = opportunities[:max(1, min(10, int(limit)))]
    for index, item in enumerate(selected):
        item["rank"] = index + 1
        alternative = next((c for c in opportunities if c["candidate_id"] != item["candidate_id"]), None)
        item["selection_reason"] = _selection_reason(item, alternative)
    refined_ids = {s["candidate_id"] for s in refinement}
    deferred = [{"candidate_id": s["candidate_id"], "topic": s["topic"], "opportunity_kind": s["kind"],
                 "reason": "bounded_refinement_budget"} for s in ordered if s["candidate_id"] not in refined_ids]
    return selected, {"version": RESEARCH_VERSION, "topics": topic_records, "eligible_units": len(pool),
                      "discovered_count": len(ordered), "refined_count": len(refinement), "refinement_budget": budget,
                      "proposed_count": len(opportunities), "registered_count": len(selected), "rejected": rejected,
                      "deferred": deferred, "goal_known": bool(goal), "coverage": "all_available_topics_scanned",
                      "refinement_complete": not deferred,
                      "unprocessed_topics": [t["topic"] for t in topic_records if not t["eligible_units"]]}

def _direct_fact(text, chain):
    """A lexical 'supported' result alone never verifies a proposed fact."""
    value = re.sub(r"^(?:材料(?:已|表明|显示|指出)?|原文(?:表明|显示|指出)?|研究发现|结果(?:表明|显示))\s*[:：,，]?", "", clean(text)).strip(" 。.!?？\"“”")
    if len(value) < 6:
        return False
    for item in chain:
        body = clean(item.get("text"))
        for sentence in re.split(r"(?<=[。！？!?])|(?<=\.)\s+", body):
            if value.casefold() in sentence.casefold():
                # Apply the existing quality/numeric/status contract to the
                # actual containing sentence, preserving its source location.
                unit = dict(item, text=sentence, original_text=sentence, translated_text=None)
                if verify_claim_evidence(value, unit).get("support_status") == "supported":
                    return True
    return False


def _prospective(text):
    text = clean(text)
    if re.search(r"已经(?:证明|实现)|研究发现|结果(?:表明|显示)|材料(?:表明|显示)|显著(?:提高|改善)|results? shows?", text, re.I):
        return False
    question = re.search(r"如何|是否|能否|哪些|whether|how (?:to|can)|can .+\?", text, re.I)
    if question and not _ASSERTION.search(text[:question.start()]):
        return True
    if _ASSERTION.search(text):
        return False
    return bool(re.search(r"^(?:(?:先|再|随后|进一步|建议|拟|计划|优先|可先|应当|需要|并|同时|带证据编号的)\s*)*(?:核验|核对|复核|检验|验证|比较|对照|确认|评估|研究|整理|补齐|保留|test|verify|compare|evaluate|propos)|值得(?:研究|复核|核验)|待验证", text, re.I))


def _without_evidence_citations(text, allowed):
    """Ignore registered citation tokens, never quantities or real-world IDs."""
    identifiers = [i for i in allowed
                   if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*[-_:][A-Za-z0-9_.:-]+", i)
                   and not re.match(r"(?:CVE|ISO|RFC|PMID|DOI)[-_:]", i, re.I)]
    if not identifiers:
        return text
    alternatives = "(?:" + "|".join(re.escape(i) for i in sorted(identifiers, key=lambda i: (-len(i), i))) + ")"
    text = re.sub(r"[\[【(（]\s*" + alternatives + r"\s*[\]】)）]", "", text)
    # ASCII token boundaries permit Chinese citation labels such as 证据E-...中,
    # while preventing removal from a longer identifier or CVE-like suffix.
    return re.sub(r"(?<![A-Za-z0-9_.:-])" + alternatives + r"(?![A-Za-z0-9_.:-])", "", text)


def review_model_proposal(candidate, proposal, catalog):
    """Review each field with candidate-bound evidence; facts need direct quotes.

    The return contract remains (patch, audit, valid). Relevance can license a
    prospective research action, but cannot promote a new fact into the report.
    """
    audit = {"accepted": [], "rejected": []}
    if not isinstance(proposal, dict):
        return {}, {"accepted": [], "rejected": [{"field": "proposal", "reason": "invalid_proposal"}]}, False
    offered_candidate = proposal.get("candidate_id")
    if offered_candidate and str(offered_candidate) != str(candidate.get("candidate_id")):
        return {}, {"accepted": [], "rejected": [{"field": "candidate_id", "reason": "outside_candidate_scope"}]}, False
    raw_ids = proposal.get("evidence_ids") or []
    if not isinstance(raw_ids, (list, tuple)):
        return {}, {"accepted": [], "rejected": [{"field": "evidence_ids", "reason": "invalid_evidence_ids"}]}, False
    allowed = {str(i) for i in candidate.get("evidence_ids") or []}
    offered_ids = {str(i) for i in raw_ids}
    if offered_ids - allowed:
        return {}, {"accepted": [], "rejected": [{"field": "evidence_ids", "reason": "outside_candidate_scope"}]}, False
    sources = candidate.get("source_paths") or []
    originals = {str(e.get("evidence_id")): e for e in candidate.get("evidence_chain") or []
                 if str(e.get("evidence_id")) in allowed and _belongs(e, sources)}
    chain = []
    for item in catalog:
        identity = str(item.get("evidence_id"))
        original = originals.get(identity)
        if (not original or not _belongs(item, sources) or _evidence_path(item) != _evidence_path(original)
                or item.get("preview_only") or not evidence_quality(item).get("eligible")):
            continue
        excerpt = clean(item.get("text"))
        if not excerpt or excerpt not in clean(original.get("text")):
            continue
        # Only the actual catalog excerpt was shown to the model. An attached
        # full translation must not quietly authorize unseen extra assertions.
        chain.append(dict(item, text=excerpt, original_text=excerpt, translated_text=None))
    available_ids = {str(e["evidence_id"]) for e in chain}
    if offered_ids - available_ids:
        return {}, {"accepted": [], "rejected": [{"field": "evidence_ids", "reason": "unavailable_or_changed_candidate_evidence"}]}, False
    if offered_ids:
        chain = [e for e in chain if str(e["evidence_id"]) in offered_ids]
    if not chain:
        return {}, {"accepted": [], "rejected": [{"field": "proposal", "reason": "no_candidate_evidence"}]}, False
    if any(_GLOBAL.search(str(proposal.get(k) or "")) for k in ("rationale", "research_questions", "methods")):
        return {}, {"accepted": [], "rejected": [{"field": "proposal", "reason": "unsupported_global_novelty"}]}, False
    patch = {}
    for key in ("rationale", "research_questions", "methods"):
        raw = proposal.get(key)
        values = [raw] if key == "rationale" else raw if isinstance(raw, list) else []
        accepted = []
        for value in values[:4]:
            if not isinstance(value, str) or not value.strip():
                continue
            text = clean(value)
            review_text = _without_evidence_citations(text, allowed)
            reason = None
            # Treat each assertion separately: an action after a comma cannot
            # launder an unsupported factual premise before it.
            parts = [p.strip() for p in re.split(r"[，,；;。!?！？]|(?<=\.)\s+", review_text) if p.strip()]
            if _GLOBAL.search(text):
                reason = "unsupported_global_novelty"
            elif re.search(r"\d", review_text) and not all(_direct_fact(p, chain) for p in parts if re.search(r"\d", p)):
                reason = "unverified_numbers"
            elif any(not _prospective(p) and not _direct_fact(p, chain) for p in parts):
                reason = "unverified_factual_statement"
            elif not any(verify_inference_evidence(review_text, e).get("support_status") == "supported" for e in chain):
                reason = "no_local_relevance"
            if reason:
                audit["rejected"].append({"field": key, "text": text, "reason": reason})
            else:
                accepted.append(text)
                audit["accepted"].append({"field": key, "text": text,
                                          "status": "direct_quote_and_inference_relevance" if any(_direct_fact(p, chain) for p in parts) else "inference_relevance_only"})
        if accepted:
            patch[key] = accepted[0] if key == "rationale" else accepted
    return patch, audit, bool(patch.get("rationale"))
