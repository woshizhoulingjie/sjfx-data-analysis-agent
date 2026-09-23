"""Bounded, source-linked evidence for isolated-package previews and naming.

No model calls, original-file reads, or changes to the embedding contract.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

VERSION = 'naming-evidence/2'
FEATURE_VERSION = 'naming-features/2'
MAX_CANDIDATES = 64
MAX_BLOCKS = 16
MAX_TERMS = 40
_SPACE = re.compile(r'[^\S\n]+')
_SENTENCE = re.compile(r'(?<=[.!?;])\s+|(?<=[。！？；])|\n+')
_NOISE = re.compile(r'版权所有|免责声明|all rights reserved|copyright\s*[©(]|'
                    r'^\s*(?:received\s*:|issn\s*:|doi\s*:)|'
                    r'^\s*(?:第\s*\d+\s*页|\d+\s*/\s*\d+)\s*$', re.I)
_TERM = re.compile(r'[A-Za-z][A-Za-z0-9_+.-]{1,39}|[\u4e00-\u9fff]{2,40}')
_STOP = frozenset('the and for with from this that these those are was were have has '
                  'been into about their there which where when then than also only '
                  'not can could would should will may must abstract keywords '
                  '本文 其中 以及 进行 通过 相关 内容 资料 文件 目录 摘要 说明'.split())
_EDGE_STOP = frozenset('的了是在与和及或而为于将把被从对由并等其该本这那以个中后前')


def normalize(value):
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(value or ''))
    return '\n'.join(_SPACE.sub(' ', line).strip() for line in text.splitlines()).strip()


def is_boilerplate(value):
    return bool(_NOISE.search(str(value or '')))


def quality(text):
    text = str(text or '')
    if not text:
        return 0.0
    useful = sum(ch.isalnum() for ch in text) / len(text)
    damage = (text.count('\ufffd') + text.count('[共享字符串未采样]') * 12) / len(text)
    return max(0.0, min(1.0, useful - 3 * damage))


def _units(text):
    found = set()
    for match in _TERM.finditer(text):
        word = match.group().casefold()
        if word in _STOP:
            continue
        found.add(word)
        if re.fullmatch(r'[\u4e00-\u9fff]{5,}', word):
            found.update(word[i:i+4] for i in range(len(word)-3))
    return found


def _pieces(text, limit=700):
    """Keep sentence boundaries; oversized sentences retain an explicit cut flag."""
    for sentence in _SENTENCE.split(normalize(text)):
        sentence = sentence.strip()
        if not sentence:
            continue
        # Bounded candidate generation even for a minified or unpunctuated page.
        if len(sentence) <= limit:
            yield sentence, False
        else:
            for offset in sorted({0, max(0, len(sentence)//2-limit//2), max(0, len(sentence)-limit)}):
                yield sentence[offset:offset+limit], True


def candidates(parts, max_candidates=MAX_CANDIDATES):
    """Stratify within each stored block before ranking across the document."""
    pool = []
    for ordinal, part in enumerate((parts or [])[:64]):
        if not isinstance(part, dict) or part.get('kind') in {'attachment', 'member_names'}:
            continue
        raw = str(part.get('text') or '')
        if not raw.strip():
            continue
        source_cut = len(raw) > 24000
        raw = raw[:24000]  # parser outputs are already bounded; cap legacy input too
        pieces = list(_pieces(raw))[:128]
        ranked = []
        for index, (value, cut) in enumerate(pieces):
            kind = part.get('kind') or 'unknown'
            structure = 1.0 if kind in {'heading', 'title', 'fields', 'abstract'} else 0.0
            intent = .5 if re.search(r'摘要|目的|结论|结果|提出|用于|abstract|objective|conclusion', value, re.I) else 0
            score = quality(value) + structure + intent + min(len(_units(value)), 20)/40
            if is_boilerplate(value) or kind in {'footer', 'navigation'}:
                score -= 3
            block = dict(part, text=value, raw_text=value, kind=kind,
                         truncated=bool(part.get('truncated') or cut or source_cut),
                         block_index=ordinal, sentence_index=index, quality=round(quality(value), 3))
            block['id'] = part.get('id') or 'B' + hashlib.sha256(
                (str(part.get('location', ''))+'\0'+value).encode()).hexdigest()[:12]
            ranked.append((score, index, block))
        # Preserve high-value and spatially different sentences from every block.
        selected = sorted(ranked, key=lambda x: (-x[0], x[1]))[:4]
        if ranked:
            for item in (ranked[0], ranked[len(ranked)//2], ranked[-1]):
                if item not in selected:
                    selected.append(item)
        pool.extend(selected)
    pool.sort(key=lambda x: (-x[0], x[2]['block_index'], x[1]))
    return [p for _, _, p in pool[:max_candidates]]


def select_blocks(parts, limit=8, chars=4000, terms=()):
    pool = candidates(parts)
    chosen, seen, used = [], set(), 0
    anchors = {str(t).casefold() for t in terms if str(t)}
    # Tokenize each candidate once, rather than once for every greedy comparison.
    prepared = []
    for block in pool:
        value = block['text']
        key = re.sub(r'\s+', ' ', value).casefold()
        structure = .7 if block['kind'] in {'title', 'heading', 'fields', 'abstract'} else 0
        penalty = 3 if is_boilerplate(value) or block['kind'] in {'footer', 'navigation'} else .6 if block['kind'] in {'header', 'quoted_history'} else .3 if block['kind'] == 'derived_summary' else 0
        score = quality(value) + structure + min(4, sum(t in value.casefold() for t in anchors))*.12 - penalty
        prepared.append((block, key, _units(value), score))
    pool = prepared
    selected_units = []
    while pool and len(chosen) < limit:
        best = None
        for index, (block, key, units, base_score) in enumerate(pool):
            if key in seen:
                continue
            overlap = max((len(units & previous) / max(1, len(units | previous))
                           for previous in selected_units), default=0)
            new_location = .25 if all(p.get('location') != block.get('location') for p in chosen) else 0
            score = base_score + new_location - overlap
            if best is None or score > best[0]:
                best = (score, index, key)
        if best is None:
            break
        _, index, key = best
        block, _, units, _ = pool.pop(index)
        room = chars - used
        if room < 40:
            break
        if len(block['text']) > room:
            # Do not fill the remaining room with an arbitrary partial sentence.
            if chosen:
                continue
            block = dict(block, text=block['text'][:room], truncated=True)
        chosen.append(block)
        selected_units.append(units)
        seen.add(key)
        used += len(block['text']) + 1
    return chosen


def compact_text(parts, chars=1800, terms=()):
    return '\n'.join(p['text'] for p in select_blocks(parts, chars=chars, terms=terms))


def trim_reservoir(parts):
    """Cheap quality/position reservoir maintenance during streaming parsing."""
    if len(parts) <= 64:
        return
    ranked = sorted(range(len(parts)), key=lambda i: (
        -(quality(parts[i]['text']) + (.7 if parts[i].get('kind') in {'title','heading','fields'} else 0)
          - (3 if is_boilerplate(parts[i]['text']) else 0)), i))
    keep = set(ranked[:24])
    rest = [i for i in range(len(parts)) if i not in keep]
    keep.update(rest[round(j*(len(rest)-1)/23)] for j in range(24))
    parts[:] = [parts[i] for i in sorted(keep)]


def record(parts, location, text, kind='text', **metadata):
    """Store selected source spans, not only the prefix of a parsed page."""
    raw = normalize(text)
    if not raw:
        return
    if kind in {'attachment', 'member_names'}:
        # Preserve inventory metadata for the existing preview; never name from it.
        parts.append(dict(text=raw[:1800], location=location, kind=kind,
                          preview_only=True, truncated=len(raw)>1800, **metadata))
        if len(parts) > 64:
            parts[:] = parts[:32] + parts[-32:]
        return
    selected = select_blocks([dict(text=raw, location=location, kind=kind, **metadata)],
                             limit=4, chars=1800)
    for block in selected:
        block.update(preview_only=True, reader_version=VERSION)
        block['truncated'] = bool(block.get('truncated') or len(raw) > sum(len(p['text']) for p in selected))
        # A bounded reservoir lets later pages compete with earlier metadata.
        parts.append(block)
    trim_reservoir(parts)


def row_parts(row):
    local = row.get('local_summary') or {}
    info = local.get('portrait') or {}
    if row.get('sensitive') or info.get('recognition_status') == 'restricted':
        return []
    parts = [p for p in info.get('evidence', []) if isinstance(p, dict)]
    deep = row.get('deep_summary') or {}
    if row.get('deep_status') == 'completed' and deep.get('summary_type') != 'local_deep_failed':
        if deep.get('text_preview'):
            parts = parts + [dict(text=deep['text_preview'], location='已保存深度正文', kind='text')]
        elif deep.get('summary'):
            parts = parts + [dict(text=deep['summary'], location='已有模型摘要', kind='derived_summary')]
    if not parts and local.get('sample'):
        parts = [dict(text=local['sample'], location='历史内容预览', kind='unknown')]
    return parts


def make_card(row):
    """At most 40 source-derived terms. Absence means not observed, not absent."""
    local = row.get('local_summary') or {}
    info = local.get('portrait') or {}
    parts = row_parts(row)
    selected = select_blocks(parts, limit=12, chars=4800)
    text = '\n'.join(p['text'] for p in selected if not is_boilerplate(p['text']))
    fields = [normalize(x)[:100] for x in info.get('fields', [])[:20]]
    headings = [normalize(x)[:100] for x in info.get('headings', [])[:12]]
    if row.get('sensitive') or info.get('recognition_status') == 'restricted':
        fields, headings, text = [], [], ''
    weights = Counter()
    for value, boost in [(text, 1)] + [(x, 4) for x in fields + headings]:
        if is_boilerplate(value):
            continue
        words = list(_TERM.finditer(value))[:1500]
        for match in words:
            word = match.group().casefold()
            if word in _STOP:
                continue
            if re.fullmatch(r'[\u4e00-\u9fff]+', word):
                if len(word) <= 12:
                    weights[word] += boost * 2
                for n in (2, 3, 4, 6, 8):
                    for start in range(max(0, len(word)-n+1)):
                        term = word[start:start+n]
                        if term[0] not in _EDGE_STOP and term[-1] not in _EDGE_STOP and term not in _STOP:
                            weights[term] += boost * (1 + n/8)
            else:
                weights[word] += boost
        for a, b in zip(words, words[1:]):
            if (a.group().isascii() and b.group().isascii() and
                    value[a.end():b.start()].isspace() and
                    a.group().casefold() not in _STOP and b.group().casefold() not in _STOP):
                weights[a.group().casefold()+' '+b.group().casefold()] += boost * 2
    ranked = sorted(weights, key=lambda t: (-weights[t], -len(t), t))
    terms = []
    for term in ranked:
        if len(term) > 64 or any(term in longer and weights[term] <= weights[longer]*1.25 for longer in terms):
            continue
        terms.append(term)
        if len(terms) >= MAX_TERMS:
            break
    payload = {'version': FEATURE_VERSION, 'terms': terms,
               'readable': bool((len(text) >= 20 and quality(text) >= .2) or len([f for f in fields if f and '[共享字符串未采样]' not in f]) >= 2),
               'quality': round(quality(text), 3),
               'headings': headings[:6], 'fields': fields[:8],
               'coverage': 'sampled', 'term_scope': 'retained_features'}
    payload['digest'] = hashlib.sha256(json.dumps(payload, ensure_ascii=False,
                                                 sort_keys=True).encode()).hexdigest()
    return payload


def naming_excerpt(parts, tokens, count, clip, terms=()):
    """One token allocation; retain locations only for text actually supplied."""
    selected = select_blocks(parts, limit=8, chars=max(1000, tokens*6), terms=terms)
    kept, remaining = [], tokens
    for block in selected:
        text = block['text']
        cost = count(text+'\n')
        if cost > remaining:
            if kept:
                continue
            text = clip(text, max(1, remaining-2))
            block = dict(block, truncated=True)
            cost = count(text+'\n')
        if text.strip() and cost <= remaining:
            kept.append(dict(block, text=text))
            remaining -= cost
    return '\n'.join(p['text'] for p in kept), kept
