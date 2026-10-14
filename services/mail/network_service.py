"""Bounded communication views. Full message membership stays on the server.

One read-only SQLite connection watches data_version for each database. The
bounded LRU is per process; every request reauthorizes and checks that same
watcher, including commits made by other workers. Any write conservatively
invalidates a view and its signed cursors. No derived graph is persisted.
"""
from __future__ import annotations

import base64
from collections import OrderedDict, defaultdict
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from urllib.parse import quote

from config import Config
from .people import authorize, filtered_messages, _role_cte, _date_bound

VERSION = 'communication-progressive-v2.1'
MAX_NODES = 60
MAX_EDGES = 180
_SECRET = os.urandom(32)
_LOCK = threading.RLock()
_WATCHERS = OrderedDict()
_CACHE = OrderedDict()


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def close_database(db_path):
    """Release one stopped instance's watcher and views before removing its DB.

    A SQLite connection context does not close this deliberately persistent
    data_version watcher. Instance owners must call this at shutdown; keeping
    it alive until LRU eviction otherwise pins the database on Windows.
    """
    path = str(Path(db_path).resolve())
    with _LOCK:
        watcher = _WATCHERS.pop(path, None)
        try:
            if watcher is not None:
                watcher[0].close()
        finally:
            for key in tuple(_CACHE):
                if key[0] == path:
                    del _CACHE[key]


def _watch(repository):
    path = str(Path(repository.storage.db_path).resolve())
    watcher = _WATCHERS.get(path)
    if watcher is None:
        connection = sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True, check_same_thread=False)
        try:
            connection.execute('PRAGMA query_only=ON')
        except BaseException:
            connection.close()
            raise
        watcher = (connection, uuid.uuid4().hex)
        _WATCHERS[path] = watcher
        while len(_WATCHERS) > 4:
            old, (connection, _) = _WATCHERS.popitem(last=False)
            connection.close()
            for key in list(_CACHE):
                if key[0] == old:
                    del _CACHE[key]
    _WATCHERS.move_to_end(path)
    return path, watcher[1], watcher[0].execute('PRAGMA data_version').fetchone()[0]


def _articulation(adjacency):
    """Iterative Tarjan on the full scoped undirected projection, O(V+E)."""
    discovered, low, parent, children, points = {}, {}, {}, defaultdict(int), set()
    clock = 0
    for root in adjacency:
        if root in discovered:
            continue
        clock += 1
        discovered[root] = low[root] = clock
        parent[root] = None
        stack = [(root, iter(sorted(adjacency[root])))]
        while stack:
            u, neighbors = stack[-1]
            v = next(neighbors, None)
            if v is None:
                stack.pop()
                p = parent[u]
                if p is None:
                    if children[u] > 1:
                        points.add(u)
                else:
                    low[p] = min(low[p], low[u])
                    if parent[p] is not None and low[u] >= discovered[p]:
                        points.add(p)
                continue
            if v == parent[u]:
                continue
            if v in discovered:
                low[u] = min(low[u], discovered[v])
            else:
                parent[v] = u
                children[u] += 1
                clock += 1
                discovered[v] = low[v] = clock
                stack.append((v, iter(sorted(adjacency[v]))))
    return points


def _build_index(repository, package_id, owner_id, view, scope, args, filters, revision):
    with repository._connection() as c:
        mapping = {r['normalized_address']: dict(r) for r in c.execute(
            "SELECT a.normalized_address,e.entity_id,e.display_name FROM mail_entities e "
            "JOIN mail_entity_addresses x ON x.entity_id=e.entity_id AND x.package_id=e.package_id "
            "JOIN mail_addresses a ON a.address_id=x.address_id WHERE e.package_id=? AND e.basis='manual'", (package_id,))}
        messages = {r['message_key']: dict(r) for r in c.execute(
            'SELECT m.message_key,m.subject_raw,m.sent_at_utc,m.raw_sha256 FROM mail_messages m WHERE m.message_key IN (' + scope + ')', args)}
        rows = c.execute(_role_cte(scope) + ' SELECT * FROM role_rows ORDER BY message_key,address,role', args).fetchall()
    nodes, flags, parts = {}, {}, defaultdict(list)
    for row in rows:
        address, role, key = row['address'], row['role'], row['message_key']
        entity = mapping.get(address)
        identity = entity['entity_id'] if view == 'person' and entity else address
        if identity not in nodes:
            nodes[identity] = {'address': identity, 'display_name': entity['display_name'] if entity else address,
                'entity_id': entity['entity_id'] if entity else None, 'addresses': set(), 'sent_relations': 0, 'received_relations': 0}
            flags[identity] = {k: set() for k in ('total', 'sent', 'received', 'direct', 'cc', 'bcc')}
        nodes[identity]['addresses'].add(address)
        flags[identity]['total'].add(key)
        if role == 'from':
            flags[identity]['sent'].add(key)
        else:
            flags[identity]['received'].add(key)
            flags[identity]['direct' if role == 'to' else role].add(key)
        parts[key].append((identity, address, role))
    pairs, adjacency, outgoing, incoming = {}, defaultdict(set), defaultdict(set), defaultdict(set)
    for key, participants in parts.items():
        senders = {(n, a) for n, a, r in participants if r == 'from'}
        recipients = {(n, a, r) for n, a, r in participants if r != 'from'}
        for source, from_address in senders:
            for target, to_address, role in recipients:
                pair = pairs.setdefault((source, target), {'role_keys': defaultdict(set), 'relations': 0})
                pair['role_keys'][role].add(key)
                pair['relations'] += 1
                nodes[source]['sent_relations'] += 1
                nodes[target]['received_relations'] += 1
                outgoing[source].add(target)
                incoming[target].add(source)
                if source != target:
                    adjacency[source].add(target)
                    adjacency[target].add(source)
    points = _articulation(adjacency)
    for identity, node in nodes.items():
        node.update({role: len(keys) for role, keys in flags[identity].items()})
        node.update(addresses=sorted(node['addresses']), degree=len(adjacency[identity]),
            centrality=len(adjacency[identity]) / max(1, len(nodes) - 1), bridge=identity in points,
            articulation_point=identity in points, one_way=not bool(outgoing[identity] & incoming[identity]))
    ordered = sorted(nodes, key=lambda n: (-nodes[n]['degree'], -nodes[n]['total'], n))
    rank = {node: i for i, node in enumerate(ordered)}
    incidence = defaultdict(list)
    for (source, target), pair in pairs.items():
        keys = set().union(*pair['role_keys'].values())
        pair['keys'] = sorted((k for k in keys if messages[k]['sent_at_utc']), key=lambda k: (messages[k]['sent_at_utc'], k), reverse=True)
        pair['keys'].extend(sorted(k for k in keys if not messages[k]['sent_at_utc']))
        times = [messages[k]['sent_at_utc'] for k in keys if messages[k]['sent_at_utc']]
        pair['edge'] = {'from_address': source, 'to_address': target, 'roles': sorted(pair['role_keys']),
            'recipient_role': 'to' if 'to' in pair['role_keys'] else sorted(pair['role_keys'])[0],
            'role_counts': {role: len(ks) for role, ks in pair['role_keys'].items()},
            'message_count': len(keys), 'recipient_relation_count': pair['relations'],
            'first_sent': min(times) if times else None, 'last_sent': max(times) if times else None,
            'bidirectional': (target, source) in pairs}
        incidence[source].append((source, target))
        if source != target:
            incidence[target].append((source, target))
        pair['role_keys'] = {role: [k for k in pair['keys'] if k in ks] for role, ks in pair['role_keys'].items()}
    manifest = hashlib.sha256(_json([VERSION, package_id, owner_id, view, filters, revision,
        sorted((k, m['raw_sha256'], m['sent_at_utc']) for k, m in messages.items()), mapping]).encode()).hexdigest()
    return {'nodes': nodes, 'ordered': ordered, 'pairs': pairs, 'messages': messages, 'filters': filters,
        'adjacency': {k: sorted(v, key=lambda n: rank[n]) for k, v in adjacency.items()}, 'incidence': incidence,
        'manifest': manifest, 'view': view, 'package_id': package_id, 'owner_id': owner_id,
        'search': {k: '\n'.join([n['display_name'], k, *n['addresses']]).casefold() for k, n in nodes.items()}}


def _index(repository, package_id, owner_id, *, view='mailbox', issue_id=None, as_of=None, manifest=None, **filters):
    authorize(repository, package_id, owner_id)
    if not Config.MAIL_ENABLE_GRAPH_METRICS:
        raise RuntimeError('通信图功能未启用')
    if view not in {'mailbox', 'person'}:
        raise ValueError('人物视角仅支持 mailbox 或 person')
    as_of = _date_bound(as_of, upper=True)
    scope, args, applied = filtered_messages(package_id, **filters)
    if as_of:
        scope = 'SELECT m.message_key FROM mail_messages m WHERE m.message_key IN (' + scope + ') AND julianday(m.sent_at_utc)<=julianday(?)'
        args = [*args, as_of]
    applied.update(issue_id=str(issue_id or '') or None, as_of=as_of)
    with _LOCK:
        revision = _watch(repository)
        cache_key = (revision[0], str(owner_id), package_id, view, _json(applied))
        cached = _CACHE.get(cache_key)
        from sjfx_protection import business, cache
        proof_key = cache.resource_key(repository.storage.db_path, 'mail-network', package_id, view, _json(applied))
        if cached and cached[0] == revision and business.enabled():
            from sjfx_protection.store import ProtectionError
            try:
                cache.require_payload(proof_key, cached[1], owner=owner_id)
            except ProtectionError as exc:
                if exc.code not in {'cache_provenance_missing', 'context_stale'}:
                    raise
                _CACHE.pop(cache_key, None)
                cached = None
        if cached and cached[0] == revision:
            index = cached[1]
            _CACHE.move_to_end(cache_key)
        else:
            if issue_id:
                from .issue_repository import issue_message_scope
                selected = issue_message_scope(repository, package_id, owner_id, str(issue_id), as_of=as_of,
                    scope={k: v for k, v in applied.items() if k in {'date_from', 'date_to'}})
                # Ledger keys are server-authorized source ids. Quote literals
                # instead of requiring JSON1 or SQLite's variable-count limit.
                keys = ','.join("'" + str(k).replace("'", "''") + "'" for k in selected['message_keys']) or 'NULL'
                scope = 'SELECT message_key FROM (' + scope + ') WHERE message_key IN (' + keys + ')'
                applied['issue_fingerprint'] = selected['fingerprint']
            index = _build_index(repository, package_id, owner_id, view, scope, args, applied, revision)
            if _watch(repository) != revision:
                raise RuntimeError('资料正在变化，请重新加载图谱')
            cache.record_payload(proof_key, index, owner=owner_id)
            _CACHE[cache_key] = (revision, index)
            _CACHE.move_to_end(cache_key)
            while len(_CACHE) > 4:
                _CACHE.popitem(last=False)
        if manifest and manifest != index['manifest']:
            raise RuntimeError('资料或人物归并已变化，请重新加载图谱')
        return index


def _cursor(index, kind, selector, offset):
    if offset is None:
        return None
    body = _json({'manifest': index['manifest'], 'kind': kind, 'selector': selector, 'offset': offset, 'created': int(time.time())}).encode()
    return base64.urlsafe_b64encode(body + b'.' + hmac.new(_SECRET, body, hashlib.sha256).hexdigest().encode()).decode()


def _offset(index, token, kind, selector):
    if not token:
        return 0
    try:
        if not isinstance(token, str) or len(token) > 8192:
            raise ValueError()
        raw = base64.b64decode(token, altchars=b'-_', validate=True)
        body, signature = raw.rsplit(b'.', 1)
        if not hmac.compare_digest(signature, hmac.new(_SECRET, body, hashlib.sha256).hexdigest().encode()):
            raise ValueError()
        value = json.loads(body)
        if value['manifest'] != index['manifest']:
            raise RuntimeError('资料或筛选范围已变化，请重新加载图谱')
        if value['kind'] != kind or value['selector'] != selector:
            raise ValueError()
        offset = value['offset']
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError()
        if not 0 <= time.time() - value['created'] <= 1800:
            raise RuntimeError('图谱分页已过期，请重新加载图谱')
        return offset
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('图谱分页游标无效或不属于当前查询') from exc


def _limit(value, default=30, maximum=MAX_NODES):
    try:
        return max(1, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def _view(index, ids, *, anchor=None):
    chosen = set(ids)
    candidates = set()
    for node in ids:
        candidates.update(index['incidence'].get(node, ()))
    pairs = [index['pairs'][p]['edge'] for p in candidates if p[0] in chosen and p[1] in chosen]
    pairs.sort(key=lambda e: (not bool(anchor and anchor in (e['from_address'], e['to_address'])), -e['message_count'], e['from_address'], e['to_address']))
    return {'nodes': [{**index['nodes'][n], 'addresses': list(index['nodes'][n]['addresses'])} for n in ids],
        'edges': [{**e, 'roles': list(e['roles']), 'role_counts': dict(e['role_counts'])} for e in pairs[:MAX_EDGES]], 'progressive': True,
        'node_total': len(index['nodes']), 'edge_total': len(index['pairs']), 'view_edge_total': len(pairs),
        'unexpanded_nodes': max(0, len(index['nodes']) - len(ids)), 'message_total': len(index['messages']),
        'manifest': index['manifest'], 'view': index['view'], 'filters': index['filters'], 'package_id': index['package_id'],
        'api_base': '/api/mail/packages/' + quote(index['package_id'], safe='') + '/relations/network',
        'count_basis': 'logical', 'node_count_basis': 'distinct_message_key',
        'edge_count_basis': 'distinct_message_key_per_directed_pair_union_of_roles',
        'metrics': {'algorithm': VERSION, 'scope': 'full_filtered_scope', 'centrality': 'normalized_undirected_degree',
            'bridge': 'undirected_articulation_point', 'interpretation': '收发结构指标不表示现实组织、权力、阅读或信息传播'},
        'bridge_calculated': True, 'enabled': True}


def overview(repository, package_id, owner_id, *, limit=30, **filters):
    authorize(repository, package_id, owner_id)
    if not Config.MAIL_ENABLE_GRAPH_METRICS:
        return {'nodes': [], 'edges': [], 'enabled': False, 'progressive': True, 'node_total': 0, 'edge_total': 0}
    index = _index(repository, package_id, owner_id, **filters)
    return _view(index, index['ordered'][:_limit(limit)])


def search(repository, package_id, owner_id, *, node_query='', cursor=None, limit=30, **filters):
    index = _index(repository, package_id, owner_id, **filters)
    query = str(node_query or '').strip().casefold()[:320]
    selector = [query, _limit(limit)]
    offset = _offset(index, cursor, 'search', selector)
    ids = [n for n in index['ordered'] if query in index['search'][n]]
    page = ids[offset:offset + selector[1]]
    return {**_view(index, page), 'total': len(ids), 'offset': offset,
        'next_cursor': _cursor(index, 'search', selector, offset + len(page)) if offset + len(page) < len(ids) else None}


def neighbors(repository, package_id, owner_id, *, node, cursor=None, limit=30, **filters):
    index = _index(repository, package_id, owner_id, **filters)
    if node not in index['nodes']:
        raise LookupError('人物不在当前图谱范围或归并已变化')
    selector = [node, _limit(limit, maximum=MAX_NODES - 1)]
    offset = _offset(index, cursor, 'neighbors', selector)
    ids = index['adjacency'].get(node, [])
    page = ids[offset:offset + selector[1]]
    return {**_view(index, [node, *page], anchor=node), 'anchor': node, 'neighbor_total': len(ids), 'offset': offset,
        'next_cursor': _cursor(index, 'neighbors', selector, offset + len(page)) if offset + len(page) < len(ids) else None,
        'unexpanded_neighbors': max(0, len(ids) - offset - len(page))}


def edge_messages(repository, package_id, owner_id, *, from_node, to_node, recipient_role=None, cursor=None, limit=25, **filters):
    index = _index(repository, package_id, owner_id, **filters)
    if recipient_role not in (None, '', 'to', 'cc', 'bcc'):
        raise ValueError('收件角色仅支持 to、cc、bcc')
    pair = index['pairs'].get((from_node, to_node))
    if not pair:
        raise LookupError('方向关系不在当前图谱范围')
    selector = [from_node, to_node, recipient_role or '', _limit(limit, default=25, maximum=50)]
    offset = _offset(index, cursor, 'edge_messages', selector)
    keys = pair['keys']
    if recipient_role:
        keys = pair['role_keys'].get(recipient_role, [])
    page = keys[offset:offset + selector[3]]
    return {'items': [{k: v for k, v in index['messages'][key].items() if k != 'raw_sha256'} for key in page],
        'total': len(keys), 'offset': offset, 'next_cursor': _cursor(index, 'edge_messages', selector, offset + len(page)) if offset + len(page) < len(keys) else None,
        'manifest': index['manifest'], 'filters': index['filters'], 'package_id': package_id,
        'from_node': from_node, 'to_node': to_node, 'recipient_role': recipient_role or None, 'count_basis': 'distinct_message_key'}


def _issue_scope(index, nodes):
    if (not isinstance(nodes, list) or not 1 <= len(nodes) <= 180
            or any(not isinstance(n, str) or n not in index['nodes'] for n in nodes)):
        raise ValueError('事项视图需要当前范围内的 1–180 个节点')
    selected = sorted(set(nodes))
    scope = {'message_keys': list(index['messages']), 'actors': sorted({a for n in selected for a in index['nodes'][n]['addresses']}),
        'date_from': index['filters']['date_from'], 'date_to': index['filters']['date_to']}
    return selected, scope


def issue_page(repository, package_id, owner_id, *, nodes=None, cursor=None, limit=15, **filters):
    from .issue_repository import IssueRepository
    index = _index(repository, package_id, owner_id, **filters)
    selected, scope = _issue_scope(index, nodes)
    selector = [selected, _limit(limit, default=15, maximum=30)]
    offset = _offset(index, cursor, 'issues', selector)
    result = IssueRepository(repository).list_issues(package_id, owner_id, scope=scope, as_of=index['filters']['as_of'], offset=offset, limit=selector[1])
    return {**result, 'manifest': index['manifest'], 'filters': index['filters'],
        'next_cursor': _cursor(index, 'issues', selector, result.get('next_offset')),
        'interpretation': '事项来自共享表达台账；暂定归并、资料不足和审核状态保留，不把收发边解释为语义支持'}


def issue_timeline(repository, package_id, owner_id, *, timeline_issue_id, nodes=None, cursor=None, limit=25, **filters):
    """Keep timeline sources within the exact graph scope and selected actors.

    Only source ids on this page reach the client; membership and actor expansion
    remain on the server. Cursor selectors cannot be reused for another issue,
    actor selection or communication view.
    """
    from .issue_repository import IssueRepository
    index = _index(repository, package_id, owner_id, **filters)
    selected, scope = _issue_scope(index, nodes)
    selector = [str(timeline_issue_id), selected, _limit(limit, default=25, maximum=50)]
    offset = _offset(index, cursor, 'issue_timeline', selector)
    result = IssueRepository(repository).issue_timeline(package_id, owner_id, selector[0],
        scope=scope, as_of=index['filters']['as_of'], offset=offset, limit=selector[2])
    if not result['total']:
        raise LookupError('事项表达不在当前图谱与人物范围内')
    return {**result, 'manifest': index['manifest'], 'filters': index['filters'],
        'next_cursor': _cursor(index, 'issue_timeline', selector, result.get('next_offset')),
        'nodes': selected, 'package_id': package_id}
