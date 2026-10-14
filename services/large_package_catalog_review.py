"""Read-only review facts; workflow flags are not accuracy measurements."""
import hashlib
import json


def _object(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or '{}')
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def leaf_review_facts(result, needs_review, evidence=None, model=None, content_revision=None):
    result = _object(result)
    metadata = _object(result.get('naming_review'))
    reasons = metadata.get('reasons') or []
    if not isinstance(reasons, list):
        reasons = []
    reasons = sorted({str(reason) for reason in reasons if isinstance(reason, str)})
    state = str(metadata.get('state') or 'unrecorded')
    if (model == 'model-subtopic' and state != 'checked') or (
            state == 'checked' and content_revision is not None
            and metadata.get('validated_revision') is not None
            and metadata['validated_revision'] != content_revision):
        state = 'pending'
    if state == 'pending':
        needs_review = True
        if not reasons:
            reasons = ['post_split_unverified' if result.get('from_split') or model == 'model-subtopic'
                       else 'model_review']
    if needs_review and not reasons:
        # Older split rows only record the provisional name, not independent
        # child validation. This is a provenance fact, not a bad-name verdict.
        if state == 'unrecorded' and result.get('from_split') and result.get('purity') == 'subtopic':
            reasons = ['post_split_unverified']
            state = 'pending'
        elif result.get('purity') == 'unsure':
            reasons = ['model_uncertain']
        elif result.get('purity') == 'mixed':
            reasons = ['cross_topic']
        else:
            reasons = ['model_review']
    return {'needs_review': bool(needs_review),
            'purity': str(result.get('purity') or 'unsure'),
            'review_reasons': reasons, 'validation_state': state,
            'validated_revision': metadata.get('validated_revision')}


def provisional_label(row):
    """A saved split suggestion is not a completed name; explicit manual names are."""
    row = dict(row)
    if row.get('locked') and row.get('model') == 'manual':
        return False
    return leaf_review_facts(row.get('result'), row.get('needs_review'),
                             model=row.get('model'), content_revision=row.get('content_revision'))[
                                 'validation_state'] == 'pending'


def provisional_label_sql(group='cg', label='cl'):
    """SQL counterpart for file-state projections, including legacy split provenance."""
    body = f"CASE WHEN json_valid({group}.result) THEN {group}.result ELSE '{{}}' END"
    state = f"COALESCE(json_extract({body},'$.naming_review.state'),'unrecorded')"
    validated = f"json_extract({body},'$.naming_review.validated_revision')"
    return (f"NOT (COALESCE({label}.locked,0)=1 AND COALESCE({label}.model,'')='manual') AND ("
            f"{state}='pending' OR ({label}.model='model-subtopic' AND {state}<>'checked') OR "
            f"({state}='unrecorded' AND {label}.needs_review=1 AND "
            f"json_extract({body},'$.from_split') IS NOT NULL AND "
            f"json_extract({body},'$.purity')='subtopic') OR "
            f"({state}='checked' AND {validated} IS NOT NULL AND {validated}<>{group}.content_revision))")


def summarize_reviews(rows, members, branches):
    """Use live identities and union scopes so ancestors cannot inflate counts."""
    leaves = []
    for raw in rows:
        row = dict(raw)
        gid = str(row['id'])
        if gid.startswith(('km-other-', 'km-pending-')) or not members.get(gid):
            continue
        facts = leaf_review_facts(row.get('result'), row.get('needs_review'),
                                 model=row.get('model'), content_revision=row.get('content_revision'))
        leaves.append(dict(facts, node_id=gid, name=row.get('name') or '',
                           files=len(members[gid]), revision=row.get('content_revision'),
                           label_revision=row.get('revision'), locked=bool(row.get('locked'))))
    themes = []
    for branch in branches:
        if branch.get('kind', 'theme') != 'theme' or branch.get('node_id') == 'branch:unassigned':
            continue
        paths = set().union(*(members.get(str(gid), set()) for gid in branch.get('children', [])))
        if not paths:
            continue
        secondary = _object(branch.get('secondary_validation'))
        needs = bool(branch.get('needs_review') or branch.get('status') == 'needs_review')
        reasons = []
        if needs:
            if branch.get('source_leaf_review_ids'):
                reasons.append('source_leaf_review')
            if secondary.get('needs_review'):
                reasons.append('secondary_evidence_review')
            if not reasons:
                reasons.append('theme_scope_review')
        themes.append({'node_id': branch['node_id'], 'name': branch.get('name') or '',
                       'needs_review': needs, 'purity': str(branch.get('purity') or 'unsure'),
                       'review_reasons': reasons, 'files': len(paths),
                       'root': not branch.get('parent_id'),
                       'source_leaf_review_ids': sorted(branch.get('source_leaf_review_ids') or []),
                       'review_child_node_ids': sorted(branch.get('review_child_node_ids') or []),
                       'secondary_validation': {key: secondary.get(key) for key in
                                                ('needs_review', 'purity', 'naming_status', 'support')},
                       'children': sorted(branch.get('children') or [])})
    leaves.sort(key=lambda row: row['node_id'])
    themes.sort(key=lambda row: row['node_id'])
    flagged_leaves = [row for row in leaves if row['needs_review']]
    flagged_themes = [row for row in themes if row['needs_review']]
    leaf_paths = set().union(*(members[row['node_id']] for row in flagged_leaves))
    theme_gids = {gid for row in flagged_themes for gid in row['children']}
    theme_paths = set().union(*(members.get(gid, set()) for gid in theme_gids))
    reasons = {}
    for row in flagged_leaves:
        for reason in row['review_reasons']:
            reasons.setdefault(reason, []).append(row['node_id'])
    revision = hashlib.sha256(json.dumps([leaves, themes], ensure_ascii=True,
                                         sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:24]
    return {'schema': 'catalog-quality/1', 'revision': revision,
            'review_leaf_nodes': len(flagged_leaves), 'review_leaf_files': len(leaf_paths),
            'review_theme_nodes': len(flagged_themes),
            'review_root_theme_nodes': sum(row['root'] for row in flagged_themes),
            'review_theme_files': len(theme_paths), 'review_files': len(leaf_paths | theme_paths),
            'review_reason_counts': {key: len(ids) for key, ids in sorted(reasons.items())},
            'leaf_reviews': flagged_leaves, 'theme_reviews': flagged_themes}


def catalog_review_state(conn, package_id, branches=None):
    """Call in the caller's snapshot transaction; never infer correctness."""
    if branches is None:
        from services.large_package_catalog_hierarchy import visible
        branches = visible(conn, package_id)
    rows = conn.execute('SELECT g.id,g.result,g.content_revision,l.name,l.model,l.needs_review,'
                        'l.revision,l.locked FROM large_catalog_groups g '
                        'LEFT JOIN lp_catalog_labels l ON l.package_id=g.package_id AND l.group_id=g.id '
                        'WHERE g.package_id=?', (package_id,)).fetchall()
    members = {}
    for gid, path in conn.execute('SELECT m.group_id,m.path FROM large_catalog_members m JOIN files f '
                                 'ON f.package_id=m.package_id AND f.path=m.path WHERE m.package_id=?',
                                 (package_id,)):
        members.setdefault(str(gid), set()).add(path)
    return summarize_reviews(rows, members, branches)
