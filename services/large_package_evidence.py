"""Bounded, source-linked evidence for isolated-package previews and naming.

No model calls, original-file reads, or changes to the embedding contract.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

VERSION = 'naming-evidence/7'
FEATURE_VERSION = 'naming-features/6'
MAX_CANDIDATES = 64
MAX_BLOCKS = 16
MAX_TERMS = 40
_SPACE = re.compile(r'[^\S\n]+')
_SENTENCE = re.compile(r'(?<=[.!?;])\s+|(?<=[。！？；])|\n+')
_NOISE = re.compile(r'^\s*(?:版权所有|免责声明)\s*[:：]|'
                    r'^\s*(?:Copyright\s+)?all rights reserved\.?\s*$|copyright\s*(?:©|\(\s*c\s*\))|'
                    r'^\s*(?:received\s*:|issn\s*:|doi\s*:)|'
                    r'^\s*(?:第\s*\d+\s*页|\d+\s*/\s*\d+)\s*$', re.I)
# These are publication notices, not the words "copyright" or "license" in
# substantive research. PDF extraction often joins words in these fixed spans.
_PREPRINT_NOTICE = re.compile(
    r'^\s*(?:\.?\s*CC[- ]BY(?:[- ]NC)?(?:[- ]ND)?\s+\d+(?:\.\d+)?\s+'
    r'International\s+licen[cs]e\s*(?:made\s*)?available\s+under\s+a\s*\.?|'
    r'(?:CC[- ]BY(?:[- ]NC)?(?:[- ]ND)?\s+\d+(?:\.\d+)?\s*)?'
    r'(?:The\s+)?copyright\s+holder\s+for\s+this\s+preprint(?:\s|this|\s*\()|'
    r'(?:Table\s+[IVX]+\s+)?\(?\s*(?:which\s+)?was\s+not\s+certified\s+by\s+peer\s+review\)\s+is\s+the\s+author/funder|'
    r'https?://doi\.org/\S+\s*doi:\s*(?:bio|med)Rxiv\s+preprint\s*$)', re.I)
_LAYOUT_NUMBER = re.compile(r'^\s*(?:\d{1,6}|(?:Page|(?:bio|med)Rxiv)\s+\d+\s+of\s+\d+|第\s*\d+\s*页)\s*$', re.I)
_STRUCTURED_KINDS = frozenset({'fields', 'record', 'table', 'date', 'amount'})
_PUBLICATION_META = frozenset({'preprint', 'preprints', 'biorxiv', 'medrxiv', 'doi',
                              'copyright', 'license', 'licenseavailable', 'licensemade',
                              'certified', 'funder', 'perpetuity', 'reuse'})
_TERM = re.compile(r'[A-Za-z][A-Za-z0-9_+.-]{1,39}|[\u4e00-\u9fff]{2,40}')
_STOP = frozenset('the and for with from this that these those are was were have has '
                  'been into about their there which where when then than also only '
                  'not can could would should will may must abstract keywords '
                  '本文 其中 以及 进行 通过 相关 内容 资料 文件 目录 摘要 说明'.split())
_EDGE_STOP = frozenset('的了是在与和及或而为于将把被从对由并等其该本这那以个中后前')

# Match fixed publication clauses, never the topic words copyright/license.
# Each match stops at its sentence/line boundary; adjacent research survives.
_NOTICE_SPANS = re.compile(
    r'(?:CC[- ]BY(?:[- ]NC)?(?:[- ]ND)?\s+\d+(?:\.\d+)?\s*)?(?:The\s+)?copyright\s+holder\s+for\s+this\s+preprint[^\n]{0,400}?(?:\.(?=\s+[A-Z]|\n|$)|(?=\n|$))|'
    r'(?:Table\s+[IVX]+\s+)?\(?\s*(?:which\s+)?was\s+not\s+certified\s+by\s+peer\s+review\)\s+is\s+the\s+author/funder[^\n.!?]{0,250}(?:[.!?]|(?=\n|$))|'
    r'\.?\s*CC[- ]BY(?:[- ]NC)?(?:[- ]ND)?\s+\d+(?:\.\d+)?\s+International\s+licen[cs]e\s*(?:made\s*)?available\s+under\s+a\s*\.?|'
    r'https?://doi\.org/\S+\s*doi:\s*(?:bio|med)Rxiv\s+preprint\b|'
    r'(?:Publisher[’\x27]s\s+[Nn]ote\s*[:：]?\s*)?Springer\s+Nature\s+remains\s+neutral\s+with\s+regard\s+to[^\n.!?]{0,220}(?:[.!?]|(?=\n|$))|'
    r'jurisdictional\s+claims\s+in\s+published\s+maps\s+and\s+institutional\s+af[ﬁf]liations\.?|'
    r'(?:This\s+(?:article\s+is\s+licensed|is\s+an\s+open\s+access\s+article)|Open\s+Access\s+This\s+article\s+is\s+licensed)\s+under\s+(?:a\s+|the\s+terms\s+of\s+the\s+)?Creative\s+Commons[^\n!?]{0,650}?(?:\.(?=\s+[A-Z]|\n|$)|(?=\n|$))|'
    r'(?:If\s+material\s+is\s+not\s+included|(?:The\s+images\s+or\s+)?other\s+third\s+party\s+material\s+in\s+this\s+article\s+are\s+included)[^\n.!?]{0,500}(?:[.!?]|(?=\n|$))|'
    r'(?:in\s+the\s+|article[’\x27]s\s+|(?:Cre|ative)\s+)?(?:Creative\s+)?Commons\s+licen[cs]e,\s+unless\s+indicated\s+otherwise\s+in\s+a\s+credit\s+line[^\n.!?]{0,140}(?:[.!?]|(?=\n|$))|'
    r'(?:is\s+not\s+included\s+in\s+the\s+|in\s+the\s+)?article[’\x27]s\s+Creative\s+Commons\s+licen[cs]e\s+and\s+your\s+intended\s+use\s+is\s+not\s+permitted[^\n.!?]{0,250}(?:[.!?]|(?=\n|$))|'
    r'International\s+Licen[cs]e,\s+which\s+permits\s+any\s+(?:non-commercial\s+)?use,\s+sharing[^\n.!?]{0,350}(?:[.!?]|(?=\n|$))|'
    r'Non-commercial\s+uses\s+of\s+the\s+work\s+are\s+permitted\s+without\s+any\s+further\s+permission\s+from\s+Dove\s+Medical\s+Press[^\n.!?]{0,300}(?:[.!?]|(?=\n|$))|'
    # Saved PDF windows can start halfway through these fixed licence clauses.
    # Match their distinctive combinations, not "sharing" or "permission" alone.
    r'(?:permits\s+(?:any\s+)?(?:non-commercial\s+)?use,\s*sharing,\s*(?:adaptation,\s*)?|sharing,\s*adaptation,\s*)distribution\s+and\s+reproduction\s+in\s+any\s+medium\s+or\s+format[^\n.!?]{0,300}(?:[.!?]|(?=\n|$))|'
    r'any\s+medium\s+or\s+format,\s+as\s+long\s+as\s+you\s+give\s+appropriate\s+credit\s+to\s+the\s+original\s+author(?:\(s\)|s)?[^\n.!?]{0,250}(?:[.!?]|(?=\n|$))|'
    r'(?:as\s+you\s+)?give\s+appropriate\s+credit\s+to\s+the\s+original\s+author(?:\(s\)|s)?\s+and\s+the\s+source,\s+provide\s+a\s+link\s+to\s+the\s+Creative\s+Commons\s+licen[cs]e[^\n.!?]{0,180}(?:[.!?]|(?=\n|$))|'
    r'permitted\s+by\s+statutory\s+regulation\s+or\s+exceeds\s+the\s+permitted\s+use,\s+you\s+will\s+need\s+to\s+obtain\s+permission\s+directly\s+from\s+the\s+copyright\s+holder\.?|'
    r'(?:to\s+)?jurisdictional\s+claims\s+in\s+published\s+maps\s+and\s+institutional(?=\s*(?:\n|$))|'
    r'(?:You\s+do\s+not\s+have\s+)?permission\s+under\s+this\s+licen[cs]e\s+to\s+share\s+adapted\s+material\s+derived\s+from\s+this\s+article\s+or\s+parts\s+of\s+it\.?|'
    r'Disclaimer/Publisher[’\x27]s\s+Note:\s+The\s+statements,\s+opinions\s+and\s+data\s+contained[^\n.!?]{0,500}(?:[.!?]|(?=\n|$))|'
    r'The\s+(?:publisher|MDPI|editors?)[^\n.!?]{0,100}\s+disclaim[^\n.!?]{0,350}(?:[.!?]|(?=\n|$))|'
    r'(?:All\s+rights\s+reserved\.?\s*)?No\s+reuse\s+allowed\s+without\s+permission\.?', re.I)
_PUBLICATION_LABEL = re.compile(
    r'^(?:(?:REVIEW|ARTICLE)\s+)?Open\s+Access$|'
    r'^(?:the\s+)?(?:article[’\x27]s\s+)?Creative\s+Commons\s+licen[cs]e$|'
    r'^Springer\s+Nature$|^Publisher[’\x27]s\s+Note$', re.I)
_NOTICE_HINTS = ('commons', 'copyright holder', 'springer', 'jurisdiction',
                 'publisher', 'no reuse', 'cc-by', 'cc by', 'peer review',
                 'international license', 'international licence', 'non-commercial uses',
                 'permission under this', 'permits use', 'sharing, adaptation',
                 'distribution and reproduction', 'appropriate credit',
                 'permitted by statutory regulation')
_NOTICE_RESEARCH_CONTEXT = re.compile(r'copyright|licen[cs]|publisher|publishing|open\s+access|publication\s+notice', re.I)
_REFERENCE_HEADING = re.compile(r'^\s*(?:\d+[. ]+)?(?:references|bibliography|literature\s+cited|参考文献|引用文献)\s*[:：]?\s*$', re.I)
_BODY_HEADING = re.compile(r'^\s*(?:(?:\d+[. ]+)?(?:abstract|introduction|methods|results|discussion|conclusions?)|摘要|正文|方法|结果|结论)\s*[:：]?\s*$', re.I)
_REFERENCE_ENTRY = re.compile(
    r'^\s*(?:\[\d{1,4}\]|\d{1,4}[.)])\s+[A-Z][A-Za-z’\x27-]+\s*,?\s+(?:[A-Z]{1,4}\b(?=\s*[,.;])|[A-Z]\.)|'
    r'^\s*[A-Z][A-Za-z’\x27-]+,\s+(?:[A-Z]\.\s*){1,3}[^\n]{0,180}\b(?:19|20)\d{2}\b')


def clean_publication_spans(value):
    """Remove recognizable notice spans, preserving all adjacent source text.

    This pure projection never edits stored portraits, summaries or vectors.
    """
    text = str(value or '')
    low = text.casefold()
    if any(hint in low for hint in _NOTICE_HINTS):
        def replace(match):
            # Quoted clauses and clauses discussed by copyright research are
            # evidence in their own right. Do not turn this into a word ban.
            prefix = text[max(0, match.start() - 240):match.start()]
            sentence_prefix = re.split(r'[.!?\n]', prefix)[-1]
            if (prefix.endswith(('“', '"', '‘', "'"))
                    or _NOTICE_RESEARCH_CONTEXT.search(sentence_prefix)):
                return match.group()
            return ' '
        text = _NOTICE_SPANS.sub(replace, text)
    return normalize(text)


def _source_sections(raw, *, normalized=False):
    """Reference context is local to one stored block, never sampling order.

    A bibliography heading or multiple numbered author entries establishes a
    reference run; a subsequent body heading resets it. Authorless citation
    titles alone are deliberately insufficient evidence for classification.
    """
    lines = (str(raw) if normalized else normalize(raw)).splitlines()
    if len(lines) == 1 and not _REFERENCE_HEADING.fullmatch(lines[0]):
        yield lines[0], bool(_REFERENCE_ENTRY.search(lines[0]))
        return
    starts = [i for i, line in enumerate(lines) if _REFERENCE_ENTRY.search(line)]
    run_start = starts[0] if len(starts) >= 2 else None
    reference = False
    for index, line in enumerate(lines):
        if _REFERENCE_HEADING.fullmatch(line):
            reference = True
            continue
        if _BODY_HEADING.fullmatch(line):
            reference = False
        elif run_start is not None and index == run_start:
            reference = True
        yield line, reference or bool(_REFERENCE_ENTRY.search(line))


def normalize(value):
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(value or ''))
    return '\n'.join(_SPACE.sub(' ', line).strip() for line in text.splitlines()).strip()


def is_boilerplate(value):
    text = str(value or '')
    low = text.casefold()
    return bool(_NOISE.search(text) or _PREPRINT_NOTICE.search(text)
                or (any(hint in low for hint in _NOTICE_HINTS)
                    and clean_publication_spans(text) != normalize(text)))


def is_publication_metadata(value, source_text=None, kind=None):
    """Known notices cannot return through headings or ungrounded keywords.

    A bare publication word remains useful when substantive body text actually
    supports it (for example research about copyright or preprint servers).
    """
    text = normalize(value)
    if is_boilerplate(text) or (kind == 'heading' and _LAYOUT_NUMBER.fullmatch(text)):
        return True
    if source_text is None or (text.casefold() not in _PUBLICATION_META
                               and not _PUBLICATION_LABEL.fullmatch(text)):
        return False
    supported = (r'licen[cs](?:e[sd]?|ing)' if text.casefold() == 'license'
                 else re.escape(text))
    return not re.search(r'(?<![A-Za-z])' + supported + r'(?![A-Za-z])',
                         str(source_text or ''), re.I)


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
    document_notice_removed = False
    # Repeated page headers and sampled windows often contain identical text.
    # Reuse only pure text features, never source metadata or ranking results.
    # Bound this per-call cache; nothing persists across documents or users.
    text_features = {}
    for ordinal, part in enumerate((parts or [])[:64]):
        if not isinstance(part, dict) or part.get('kind') in {'attachment', 'member_names'}:
            continue
        raw = str(part.get('text') or '')
        if not raw.strip():
            continue
        source_cut = len(raw) > 24000
        raw = raw[:24000]  # parser outputs are already bounded; cap legacy input too
        kind = part.get('kind') or 'unknown'
        normalized = normalize(raw)
        cleaned = clean_publication_spans(normalized) if kind not in _STRUCTURED_KINDS else normalized
        notice_removed = cleaned != normalized
        document_notice_removed = document_notice_removed or notice_removed
        # Apply layout filtering before the existing sentence cap: some PDF
        # extractors emit every margin line number before the article body.
        pieces = [(value, cut, reference or part.get('evidence_role') == 'reference'
                   or kind in {'reference', 'references', 'bibliography'})
                  for section, reference in _source_sections(cleaned, normalized=True)
                  for value, cut in _pieces(section)
                  if kind in _STRUCTURED_KINDS or not (_LAYOUT_NUMBER.fullmatch(value) or _PREPRINT_NOTICE.search(value))][:128]
        ranked = []
        for index, (value, cut, reference) in enumerate(pieces):
            # Pure page/line numbers have quality=1.0 because every character
            # is alphanumeric. Numeric records retain their structure above.
            structure = 1.0 if kind in {'heading', 'title', 'fields', 'abstract'} else 0.0
            features = text_features.get(value)
            if features is None:
                intent = .5 if re.search(r'摘要|目的|结论|结果|提出|用于|abstract|objective|conclusion', value, re.I) else 0
                features = (quality(value), intent, min(len(_units(value)), 20), is_boilerplate(value))
                if len(text_features) < 256:
                    text_features[value] = features
            source_quality, intent, term_count, boilerplate = features
            score = source_quality + structure + intent + term_count/40
            if boilerplate or kind in {'footer', 'navigation'}:
                score -= 3
            if reference:
                score -= 2.5
            block = dict(part, text=value, raw_text=value, kind=kind,
                         truncated=bool(part.get('truncated') or cut or source_cut),
                         block_index=ordinal, sentence_index=index, quality=round(source_quality, 3))
            block['id'] = part.get('id') or 'B' + hashlib.sha256(
                (str(part.get('location', ''))+'\0'+value).encode()).hexdigest()[:12]
            if reference:
                block['evidence_role'] = 'reference'
            if notice_removed:
                block['publication_notice_removed'] = True
            ranked.append((score, index, block))
        # Preserve high-value and spatially different sentences from every block.
        selected = sorted(ranked, key=lambda x: (-x[0], x[1]))[:4]
        if ranked:
            for item in (ranked[0], ranked[len(ranked)//2], ranked[-1]):
                if item not in selected:
                    selected.append(item)
        pool.extend(selected)
    pool.sort(key=lambda x: (-x[0], x[2]['block_index'], x[1]))
    result = [p for _, _, p in pool[:max_candidates]]
    if document_notice_removed:
        # Standalone notice blocks can disappear entirely. Carry only a bool,
        # so validation can still see why the remaining source text changed.
        for block in result:
            block['publication_notice_removed'] = True
    return result


def prepare_selection(parts):
    """Per-document pure features; each budget still runs its own selection.

    Nothing persists across files or permission scopes. The tuple carries no
    greedy state, and does not truncate one budget's result for another budget.
    """
    prepared = []
    for block in candidates(parts):
        value = block['text']
        key = re.sub(r'\s+', ' ', value).casefold()
        structure = .7 if block['kind'] in {'title', 'heading', 'fields', 'abstract'} else 0
        penalty = 3 if is_boilerplate(value) or block['kind'] in {'footer', 'navigation'} else .6 if block['kind'] in {'header', 'quoted_history'} else .3 if block['kind'] == 'derived_summary' else 0
        if block.get('evidence_role') == 'reference':
            penalty += 2.5
        if block['kind'] not in _STRUCTURED_KINDS | {'title', 'heading'} and re.fullmatch(r'[A-Za-z]{1,24}', value.strip()):
            penalty += .8
        units = frozenset(_units(value))
        signal = min(12, len(units))/24 + min(80, len(value))/320
        score = quality(value) + signal + structure
        prepared.append((block, key, units, score, value.casefold(), penalty))
    return tuple(prepared)


def select_blocks(parts, limit=8, chars=4000, terms=(), *, prepared=None):
    chosen, seen, used = [], set(), 0
    anchors = {str(t).casefold() for t in terms if str(t)}
    # Keep the exact greedy score, but update maximum redundancy only against
    # the newly selected block. Rechecking every earlier selection on each
    # round repeats the same set operations O(candidates * limit**2) times.
    # This state lives only within this call: no stale cross-document cache.
    features = prepare_selection(parts) if prepared is None else prepared
    # Preserve the original floating-point addition order and exact ranking.
    pool = [[block, key, units, score + min(4, sum(t in folded for t in anchors))*.12 - penalty,
             0, False] for block, key, units, score, folded, penalty in features]
    while pool and len(chosen) < limit:
        best = None
        for index, (block, key, units, base_score, overlap, same_location) in enumerate(pool):
            if key in seen:
                continue
            new_location = 0 if same_location else .25
            score = base_score + new_location - overlap
            if best is None or score > best[0]:
                best = (score, index, key)
        if best is None:
            break
        _, index, key = best
        block, _, units, _, _, _ = pool.pop(index)
        room = chars - used
        if room < 40:
            break
        if len(block['text']) > room:
            # Do not fill the remaining room with an arbitrary partial sentence.
            if chosen:
                continue
            block = dict(block, text=block['text'][:room], truncated=True)
        chosen.append(block)
        seen.add(key)
        used += len(block['text']) + 1
        if len(chosen) < limit and chars - used >= 40:
            for candidate in pool:
                if candidate[1] in seen:
                    continue
                other_units = candidate[2]
                overlap = len(other_units & units) / max(1, len(other_units | units))
                candidate[4] = max(candidate[4], overlap)
                candidate[5] = candidate[5] or block.get('location') == candidate[0].get('location')
        else:
            break
    return chosen


def compact_text(parts, chars=1800, terms=(), *, prepared=None):
    # Ranking is needed only when source content exceeds the budget. Repeated
    # sentence selection otherwise drops subjects, quantities and qualifiers
    # from even a short note. Keep source order, never inventory-only names.
    source = [p for p in (parts or [])[:64] if isinstance(p, dict)
              and p.get('kind') not in {'attachment', 'member_names'}
              and str(p.get('text') or '').strip()]
    # Preserve the short-note contract only for unambiguous ordinary content.
    # Notice-bearing short pages used to bypass the evidence policy entirely.
    clean_short = all(clean_publication_spans(p['text']) == normalize(p['text'])
                      and p.get('evidence_role') != 'reference'
                      and p.get('kind') not in {'reference', 'references', 'bibliography'}
                      and not any(_REFERENCE_HEADING.fullmatch(line) for line in str(p['text']).splitlines())
                      and not any(reference for _, reference in _source_sections(p['text']))
                      for p in source)
    if clean_short and len(parts or []) <= 64 and sum(len(str(p['text'])) + 1 for p in source) <= chars + 1:
        return '\n'.join(normalize(p['text']) for p in source)
    output, used = [], 0
    for block in select_blocks(parts, chars=chars, terms=terms, prepared=prepared):
        value = ('[参考文献背景] ' if block.get('evidence_role') == 'reference' else '') + block['text']
        room = chars - used - bool(output)
        if len(value) > room:
            if output:
                continue
            value = value[:max(0, room)]
        if value:
            used += len(value) + bool(output)
            output.append(value)
    return '\n'.join(output)


def substantive_text(parts, chars=4800):
    """Bounded research body for topic support, excluding cited-work titles."""
    prepared = tuple(item for item in prepare_selection(parts)
                     if item[0].get('evidence_role') != 'reference'
                     and not is_boilerplate(item[0]['text']))
    return '\n'.join(p['text'] for p in select_blocks(parts, limit=12, chars=chars,
                                                     prepared=prepared))


def trim_reservoir(parts):
    """Cheap quality/position reservoir maintenance during streaming parsing."""
    if len(parts) <= 64:
        return
    # Rank the surviving body, so a notice on the same retained page cannot
    # evict its useful research from the streaming reservoir.
    clean = [clean_publication_spans(part['text']) for part in parts]
    ranked = sorted(range(len(parts)), key=lambda i: (
        -(quality(clean[i]) + (.7 if parts[i].get('kind') in {'title','heading','fields'} else 0)
          - (3 if not clean[i] or is_boilerplate(clean[i]) else 0)), i))
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
    if len(raw) <= 1800:
        # This is a complete retained window, not a claim that the file is
        # complete. Keep any truncation reported by the parser. Larger windows
        # retain the existing selector, reservoir and byte/time budgets.
        block = dict(text=raw, raw_text=raw, location=location, kind=kind, **metadata)
        block['id'] = block.get('id') or 'B' + hashlib.sha256(
            (str(location)+'\0'+raw).encode()).hexdigest()[:12]
        selected = [block]
    else:
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
    # Legacy sentence selection may have removed bibliography entry numbers
    # from portrait.evidence while the same source's sample still retains them.
    # Recover only exact source-span membership, never a topic-word guess.
    reference_lines = [line for line, reference in _source_sections(
        str(local.get('sample') or '')[:24000]) if reference]
    if reference_lines:
        reference_context = normalize('\n'.join(reference_lines))
        parts = [dict(part, evidence_role='reference')
                 if 20 <= len(str(part.get('text') or '')) <= 24000
                 and normalize(part.get('text')) in reference_context else part
                 for part in parts[:64]] + parts[64:]
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
    text = substantive_text(parts)
    fields = [normalize(x)[:100] for x in info.get('fields', [])[:20]
              if not is_publication_metadata(x, text)]
    headings = [normalize(x)[:100] for x in info.get('headings', [])[:12]
                if not is_publication_metadata(x, text, kind='heading')]
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
        text = ('[参考文献背景] ' if block.get('evidence_role') == 'reference' else '') + block['text']
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
