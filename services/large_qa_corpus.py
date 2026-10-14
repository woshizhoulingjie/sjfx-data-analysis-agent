"""Stream every current scoped file into bounded, traceable QA capsules.

The file count is never a retrieval limit. Capsules are compressed inputs for
query-specific synthesis, not a claim that the answer model read full files.
"""
from __future__ import annotations

from contextlib import contextmanager
import heapq
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from services.large_archive_members import IDENTITY_FIELDS, member_node
from services.large_qa_index import loads, tokens
from services.large_qa_search import _matches_current_source, _source_is_current, current_paths, scope_filter


_EXCEPTION = re.compile(
    r'但是|然而|例外|争议|限制|除非|不适用|禁止|风险|however|unless|except|'
    r'limitation|restricted|prohibited|risk|unsafe', re.I)
_SENTENCE = re.compile(r'[^。！？.!?\n]+(?:[。！？.!?\n]+|$)')


class ReadOnlySourceStore:
    """Read the current inventory without invoking store migrations."""

    def __init__(self, path):
        self.path = Path(path).resolve()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=20)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA query_only=ON')
            connection.execute('BEGIN')
            yield connection
        finally:
            connection.close()

    def get(self, package_id):
        with self._connect() as connection:
            row = connection.execute('SELECT * FROM packages WHERE id=?', (package_id,)).fetchone()
            return dict(row) if row else None


def _scope(value):
    # Delay this import: ConversationEngine imports the synthesis consumer.
    from services.conversation import ConversationScope
    return ConversationScope.from_dict(value) if isinstance(value, dict) or value is None else value


def scope_manifest(store, index, scope=None):
    """Distinguish inventoried scope files from a potentially incomplete index."""
    scope = _scope(scope)
    with index.connect() as connection:
        indexed = {row['path']: dict(row) for row in connection.execute(
            'SELECT path,topic,topic_name FROM documents WHERE sensitive=0')}
        scoped_index = scope_filter(connection, scope)
    if store is None:
        return {'paths': scoped_index, 'indexed_paths': scoped_index,
                'missing_index_paths': set(), 'scope_files': len(scoped_index),
                'membership_rechecked': False, 'source_digest': None, 'current_sources': {}}
    visible = set()
    sources = {}
    fingerprint = hashlib.sha256()
    with store._connect() as connection:
        columns = {row[1] for row in connection.execute('PRAGMA table_info(files)')}
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        fields = ['f.path'] + ['f.' + name for name in
            ('category', 'group_id', 'topic_name', 'size', 'modified_at_ns', 'sha256',
             'source_kind', 'source_metadata', 'local_summary', 'deep_summary') if name in columns]
        joins = ''
        if 'large_catalog_members' in tables:
            joins = (' LEFT JOIN large_catalog_members m ON m.package_id=f.package_id AND m.path=f.path')
            fields.append('m.group_id current_group_id')
            if 'lp_catalog_labels' in tables:
                joins += (' LEFT JOIN lp_catalog_labels l ON l.package_id=m.package_id AND l.group_id=m.group_id')
                fields.append('l.name current_topic_name')
        sql = 'SELECT ' + ','.join(fields) + ' FROM files f' + joins + ' WHERE f.package_id=? AND f.sensitive=0'
        if 'source_changed' in columns:
            sql += ' AND f.source_changed=0'
        sql += ' ORDER BY f.path'
        for result in connection.execute(sql, (index.package_id,)):
            row = dict(result)
            path = row['path']
            if not scope.contains_source(path):
                continue
            if scope.kind == 'file_type' and not scope.source_paths:
                if Path(path).suffix.lower().lstrip('.') != str(scope.value).lower().lstrip('.'):
                    continue
            if scope.kind in {'entity', 'time'} and not scope.source_paths:
                continue
            if scope.kind == 'topic' and not scope.source_paths:
                # Current catalog/source values win over the disposable index.
                # Older minimal stores have no current topic metadata, so their
                # only available resolved label remains the indexed value.
                keys = ('current_group_id', 'current_topic_name', 'group_id', 'topic_name', 'category')
                metadata = row if any(row.get(key) for key in keys) else indexed.get(path, row)
                label = ' '.join(str(metadata.get(key) or '') for key in
                                 ('topic', 'current_group_id', 'current_topic_name', 'topic_name', 'group_id', 'category'))
                if str(scope.value).casefold() not in label.casefold():
                    continue
            visible.add(path)
            sources[path] = {key: row[key] for key in
                             ('path', 'size', 'modified_at_ns', 'source_kind', 'source_metadata') if key in row}
            fingerprint.update(json.dumps(row, sort_keys=True, ensure_ascii=False,
                                          separators=(',', ':'), default=str).encode('utf-8'))
            fingerprint.update(b'\n')
    # Reuse the retrieval membership checker, including newer source_changed
    # columns. It works with older minimal inventory schemas as well.
    scoped_index = current_paths(store, index.package_id, set(indexed)) & visible
    return {'paths': visible, 'indexed_paths': scoped_index,
            'missing_index_paths': visible - scoped_index,
            'scope_files': len(visible), 'membership_rechecked': True,
            'source_digest': fingerprint.hexdigest(), 'current_sources': sources}


def _row_current(root, row, containers, current_sources=None):
    """Verify a row once, sharing physical archive-container identity checks."""
    try:
        member = member_node(row)
        key = tuple(member[field] for field in IDENTITY_FIELDS[:5]) if member else None
        if key is not None:
            if key not in containers:
                containers[key] = _source_is_current(root, row)
            fresh = containers[key]
        else:
            fresh = _source_is_current(root, row)
        source = (current_sources or {}).get(row['path'])
        if source and {'size', 'modified_at_ns'}.issubset(source):
            if member is not None and 'source_kind' not in source:
                # Old minimal inventory schemas cannot supply member metadata;
                # retain their available size/time check plus verified container.
                matching = (source['size'], source['modified_at_ns']) == (row['size'], row['mtime'])
            else:
                matching = _matches_current_source(row, source)
            fresh = fresh and matching
        return fresh
    except (ValueError, TypeError):
        return False


def fresh_paths(connection, root, paths, *, current_sources=None):
    """Return fresh scoped paths without text loads or per-member stat repeats.

    A JSON membership subquery uses one binding for arbitrary scope sizes; no
    source names are interpolated into SQL and there is no 500/999-file cap.
    """
    paths = set(paths)
    if not paths:
        return set()
    columns = {row[1] for row in connection.execute('PRAGMA table_info(documents)')}
    origin = ','.join(name if name in columns else "'" + default + "' AS " + name
                      for name, default in (('source_kind', ''), ('source_metadata', '{}')))
    sql = ('SELECT path,size,mtime,sensitive,' + origin +
           ' FROM documents WHERE sensitive=0 AND path IN (SELECT value FROM json_each(?)) ORDER BY path')
    output, containers = set(), {}
    for result in connection.execute(sql, (json.dumps(sorted(paths)),)):
        row = dict(result)
        if _row_current(root, row, containers, current_sources):
            output.add(row['path'])
    return output


def _excerpt(text, query_terms, limit):
    """Return a literal substring, selecting late matches and exceptions too."""
    best = None
    for match in _SENTENCE.finditer(text):
        body = match.group()
        if not body.strip():
            continue
        overlap = len(query_terms & set(tokens(body))) / max(1, len(query_terms))
        rank = overlap + .28 * bool(_EXCEPTION.search(body))
        if best is None or rank > best[0]:
            best = (rank, match.start(), match.end())
    if best is None:
        return '', 0, 0
    _, start, end = best
    if end - start > limit:
        paragraph = text[start:end]
        anchors = [paragraph.lower().find(term) for term in query_terms if term in paragraph.lower()]
        exception = _EXCEPTION.search(paragraph)
        if exception:
            anchors.append(exception.start())
        anchor = min(anchors) if anchors else 0
        start += max(0, min(anchor - limit // 3, len(paragraph) - limit))
        end = min(end, start + limit)
    elif len(text) > end - start:
        # Keep neighboring sentences instead of reducing a short file to its
        # first sentence when the question is a broad overview.
        start = max(0, min(start - limit // 5, len(text) - limit))
        end = min(len(text), start + limit)
    # Preserve exact offsets after trimming presentation whitespace.
    body = text[start:end]
    left = len(body) - len(body.lstrip())
    right = len(body.rstrip())
    return body[left:right], start + left, start + right


def _excerpts(text, query_terms, limit):
    """Include the best late match and all other bounded text windows."""
    seen = set()
    best = _excerpt(text, query_terms, limit)
    if best[0]:
        seen.add(best[0])
        yield best
    for position in range(0, len(text), limit):
        # These windows already fit the point budget. Do not tokenize them a
        # second time merely to choose a sentence; offer() scores each exact
        # window once, and the best contextual excerpt above handles boundaries.
        window = text[position:position + limit]
        start = len(window) - len(window.lstrip())
        end = len(window.rstrip())
        body = window[start:end]
        if body and body not in seen:
            seen.add(body)
            yield body, position + start, position + end


def _select(candidates, query_terms, limit, max_chars):
    chosen, used, remaining = [], set(), list(candidates)
    while remaining and len(chosen) < limit:
        def score(item):
            redundancy = max((len(item['_terms'] & old['_terms']) /
                              max(1, len(item['_terms'] | old['_terms'])) for old in chosen), default=0)
            return (item['_score'] - .7 * redundancy +
                    .15 * (item['kind'] not in {old['kind'] for old in chosen}) +
                    .8 * (item['_exception'] and not any(old['_exception'] for old in chosen)))
        selected = max(remaining, key=score)
        remaining.remove(selected)
        if selected['text'] in used:
            continue
        room = max_chars - sum(len(item['text']) for item in chosen)
        if room <= 0:
            break
        if len(selected['text']) > room:
            selected = {**selected, 'text': selected['text'][:room],
                        'excerpt_end': selected['excerpt_start'] + room}
        used.add(selected['text'])
        chosen.append(selected)
    return [{key: value for key, value in item.items() if not key.startswith('_')} for item in chosen]


def scan_corpus(index, query, *, scope=None, store=None, root=None,
                allowed_paths=None, point_limit=4, max_chars=1400, progress=None, checkpoint=None):
    """Scan all summary/original windows once, retaining a capsule per file.

    All source membership and physical freshness checks happen before text is
    selected. Archive members share one verified container stat per pass. SQL
    streams ordered indexed rows; only a small candidate heap for the current
    file and the bounded output capsules are held in memory.
    """
    scope = _scope(scope)
    manifest = scope_manifest(store, index, scope)
    allowed = set(manifest['indexed_paths'])
    expected = set(manifest['paths'])
    if allowed_paths is not None:
        allowed &= set(allowed_paths)
        expected &= set(allowed_paths)
    point_limit = max(1, min(4, int(point_limit)))
    max_chars = max(1, int(max_chars))
    point_chars = max(1, max_chars // point_limit)
    query_terms = set(tokens(query))
    capsules = []
    stats = {'scope_files': len(expected), 'indexed_scope_files': len(allowed),
             'scanned_files': 0, 'summary_files': 0, 'original_files': 0,
             'chunks': 0, 'summary_chunks': 0, 'original_chunks': 0,
             'files_without_content': 0, 'stale_files': 0,
             'missing_index_files': len(expected - allowed),
             'membership_rechecked': manifest['membership_rechecked']}
    containers = {}
    with index.connect() as connection:
        metadata = index.meta(connection)
        root = root or (metadata.get('package') or {}).get('root_path')
        if not root:
            raise ValueError('全包综合缺少来源根目录')
        columns = {row[1] for row in connection.execute('PRAGMA table_info(documents)')}
        source_columns = ','.join('d.' + name if name in columns else "'" + default + "' AS " + name
                                  for name, default in (('source_kind', ''), ('source_metadata', '{}')))
        sql = '''SELECT d.path,d.name,d.topic,d.topic_name,d.size,d.mtime,d.sensitive,
                 d.parse_status,d.parse_complete,d.summary!='' summary_available,
                 d.version document_version,''' + source_columns + ''',
                 c.id chunk_id,c.kind,c.text,c.location,c.version chunk_version,c.ordinal
                 FROM documents d LEFT JOIN chunks c ON c.path=d.path
                 WHERE d.sensitive=0 AND d.path IN (SELECT value FROM json_each(?))
                 ORDER BY d.path,c.kind,c.ordinal,c.id'''
        current = None
        heap = []
        sequence = 0
        special = {}

        def offer(text, kind, chunk_id, version, location):
            nonlocal sequence
            for body, start, end in _excerpts(text, query_terms, point_chars):
                current['original_available'] |= kind == 'original'
                terms = set(tokens(body))
                exception = bool(_EXCEPTION.search(body))
                relevance = len(terms & query_terms) / max(1, len(query_terms))
                score = relevance + .28 * exception + .10 * (kind == 'original')
                point = {'chunk_id': chunk_id, 'kind': kind, 'text': body,
                         'location': dict(location), 'version': version,
                         'excerpt_start': start, 'excerpt_end': end,
                         '_terms': terms, '_score': score, '_exception': exception}
                sequence += 1
                entry = (score, sequence, point)
                if len(heap) < 24:
                    heapq.heappush(heap, entry)
                elif entry[:2] > heap[0][:2]:
                    heapq.heapreplace(heap, entry)
                for tag in ([kind] + (['exception'] if exception else [])):
                    if tag not in special or score > special[tag][0]:
                        special[tag] = entry

        def finish():
            if current is None:
                return
            if not current['_fresh']:
                capsules.append({key: value for key, value in current.items() if not key.startswith('_')})
                stats['files_without_content'] += 1
                return
            if current['summary_available'] and not current['_summary_scanned']:
                # Historical/corrupt indexes can retain the document summary
                # without publishing summary windows. Consume that summary as
                # an explicit non-chunk fallback; never invent a chunk citation.
                summary = connection.execute('SELECT summary FROM documents WHERE path=?',
                                             (current['path'],)).fetchone()[0]
                offer(str(summary or ''), 'summary', None, current['_document_version'],
                      {'source': 'file_summary', 'representation': 'stored_summary_fallback'})
                stats['summary_fallback_files'] = stats.get('summary_fallback_files', 0) + 1
            candidates = {entry[1]: entry[2] for entry in heap}
            candidates.update({entry[1]: entry[2] for entry in special.values()})
            current['points'] = _select(candidates.values(), query_terms, point_limit, max_chars)
            stats['summary_files'] += bool(current['summary_available'])
            stats['original_files'] += bool(current['original_available'])
            stats['files_without_content'] += not bool(current['points'])
            stats['scanned_files'] += 1
            capsules.append({key: value for key, value in current.items() if not key.startswith('_')})
            if progress:
                progress(phase='scan', files_done=len(capsules), files_total=len(expected))

        for result in connection.execute(sql, (json.dumps(sorted(allowed)),)):
            if checkpoint and stats['chunks'] % 200 == 0:
                checkpoint()
            row = dict(result)
            if row['path'] not in allowed:
                continue
            if current is None or current['path'] != row['path']:
                finish()
                heap, special = [], {}
                fresh = _row_current(root, row, containers, manifest['current_sources'])
                current = {key: row[key] for key in ('path', 'name', 'topic', 'topic_name',
                           'parse_status', 'parse_complete', 'summary_available',
                           'source_kind', 'source_metadata')}
                current.update(original_available=False, points=[], _fresh=fresh,
                               _summary_scanned=False, _document_version=row['document_version'])
                if not fresh:
                    current.update(parse_status='stale', summary_available=False)
                    stats['stale_files'] += 1
            if not current['_fresh'] or row['chunk_id'] is None:
                continue
            kind = row['kind']
            if kind not in {'summary', 'original'}:
                continue
            stats['chunks'] += 1
            stats[kind + '_chunks'] += 1
            current['_summary_scanned'] |= kind == 'summary'
            text = str(row['text'] or '')
            # Every indexed window is read and scored, including a file's tail.
            offer(text, kind, row['chunk_id'], row['chunk_version'],
                  dict(loads(row['location'], {}) or {}))
        finish()
    stats['content_files'] = sum(bool(item['points']) for item in capsules)
    stats['complete'] = bool(stats['membership_rechecked'] and not stats['stale_files']
                             and not stats['missing_index_files']
                             and not stats['files_without_content']
                             and stats['scanned_files'] == stats['scope_files'])
    stats['generation'] = metadata.get('generation')
    stats['summary_generation'] = metadata.get('summary_generation')
    return {'capsules': capsules, 'stats': stats, 'scope': scope.as_dict(),
            'complete': stats['complete'],
            'source_snapshot': {'source_digest': manifest.get('source_digest'),
                                'index_epoch': metadata.get('index_epoch'),
                                'generation': metadata.get('generation'),
                                'summary_generation': metadata.get('summary_generation')},
            'basis': 'all_current_indexed_files'}
