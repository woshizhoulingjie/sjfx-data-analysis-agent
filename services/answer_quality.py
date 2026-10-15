"""Bounded, query-aware answer preparation. No I/O except a local tokenizer.

Coverage signals describe lexical candidates, never proof of entailment.
The generator remains responsible for composition; evidence retains provenance.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping

from config import Config


def enabled():
    return bool(getattr(Config, 'CONVERSATION_QUALITY_ENABLED', True))


STOP = {'资料', '文件', '文档', '问题', '这个', '什么', '如何', '是否', '为什么',
        '请问', '告诉', '说明', '一下', '详细', '分析', '哪些', 'the', 'and', 'is', 'are', 'of', 'in'}
ASPECTS = (
    ('实收数量', r'(?:实收|实际收到|已收到|收到|到货)[^。！？?\n]{0,18}(?:多少|几[台件份套辆箱]|数量)',
     r'(?:实收|实际收到|实际到货|已收到|收到|已到货)(?:到货)?(?:数量)?\s*(?:为|是|：|:|共|了)?\s*\d+(?:\.\d+)?\s*(?:台|件|份|套|辆|箱|个|吨|千克)'),
    ('缺少数量', r'(?:还缺|尚缺|缺少|缺口|未交付|待补交)[^。！？?\n]{0,12}(?:多少|几|数量)',
     r'(?:还缺|尚缺|缺少|缺口|未交付|待补交)(?:数量)?\s*(?:为|是|：|:|共)?\s*\d+(?:\.\d+)?\s*(?:台|件|份|套|辆|箱|个|吨|千克)'),
    ('原因', r'为什么|原因|为何|why|cause', r'原因|由于|因为|因.{1,30}(?:延期|故障|导致)|导致|造成|because|due to|cause'),
    ('影响', r'影响|后果|impact|consequence', r'影响|后果|损失|延误|增加|减少|impact|loss|delay'),
    ('下一步', r'怎么办|下一步|建议|措施|如何改|怎么改|recommend|next step', r'建议|措施|计划|应当|需要|下一步|recommend|plan'),
    ('条件与例外', r'条件|例外|前提|限制|exception|condition', r'条件|条款|例外|前提|除非|如果|若|仅限|不得|除外|condition|unless|except'),
    ('比较', r'比较|对比|差异|不同|compare|difference', r'比较|相比|差异|不同|分别|而|compare|different'),
)


def terms(text):
    result = set()
    for part in re.findall(r'[a-zA-Z][a-zA-Z0-9_.-]*|[\u4e00-\u9fff]+', str(text).lower()):
        if re.fullmatch(r'[\u4e00-\u9fff]+', part):
            result.update(part[i:i + 2] for i in range(len(part) - 1))
        else:
            result.add(part)
    return result - STOP


def relevance(query, text):
    query_terms = terms(query)
    return len(query_terms & terms(text)) / max(1, len(query_terms))


def question_aspects(query):
    return [{'name': name, 'signal': evidence_pattern}
            for name, pattern, evidence_pattern in ASPECTS
            if re.search(pattern, query, re.I)][:5]


def source_kind(item):
    location = item.get('location') or {}
    if item.get('preview_only') or (item.get('coverage') or {}).get('level') == 'preview':
        return 'preview'
    role = str(item.get('evidence_role') or '')
    label = str(item.get('label') or '')
    if role == 'structured_statistic' or label == 'table_statistics' or item.get('derivation'):
        return 'structured_statistic'
    if role == 'structured_record' or label == 'table_record':
        return 'structured_record'
    if ('summary' in str(location.get('source', '')) or '摘要' in role or 'summary' in role
            or 'summary' in label or item.get('evidence_level') == 'file'):
        return 'summary'
    return 'original'


def extract_passage(text, query, limit=1800):
    """Return exact, ordered source spans, keeping adjacent conditions intact."""
    text = str(text or '').strip()
    if limit <= 0:
        return ''
    if len(text) <= limit:
        return text
    # Sentence boundaries preserve decimal points, table rows and headings.
    spans = [m.span() for m in re.finditer(r'[^。！？!?\n]+[。！？!?\n]*', text)]
    if not spans:
        return text[:limit]
    ranked = sorted(range(len(spans)), key=lambda i: (-relevance(query, text[slice(*spans[i])]), i))
    selected = set()
    remaining = max(0, limit - 16)
    for index in ranked:
        a, b = spans[index]
        if b - a > remaining:
            # A single long paragraph is windowed around its strongest term.
            if not selected:
                hits = [text.lower().find(t, a, b) for t in terms(query)]
                hits = [p for p in hits if p >= 0]
                pos = min(hits) if hits else a
                start = max(a, min(pos - remaining // 3, b - remaining))
                return ('…' if start else '') + text[start:start + remaining] + '…'
            continue
        selected.add(index)
        remaining -= b - a
        for neighbor in (index - 1, index + 1):
            if 0 <= neighbor < len(spans) and neighbor not in selected:
                x, y = spans[neighbor]
                if y - x <= remaining:
                    selected.add(neighbor)
                    remaining -= y - x
        if remaining < 30:
            break
    result = []
    previous = -2
    for index in sorted(selected):
        if index != previous + 1:
            result.append('…')
        result.append(text[slice(*spans[index])])
        previous = index
    if previous < len(spans) - 1:
        result.append('…')
    return ''.join(result)[:limit]


def count_tokens(text):
    from services.model_budget import count_tokens as selected_count
    return selected_count(text, config=Config)


def token_count_method():
    from services.model_budget import token_count_method as selected_method
    return selected_method(config=Config)


def evidence_payload(item):
    return {
        'citation': item.get('citation_label'), 'source_path': item.get('source_path'),
        'location': item.get('location') or {}, 'source_kind': source_kind(item),
        'original_text': item.get('original_text') or '',
        'translated_text': item.get('translated_text') or None,
    }


def select_evidence(citations, policy, query=''):
    candidates = []
    for rank, raw in enumerate(citations):
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        item['original_text'] = extract_passage(raw.get('original_text'), query, policy.max_evidence_chars)
        if item.get('translated_text'):
            item['translated_text'] = extract_passage(item['translated_text'], query, policy.max_evidence_chars)
        item['source_kind'] = source_kind(item)
        if item['source_kind'] == 'preview':
            continue
        body = item['original_text']
        size = len(json.dumps(evidence_payload(item), ensure_ascii=False, separators=(',', ':')))
        candidates.append((item, rank, size, terms(body), relevance(query, body)))
    chosen = []
    used = 0
    seen_text = set()
    covered = set()
    counts = {}
    aspects = question_aspects(query)
    while candidates and len(chosen) < policy.max_prompt_evidence:
        scored = []
        for candidate in candidates:
            item, rank, size, concepts, rel = candidate
            body = item['original_text']
            if used + size > policy.max_model_evidence_chars or body in seen_text:
                continue
            fresh = sum(a['name'] not in covered and bool(re.search(a['signal'], body, re.I)) for a in aspects)
            redundancy = max((len(concepts & other) / max(1, len(concepts | other)) for other in (terms(x['original_text']) for x in chosen)), default=0)
            # Soft per-file preference: a single contract can still supply all clauses.
            repeated_file = counts.get(item.get('source_path'), 0) >= policy.max_model_evidence_per_file
            score = 2 * rel + 0.6 * fresh + 1 / (rank + 2) - 0.45 * redundancy - 0.2 * repeated_file
            score += 0.12 if source_kind(item) == 'original' else 0
            if item.get('large_global'):
                directories={str(x.get('source_path') or '').split('/')[0] for x in chosen}
                topics={x.get('topic_id') for x in chosen}
                directory=str(item.get('source_path') or '').split('/')[0]
                score += 2.0 if directory not in directories else 0
                score += .6 if item.get('topic_id') not in topics else 0
            scored.append((score, -rank, candidate))
        if not scored:
            break
        _, _, winner = max(scored, key=lambda x: (x[0], x[1]))
        candidates.remove(winner)
        item, _, size, _, _ = winner
        chosen.append(item)
        used += size
        seen_text.add(item['original_text'])
        counts[item.get('source_path')] = counts.get(item.get('source_path'), 0) + 1
        covered.update(a['name'] for a in aspects if re.search(a['signal'], item['original_text'], re.I))
    return chosen


def answer_contract(query, citations):
    aspects = question_aspects(query)
    rows = []
    for aspect in aspects:
        hits = [item for item in citations if re.search(aspect['signal'], item.get('original_text', ''), re.I)]
        rows.append({'aspect': aspect['name'], 'candidate_citations': [x['citation_label'] for x in hits],
                     'state': 'candidate_support' if hits else 'not_located'})
    instruction = '逐项回应用户问题。候选命中仅为词语信号，不代表事实充分；请核对条件、例外和矛盾。未定位不等于资料不存在。局部缺口不影响回答其他有依据部分。'
    if any(row['aspect'] in {'实收数量', '缺少数量'} for row in rows):
        instruction += ('数量问题分别写明实收与缺少的数值、单位及引用，不能用到货情况标题或验收状态代替数量。'
                        '每项数量单独成句并紧接来源引用；优先忠实引用含数量的原文短句，不在同一句追加原文没有直接绑定的主语、日期或编号。'
                        '优先用来源中的明确字段组成简洁事实句；独立记载的日期、编号和条件分别说明，不拼接成来源未明确记载的单一断言。'
                        '资料没有数量时明确说明未能确定，不从其他对象借用数值。')
    return {'required_aspects': rows, 'question': query, 'instruction': instruction}


def output_budget(query, intent):
    complex_task = len(question_aspects(query)) > 1 or intent in {'comparison', 'timeline', 'relationship', 'risk', 'multi_task', 'analysis', 'summary'}
    detailed = bool(re.search(r'详细|仔细|深入|全面|完整|具体|detail|thorough', query, re.I))
    budget = 2800 if detailed else (2200 if complex_task else 1400)
    return min(budget, int(getattr(Config, 'CONVERSATION_MAX_ANSWER_TOKENS', 3000)))


def missing_answer_aspects(answer, contract):
    """Flag explicit multi-part omissions; do not pretend to judge sufficiency."""
    rows = contract.get('required_aspects') or []
    if not rows or (len(rows) < 2 and rows[0].get('aspect') not in {'实收数量', '缺少数量'}):
        return []
    patterns = {name: signal for name, _, signal in ASPECTS}
    answer = str(answer or '').replace('**', '')
    return [row['aspect'] for row in rows
            if row.get('candidate_citations') and row.get('aspect') in patterns
            and not re.search(patterns[row['aspect']], answer, re.I)]


def claim_source(item, claim):
    """Use a relevant sentence for polarity checks, not unrelated negations."""
    result = dict(item)
    text = str(result.get('text') or result.get('original_text') or '')
    sentences = [s.strip() for s in re.split(r'(?<=[。！？!?])\s*|\n+', text) if s.strip()]
    if len(sentences) > 1:
        best = max(sentences, key=lambda sentence: relevance(claim, sentence))
        if relevance(claim, best) > 0:
            result['text'] = best
            result['original_text'] = best
            result.pop('translated_text', None)
    return result


NUMERIC_UNITS = r'万元|亿元|千克|公斤|毫米|厘米|公里|平方米|立方米|%|％|万|亿|吨|元|台|件|套|辆|箱|米|天|次|例|倍|项|种|个|份|人|年|月|日'
QUANTITY_VALUE = r'[+-]?\d+(?:\.\d+)?\s*(?:台|件|份|套|辆|箱|个|吨|千克|公斤|毫米|厘米|米)'
RECORD_ID = r'(?<![A-Za-z0-9])[A-Z][A-Z0-9]{0,15}(?:-\d+){1,3}(?![A-Za-z0-9-])'


def numeric_text(value):
    """Normalize valid three-digit grouping only, retaining signs and scale."""
    return re.sub(r'(?<![A-Za-z0-9_.,])([+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?)(?![\d,]|\.\d)',
                  lambda match: match[0].replace(',', ''), str(value or ''))


def _numeric_match_text(value):
    # Whitespace can separate two independent numbers (an ID and a date).
    # Normalize only a quantity's number/unit spacing, never join tokens.
    return re.sub(r'(?<=\d)\s+(?=(?:' + NUMERIC_UNITS + '))', '', value)


def numeric_evidence_text(value):
    """Bind only an explicit key's unit to its own value, never a nearby unit."""
    unit = r'万元|亿元|元|吨|千克|公斤|台|件|份|套|辆|箱|人|个|%|％'
    pattern = (r'(?P<key>[\u4e00-\u9fffA-Za-z_]{1,40}?)'
               r'(?:[（(](?P<wrapped>' + unit + r')[）)]|(?P<plain>' + unit + r'))'
               r'\s*[:：=]\s*(?P<number>[+-]?\d+(?:\.\d+)?)(?![\d.])')
    return re.sub(pattern, lambda m: m['key'] + '=' + m['number'] + (m['wrapped'] or m['plain']), numeric_text(value))


def _quantity_label_text(value):
    # Canonicalize explicit quantity labels only for matching, not citations.
    # Missing-count synonyms share a role; received and missing never do.
    # Keep prefixes (project/date, plans, negation) and the number/unit intact.
    pattern = (r'\*{0,2}(?P<role>实收|尚缺|还缺|缺少)(?:数量)?\*{0,2}\s*'
               r'(?:(?:为|是)\s*)?(?:[:：]\s*)?'
               r'(?=\*{0,2}' + QUANTITY_VALUE + r')')
    return re.sub(pattern, lambda match: '实收' if match['role'] == '实收' else '尚缺', value)


def _projects(value):
    return set(re.findall(r'[\u4e00-\u9fffA-Za-z0-9_-]{1,24}项目', value))


def _quantity_roles(value):
    roles = set()
    if re.search(r'实收|实际收到|已收到|已到货', value):
        roles.add('received')
    if re.search(r'尚缺|还缺|缺少', value):
        roles.add('missing')
    return roles


def _quantity_denials(value):
    # Compare the negation attached to each canonical role independently of
    # unrelated negations elsewhere in the clause. Unconfirmed is not denied.
    return set(re.findall(
        r'(?:未|没有|不|非|无)(?:曾经|已经|真正|确实|实际|仍然|仍|再|曾|是|为|被|\s){0,3}'
        r'(实收|尚缺)', value))


def _quantity_context_clauses(evidence, boundary):
    """Carry identity/date metadata, never another clause's facts or amounts.

    Only explicit short records seed context. A new project or record date
    replaces it; field/line boundaries and distance expire it. Context is an
    internal matching aid, never generated prose or a rewritten citation.
    """
    pieces = re.split('(' + boundary + r'|(?<=[.!?])\s+)', evidence, flags=re.I)
    output, projects, identifiers, date = [], set(), {}, ''
    offset, origin, age = 0, -1000, 0
    for index in range(0, len(pieces), 2):
        clause = pieces[index]
        separator = pieces[index + 1] if index + 1 < len(pieces) else ''
        age += 1
        if offset - origin > 500 or age > 8:
            projects, identifiers, date = set(), {}, ''
        current = _projects(clause)
        if current and current != projects:
            projects, identifiers, date = set(), {}, ''
        record = bool(re.search(r'记录|台账|清单', clause))
        disallowed = bool(re.search(r'无关|不涉及|若|如果|假设|假如', clause))
        if record and re.search(r'计划|预计|希望|拟', clause):
            disallowed = True
        quantity = bool(re.search(QUANTITY_VALUE, clause))
        field_only = bool(re.match(r'^\s*(?:计划交付|实收|尚缺|还缺|缺少|已收到|实际收到|已到货)', clause))
        if disallowed or (quantity and not field_only):
            projects, identifiers, date = set(), {}, ''
        if len(current) == 1 and record and not disallowed:
            projects, origin, age = current, offset, 0
        dated = re.search(r'(?:记录时间|记录日期|巡检日期|统计日期)\s*[:：]?\s*(\d{4}年\d{1,2}月\d{1,2}日)', clause)
        if dated and not disallowed:
            date, origin, age = dated[1], offset, 0
        if (projects or date) and not disallowed:
            for identifier in re.findall(RECORD_ID, clause):
                identifiers[identifier.split('-')[0]] = identifier
        if quantity and field_only and (projects or date) and not disallowed:
            context = ' '.join(sorted(projects) + list(identifiers.values()) + ([date] if date else []))
            output.append(clause + ' ' + context)
        else:
            output.append(clause)
        offset += len(clause) + len(separator)
        if re.search(r'[\n;；|]', separator):
            projects, identifiers, date = set(), {}, ''
    return output


def numeric_relation_supported(claim, evidence, numbers):
    """Conservative local entity/attribute check, not an entailment model.

    Each claimed number needs its own matching clause. No cross-clause union
    can transfer Beta's amount to Alpha. Unknown relations stay unverified.
    """
    clean = _quantity_label_text(numeric_text(re.sub(r'\[\d+\]', '', claim)))
    evidence = _quantity_label_text(numeric_evidence_text(evidence))
    # Chinese commas always separate clauses. Only a comma between two digits
    # can be a numeric separator; a lead-in followed by ", 63.1%" is not one.
    boundary = r'[。！？!?;；\n，|]|\band\b|(?<!\d),|,(?!\d)'
    clauses = _quantity_context_clauses(evidence, boundary)
    claim_clauses = re.split(boundary, clean, flags=re.I)
    for number in numbers:
        value = re.sub(r'\s+', '', numeric_text(number))
        suffix = r'(?!\d|\.\d)' if value[-1:].isdigit() else ''
        pattern = re.compile(r'(?<![\d.])' + re.escape(value) + suffix)
        locals_for_value = [c for c in claim_clauses if pattern.search(_numeric_match_text(c))]
        if not locals_for_value or not all(_numeric_clause_supported(local, clauses, pattern) for local in locals_for_value):
            return False
    return True


def _numeric_clause_supported(local, clauses, pattern):
    # Every occurrence must retain its field/entity binding. A first valid
    # occurrence cannot validate the same value reused for another object.
    normalized = local.replace('的', '')
    normalized = re.sub(r'[+-]?\d+(?:\.\d+)?\s*(?:' + NUMERIC_UNITS + ')?', ' ', normalized)
    anchors = terms(normalized) - {'is', 'was', '为', '万元'}
    attributes = set(re.findall(r'预算|收入|支出|成本|利润|人数|面积|温度|交付|付款|验收|budget|income|cost|profit|temperature', local, re.I))
    dates = re.findall(r'\d{4}年(?:\d{1,2}月)?(?:\d{1,2}日)?', local)
    # Named English entities and explicit Chinese subject prefixes are hard anchors.
    names = set(re.findall(r'\b[A-Z][a-zA-Z]{2,}\b', local)) - {'The', 'This', 'According'}
    subject = re.search(r'([\u4e00-\u9fffA-Za-z0-9_-]{1,24}?)(?:的)?(?:预算|金额|付款|收入|支出|成本|利润|人数)', local)
    subject = subject.group(1).strip('该本其中') if subject else ''
    projects = _projects(local)
    identifiers = set(re.findall(RECORD_ID, local))
    roles = _quantity_roles(local)
    for clause in clauses:
        flat = _numeric_match_text(clause)
        if not pattern.search(flat):
            continue
        if projects and projects != _projects(clause):
            continue
        if identifiers and not identifiers.issubset(set(re.findall(RECORD_ID, clause))):
            continue
        if roles and roles != _quantity_roles(clause):
            continue
        if roles and _quantity_denials(local) != _quantity_denials(clause):
            continue
        if roles and (bool(re.search(r'计划|预计|希望|拟', local)) !=
                      bool(re.search(r'计划|预计|希望|拟', clause))):
            continue
        if names and not all(name.lower() in clause.lower() for name in names):
            continue
        if subject and subject not in clause:
            continue
        if attributes and not all(a.lower() in clause.lower() for a in attributes):
            continue
        positions = [clause.find(date) for date in dates]
        if dates and (min(positions) < 0 or positions != sorted(positions)):
            continue
        overlap = len(anchors & terms(clause.replace('的', ''))) / max(1, len(anchors))
        if anchors and overlap < (0.30 if len(dates) >= 2 else 0.45):
            continue
        if bool(re.search(r'未|没有|不再|not\b', local, re.I)) != bool(re.search(r'未|没有|不再|not\b', clause, re.I)):
            continue
        return True
    return False
