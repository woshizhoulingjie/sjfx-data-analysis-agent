"""Boundary review is a lossless, scoped part of ordinary smart catalogs."""
import copy
from collections import Counter
from unittest.mock import patch

import pytest
from services import package_analysis as pa


def documents(count=5):
    return {f'doc-{i}.txt': {
        'source': {'path': f'doc-{i}.txt', 'name': f'doc-{i}.txt', 'size': 100+i, 'extension': '.txt'},
        'text': f'工程资料{i}。合同采购与交付记录，验收状态需参考原文。',
        'structure': {'headings': [f'工程记录{i}']}, 'evidence': [],
    } for i in range(count)}


class Embedder:
    model='test-boundary';max_chars=1700
    def embed(self, texts):
        return [[1.0, 0.1+i*0.001, 0.01] for i in range(len(texts))]


class Naming:
    def __init__(self, mismatches):
        self.mismatches=mismatches;self.prompts=[]
    def chat_json(self, system, prompt, **kwargs):
        self.prompts.append(prompt)
        return {'json': {'clusters': [{'cluster_id': 'SEM-0001',
            'name': '采购交付记录', 'summary': '采购与交付核对资料。',
            'keywords': ['交付'], 'boundary_mismatches': self.mismatches}]}}


def cluster():
    return {'cluster_id': 'SEM-0001','name':'采购交付记录','members':list(documents()),
        'representative_documents':['doc-0.txt','doc-1.txt','doc-2.txt'],
        'boundary_documents':['doc-3.txt','doc-4.txt'], 'profile_fingerprint':'fixed',
        'boundary_review':{'status':'flagged','flagged_paths':['doc-4.txt']}}


@pytest.mark.parametrize('strategy',['centroid','legacy'])
def test_cluster_results_preserve_boundary_samples(monkeypatch,strategy):
    monkeypatch.setenv('SJFX_SEMANTIC_CLUSTERING',strategy)
    class KMeans:
        def __init__(self,**kwargs):self.cluster_centers_=[[1,0,0]]
        def fit_predict(self,values):
            import numpy as np
            return np.array([0]*len(values))
    with patch('sklearn.cluster.MiniBatchKMeans',KMeans):
        groups,_=pa._semantic_document_clusters(documents(),Embedder())
    assert len(groups)==1
    g=groups[0];boundaries=g.get('boundary_documents') or []
    assert len(boundaries)==2
    assert set(boundaries).isdisjoint(g['representative_documents'])
    assert set(boundaries)<=set(g['members'])
    naming=Naming([boundaries[0]])
    named,_=pa._name_semantic_clusters(groups,documents(),naming)
    assert all(p in naming.prompts[0] for p in boundaries)
    assert named[0]['boundary_review']['flagged_paths']==[boundaries[0]]


def test_centroid_fallback_samples_are_disjoint(monkeypatch):
    monkeypatch.setenv('SJFX_SEMANTIC_CLUSTERING','centroid')
    with patch('sklearn.cluster.MiniBatchKMeans',side_effect=ValueError('test fallback')):
        groups,_=pa._semantic_document_clusters(documents(4),Embedder())
    for group in groups:
        assert set(group.get('boundary_documents') or []).isdisjoint(group['representative_documents'])
        if len(group['members'])>3:assert group['boundary_documents']


def test_reviewed_files_remain_visible_selectable_and_uniquely_assigned():
    docs=documents();tree=pa._semantic_adaptive_tree({'root':'/synthetic'},docs,{},[cluster()])
    counts=Counter(p for group in tree['children'] for p in group['member_paths'])
    assert counts==Counter(docs.keys())
    normal,review=tree['children']
    assert 'doc-4.txt' not in normal['member_paths']
    assert review['classification_status']=='needs_review'
    assert review['member_paths']==['doc-4.txt']
    def leaves(node):
        if node.get('kind')=='file':yield node['path']
        for child in node.get('children',[]):yield from leaves(child)
    assert list(leaves(review))==['doc-4.txt']
    assert review['total_size']==104
    assert docs['doc-4.txt']['classification']['primary_topic']=='边界待复核'
    assert not docs['doc-4.txt']['classification']['topic_memberships']
    assert pa._primary_membership_validation(tree,docs)['valid']
    assert tree['dimensions'][0]['cardinality']==2


def test_all_reviewed_members_survive_with_stable_review_identity():
    c=cluster();c['members']=['doc-3.txt','doc-4.txt'];c['representative_documents']=[]
    c['boundary_review']['flagged_paths']=['doc-4.txt','doc-3.txt','doc-4.txt']
    docs={p:d for p,d in documents().items() if p in c['members']}
    first=pa._semantic_adaptive_tree({'root':'/synthetic'},copy.deepcopy(docs),{},[c])
    second=pa._semantic_adaptive_tree({'root':'/synthetic'},copy.deepcopy(docs),{},[copy.deepcopy(c)])
    assert len(first['children'])==1
    assert first['children'][0]['member_paths']==sorted(docs)
    assert first['children'][0]['node_id']==second['children'][0]['node_id']
    assert pa._primary_membership_validation(first,docs)['valid']


@pytest.mark.parametrize('flag',['outside.txt','../private.txt','doc-0.txt'])
def test_review_flags_cannot_escape_sampled_member_scope(flag):
    c=cluster();c['boundary_review']['flagged_paths']=[flag]
    tree=pa._semantic_adaptive_tree({'root':'/synthetic'},documents(),{},[c])
    assert len(tree['children'])==1
    assert tree['children'][0]['member_paths']==list(documents())


@pytest.mark.parametrize('value',[None,{},'doc-4.txt',[None],['outside.txt']])
def test_malformed_model_review_cannot_claim_passed(value):
    named,_=pa._name_semantic_clusters([cluster()],documents(),Naming(value))
    assert named[0]['boundary_review']['status']=='unverified'
    assert not named[0]['boundary_review']['flagged_paths']


def test_valid_empty_review_keeps_existing_membership():
    named,_=pa._name_semantic_clusters([cluster()],documents(),Naming([]))
    assert named[0]['boundary_review']['status']=='passed'
    tree=pa._semantic_adaptive_tree({'root':'/synthetic'},documents(),{},named)
    assert len(tree['children'])==1
    assert pa._primary_membership_validation(tree,documents())['valid']


def test_cache_identity_changes_when_review_samples_change():
    a=cluster();b=copy.deepcopy(a);b['boundary_documents']=['doc-3.txt']
    assert pa._semantic_naming_cache_id(a)!=pa._semantic_naming_cache_id(b)


def test_cached_flagged_review_also_preserves_files():
    c=cluster();cached={pa._semantic_naming_cache_id(c):{'name':'采购交付记录',
        'summary':'缓存主题','boundary_review_schema':1,'boundary_review':c['boundary_review']}}
    named,_=pa._name_semantic_clusters([c],documents(),llm=None,cached_names=cached)
    tree=pa._semantic_adaptive_tree({'root':'/synthetic'},documents(),{},named)
    assert named[0]['naming_source']=='persistent_cache'
    assert pa._primary_membership_validation(tree,documents())['valid']
    assert tree['children'][-1]['classification_status']=='needs_review'


def test_restored_samples_share_existing_profile_budget():
    limits=[]
    original=pa._semantic_document_profile
    def profile(path,document,max_chars):
        limits.append(max_chars)
        return original(path,document,max_chars=max_chars)
    naming=Naming([])
    with patch.object(pa,'_semantic_document_profile',side_effect=profile):
        pa._name_semantic_clusters([cluster()],documents(),naming)
    assert len(limits)==5
    assert sum(limits)<=3*850
    assert len(naming.prompts)==1
    assert all(path in naming.prompts[0] for path in documents())
