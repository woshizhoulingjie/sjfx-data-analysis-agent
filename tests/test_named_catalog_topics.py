"""The published ordinary catalog uses named topics, including boundary samples."""
import copy
from collections import Counter

from services import package_analysis as pa


def fixture():
    docs = {p: {'source': {'path': p, 'name': p, 'size': 10},
                'classification': {'primary_topic': '边界待复核', 'classification_status': 'needs_review'}}
            for p in ['13.pdf', '4.pdf']}
    named = {'kind': 'group', 'node_id': 'named-risc', 'name': 'RISC-V机密计算与CoVE架构',
             'node_type': 'subtopic', 'dimension': '研究方向', 'member_paths': list(docs),
             'file_count': 2, 'total_size': 20, 'naming_source': 'model', 'naming_status': 'named',
             'evidence_chain': [{'evidence_id': 'e-1', 'text': '原始证据'}],
             'conclusion_evidence': [{'question': '“边界待复核”主题中的问题', 'answer': '证据支持的回答'}],
             'children': [{'kind': 'file', 'path': p, 'primary_topic': '边界待复核',
                           'children': [{'kind': 'evidence', 'evidence': {'text': '原始证据'}}]} for p in docs]}
    wrapper = {'kind': 'group', 'node_id': 'internal-review', 'name': '边界待复核',
               'dimension': '分类复核', 'classification_status': 'needs_review',
               'member_paths': list(docs), 'boundary_review': {'status': 'flagged', 'flagged_paths': list(docs)},
               'children': [named]}
    normal = {'kind': 'group', 'node_id': 'normal-topic', 'name': '硬件可信执行环境综述与架构分析',
              'member_paths': ['1.pdf'], 'children': []}
    return {'kind': 'analysis_root', 'dimensions': [{'name': '内容主题', 'cardinality': 2}],
            'children': [normal, wrapper]}, docs


def test_publish_existing_name_and_preserve_evidence_identity_and_files():
    tree, docs = fixture()
    normal = copy.deepcopy(tree['children'][0])
    evidence = copy.deepcopy(tree['children'][1]['children'][0]['evidence_chain'])
    result = pa._finalize_named_catalog_topics(tree, docs)
    assert result['children'][0] == normal
    topic = result['children'][1]
    assert topic['name'] == 'RISC-V机密计算与CoVE架构'
    assert topic['node_id'] == 'named-risc'
    assert topic['dimension'] == '内容主题'
    assert topic['classification_status'] == 'classified'
    assert topic['naming_source'] == 'model'
    assert topic['evidence_chain'] == evidence
    assert topic['conclusion_evidence'][0]['answer'] == '证据支持的回答'
    assert '边界待复核' not in topic['conclusion_evidence'][0]['question']
    assert topic['boundary_review']['status'] == 'flagged'
    assert Counter(topic['member_paths']) == Counter(docs.keys())
    assert all(d['classification']['primary_topic'] == topic['name'] for d in docs.values())
    assert all(n['primary_topic'] == topic['name'] for n in topic['children'])
    assert pa._primary_membership_validation(result, ['1.pdf', *docs])['valid']
    again = copy.deepcopy(result)
    assert pa._finalize_named_catalog_topics(again, docs) == result


def test_multiple_named_directions_become_independent_topics():
    tree, docs = fixture()
    first = tree['children'][1]['children'][0]
    second = copy.deepcopy(first)
    first['member_paths'] = ['13.pdf']; first['children'] = first['children'][:1]; first['file_count'] = 1
    second.update({'node_id': 'named-second', 'name': '机密计算架构路线对比', 'member_paths': ['4.pdf'], 'file_count': 1})
    second['children'] = second['children'][1:]
    tree['children'][1]['children'].append(second)
    result = pa._finalize_named_catalog_topics(tree, docs)
    assert [n['node_id'] for n in result['children']] == ['normal-topic', 'named-risc', 'named-second']
    assert result['dimensions'][0]['cardinality'] == 3
    assert pa._primary_membership_validation(result, ['1.pdf', *docs])['valid']


def test_legacy_file_only_wrapper_keeps_all_members():
    tree, docs = fixture()
    wrapper = tree['children'][1]
    wrapper['children'] = wrapper['children'][0]['children']
    wrapper['related_topics'] = ['RISC-V机密计算与CoVE架构']
    result = pa._finalize_named_catalog_topics(tree, docs)
    assert result['children'][1]['name'] == 'RISC-V机密计算与CoVE架构'
    assert result['children'][1]['node_id'] == 'internal-review'
    assert pa._primary_membership_validation(result, ['1.pdf', *docs])['valid']


def test_unflagged_topics_are_unchanged():
    tree, docs = fixture()
    tree['children'] = tree['children'][:1]
    tree['dimensions'][0]['cardinality'] = 1
    original = copy.deepcopy(tree)
    assert pa._finalize_named_catalog_topics(tree, docs) == original


def test_fresh_ordinary_import_persists_named_direction_without_review_wrapper(tmp_path, monkeypatch):
    from services.scanner import scan_directory
    from services.storage import Storage
    from services.unified_parser import UnifiedDocumentParser
    source = tmp_path / 'files'; source.mkdir()
    for i in range(5):
        (source / f'doc-{i}.txt').write_text(
            f'第{i}份材料：RISC-V机密计算采用CoVE架构和远程证明验证可信执行环境。' * 12,
            encoding='utf-8')
    scan = scan_directory(source)
    store = Storage(tmp_path / 'test.db')
    scan_id = store.save_scan(scan)
    cluster = {'cluster_id': 'SEM-0001', 'name': '硬件可信执行环境综述与架构分析',
               'members': [f'doc-{i}.txt' for i in range(5)],
               'representative_documents': ['doc-0.txt'], 'boundary_documents': ['doc-4.txt'],
               'boundary_review': {'status': 'flagged', 'flagged_paths': ['doc-4.txt']}}
    monkeypatch.setattr(pa, '_semantic_document_clusters', lambda *a, **k: ([copy.deepcopy(cluster)], 0.8))
    monkeypatch.setattr(pa, '_name_semantic_clusters', lambda groups, *a, **k: (groups, None))
    monkeypatch.setattr(pa, '_optional_llm_enrichment_enabled', lambda: True)
    def name_subtopics(tree, *args):
        for topic in tree['children']:
            if topic['name'] == '边界待复核':
                for child in topic['children']:
                    child.update(name='RISC-V机密计算与CoVE架构', naming_source='model', naming_status='named')
        return tree, None
    monkeypatch.setattr(pa, '_name_subtopic_nodes', name_subtopics)
    class Embedder:
        model = 'fixture'; max_chars = 1700
        def embed(self, values): return [[1.0, 0.0] for _ in values]
    class Naming:
        model = 'fixture'
    result = pa.analyze_package(scan_id, scan, store, UnifiedDocumentParser(max_chars=10000),
                                embedding_client=Embedder(), llm=Naming())
    topics = result['analysis_tree']['children']
    assert '边界待复核' not in [n['name'] for n in topics]
    named = next(n for n in topics if n['name'] == 'RISC-V机密计算与CoVE架构')
    assert named['member_paths'] == ['doc-4.txt']
    assert result['analysis_tree']['membership_validation']['valid']
    assert store.get_analysis(scan_id)['analysis_tree'] == result['analysis_tree']
    assert store.get_document(scan_id, 'doc-4.txt')['classification']['primary_topic'] == named['name']
    assert '边界待复核' not in [n['name'] for n in store.get_tree_page(scan_id, 'analysis')['children']]
