"""The actual paged-tree API must expose every selected physical file."""
from test_catalog_handoff_real_files import handoff
from services.large_package_isolated import iter_inventory


def test_every_parent_can_expand_without_phantom_next_page(handoff):
    _, _, store, result = handoff('a/b/c/report.txt')
    scan_id = result['scan_id']
    for parent, child in [('.', 'a'), ('a', 'a/b'), ('a/b', 'a/b/c'), ('a/b/c', 'a/b/c/report.txt')]:
        page = store.get_tree_page(scan_id, node_key=parent, limit=1)
        assert page['has_children'] is True
        assert page['child_count'] == page['_children_total'] == 1
        assert page['_children_next_offset'] is None
        assert [item['path'] for item in page['children']] == [child]
        assert page['children'][0]['has_children'] is (child != 'a/b/c/report.txt')
    leaf = store.get_tree_page(scan_id, node_key='a/b/c/report.txt')
    assert leaf['has_children'] is False and leaf['child_count'] == 0


def test_paging_counts_direct_children_and_matches_full_tree_order(handoff):
    root, large, store, first = handoff('a/b/report.txt')
    for path in ['root.txt', 'z/c/another.txt', 'a/b/second.txt']:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('Physical paging acceptance', encoding='utf-8')
    package_id = store.get_scan(first['scan_id'])['source_large_package_id']
    rows = list(iter_inventory(root))
    large.upsert_files(package_id, rows)
    selected = large.export_selection(package_id, paths=[item['path'] for item in rows])
    result = store.create_scan_from_large_selection(large.get(package_id), selected, name='分页验收',
        parse_mode='fast', owner_id='real-owner', request_key='paging', large_store=large)
    scan_id = result['scan_id']
    scan = store.get_scan(scan_id)
    for parent, total in [('.', 3), ('a/b', 2)]:
        node = scan['tree']
        if parent != '.':
            for component in parent.split('/'):
                node = next(child for child in node['children'] if child['name'] == component)
        expected = [child['path'] for child in node['children']]
        observed = []
        for offset in range(total):
            page = store.get_tree_page(scan_id, node_key=parent, offset=offset, limit=1)
            assert page['_children_total'] == total
            assert page['_children_next_offset'] == (offset + 1 if offset + 1 < total else None)
            observed.extend(child['path'] for child in page['children'])
        assert observed == expected
        assert len(set(observed)) == total
