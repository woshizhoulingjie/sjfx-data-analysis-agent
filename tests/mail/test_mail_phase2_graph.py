"""Progressive graph contract: bounded payload, role unions and signed scopes."""
from email.message import EmailMessage
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from config import Config
from services.storage import Storage
from services.mail.repository import MailRepository
from services.mail.models import MailMember
from services.mail.parsers.eml import parse_eml
from services.mail.graph_metrics import network, confirm_entity, revoke_entity
from services.mail import network_service as graph


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, 'MAIL_ENABLE_GRAPH_METRICS', True)
    monkeypatch.setattr(Config, 'MAIL_FEATURE_ENABLED', True)
    repo = MailRepository(Storage(tmp_path / 'graph.sqlite3'))
    repo.migrate()
    package, _ = repo.create_package(owner_id='owner', display_name='Synthetic graph', source_kind='directory',
        source_path=str(tmp_path), source_revision='v1', config={})
    pid, serial = package['package_id'], [0]

    def add(sender='a@example.com', to='b@example.com', cc=None, bcc=None, raw=None,
            subject='Graph terms', date='Tue, 07 May 2024 12:00:00 +0000', package_id=None, body='Please review terms before approval.'):
        serial[0] += 1
        if raw is None:
            message = EmailMessage()
            message['From'], message['To'] = sender, to
            message['Message-ID'] = '<graph-%s@example.com>' % serial[0]
            message['Subject'] = subject
            if date: message['Date'] = date
            if cc: message['Cc'] = cc
            if bcc: message['Bcc'] = bcc
            message.set_content(body)
            raw = message.as_bytes()
        result = repo.upsert_ingested_message(package_id=package_id or pid,
            source={'locator_type':'directory','root_path':str(tmp_path),'source_revision':'v1'},
            member=MailMember('%s.eml' % serial[0],serial[0],len(raw),{}), raw=raw, parsed=parse_eml(raw))
        return result['message_key'], raw

    yield repo, pid, add
    with graph._LOCK:
        graph._CACHE.clear()
        for connection, _ in graph._WATCHERS.values(): connection.close()
        graph._WATCHERS.clear()


def test_large_scope_search_and_neighbor_pages_are_bounded_and_cached(corpus):
    repo, pid, add = corpus
    add(to=', '.join('contact%03d@example.com' % i for i in range(500)))
    with patch.object(graph, '_build_index', wraps=graph._build_index) as build:
        overview = graph.overview(repo,pid,'owner')
        assert overview['node_total'] == 501 and len(overview['nodes']) == 30
        assert len(overview['edges']) <= graph.MAX_EDGES
        assert 'message_keys' not in json.dumps(overview)
        assert overview['unexpanded_nodes'] == 471
        found = graph.search(repo,pid,'owner',node_query='contact499',manifest=overview['manifest'])
        assert [n['address'] for n in found['nodes']] == ['contact499@example.com']
        seen, cursor = [], None
        for _ in range(18):
            page = graph.neighbors(repo,pid,'owner',node='a@example.com',cursor=cursor,limit=30)
            assert len(page['nodes']) <= 31 and page['neighbor_total'] == 500
            seen.extend(n['address'] for n in page['nodes'] if n['address'] != 'a@example.com')
            cursor = page['next_cursor']
            if not cursor: break
        assert len(set(seen)) == len(seen) == 500
        assert build.call_count == 1  # no role/message scans on repeated pagination
    assert len(json.dumps(overview)) < len(json.dumps(network(repo,pid,'owner'))) / 5


def test_role_overlap_duplicates_and_recipient_only_pairs(corpus):
    repo,pid,add=corpus
    first,raw=add(to='b@example.com, c@example.com',cc='b@example.com',bcc='b@example.com')
    add(raw=raw)
    second,_=add(sender='b@example.com',to='a@example.com')
    result=graph.overview(repo,pid,'owner')
    a=next(n for n in result['nodes'] if n['address']=='a@example.com')
    b=next(n for n in result['nodes'] if n['address']=='b@example.com')
    assert (a['sent'],a['received'],a['total']) == (1,1,2)
    assert (b['direct'],b['cc'],b['bcc'],b['received'],b['total']) == (1,1,1,1,2)
    edge=next(e for e in result['edges'] if e['from_address']=='a@example.com' and e['to_address']=='b@example.com')
    assert edge['message_count']==1 and edge['role_counts']=={'to':1,'cc':1,'bcc':1}
    assert edge['recipient_relation_count']==3
    assert not any({e['from_address'],e['to_address']}=={'b@example.com','c@example.com'} for e in result['edges'])
    assert graph.edge_messages(repo,pid,'owner',from_node='a@example.com',to_node='b@example.com')['items'][0]['message_key']==first
    assert graph.edge_messages(repo,pid,'owner',from_node='b@example.com',to_node='a@example.com')['items'][0]['message_key']==second
    assert graph.edge_messages(repo,pid,'owner',from_node='a@example.com',to_node='b@example.com',recipient_role='cc')['total']==1


def test_cursor_tampering_owner_filter_view_and_selector_are_rejected(corpus):
    repo,pid,add=corpus
    add(to='b@example.com, c@example.com, d@example.com')
    first=graph.neighbors(repo,pid,'owner',node='a@example.com',limit=1)
    token=first['next_cursor']
    with pytest.raises(ValueError): graph.neighbors(repo,pid,'owner',node='a@example.com',limit=1,cursor=token[:-5]+'abcde')
    with pytest.raises(LookupError): graph.neighbors(repo,pid,'intruder',node='a@example.com',limit=1,cursor=token)
    with pytest.raises(RuntimeError): graph.neighbors(repo,pid,'owner',node='a@example.com',limit=1,cursor=token,view='person')
    with pytest.raises(RuntimeError): graph.neighbors(repo,pid,'owner',node='a@example.com',limit=1,cursor=token,q='Graph')
    with pytest.raises(ValueError): graph.search(repo,pid,'owner',node_query='',limit=1,cursor=token)
    other,_=repo.create_package(owner_id='owner',display_name='Other',source_kind='directory',source_path='x',source_revision='v1',config={})
    add(package_id=other['package_id'],to='b@example.com, c@example.com')
    with pytest.raises(RuntimeError): graph.neighbors(repo,other['package_id'],'owner',node='a@example.com',limit=1,cursor=token)


def test_commits_in_another_connection_invalidate_existing_views_and_merges(corpus):
    repo,pid,add=corpus
    add(to='b@example.com, c@example.com, alias@example.com')
    old=graph.neighbors(repo,pid,'owner',node='a@example.com',limit=1)
    entity=confirm_entity(repo,pid,'owner','Alice',['a@example.com','alias@example.com'])
    with pytest.raises(RuntimeError): graph.neighbors(repo,pid,'owner',node='a@example.com',limit=1,cursor=old['next_cursor'])
    merged=graph.overview(repo,pid,'owner',view='person')
    assert len(merged['nodes'])==3
    assert next(n for n in merged['nodes'] if n['address']==entity)['total']==1
    revoke_entity(repo,pid,'owner',entity)
    with pytest.raises(RuntimeError): graph.search(repo,pid,'owner',manifest=merged['manifest'],view='person')
    filtered=graph.overview(repo,pid,'owner',q='Please')
    with repo._connection() as c: c.execute("UPDATE mail_text_segments SET text='Changed' WHERE package_id=?",(pid,))
    with pytest.raises(RuntimeError): graph.search(repo,pid,'owner',manifest=filtered['manifest'],q='Please')
    assert graph.overview(repo,pid,'owner',q='Please')['nodes']==[]


def test_date_conversation_scope_articulation_and_current_edge_counts(corpus):
    from services.mail.conversations import rebuild
    repo,pid,add=corpus
    key,_=add(to='b@example.com, c@example.com')
    add(sender='b@example.com',to='d@example.com',date='Mon, 06 May 2024 12:00:00 +0000')
    rebuild(repo,pid,'owner')
    with repo._connection() as c:
        cid=c.execute('SELECT cm.conversation_id FROM mail_conversation_members cm JOIN mail_conversations cv ON cv.conversation_id=cm.conversation_id WHERE cv.package_id=? AND cm.message_key=?',(pid,key)).fetchone()[0]
    scoped=graph.overview(repo,pid,'owner',conversation_id=cid,date_from='2024-05-07',date_to='2024-05-07')
    assert scoped['message_total']==1 and scoped['node_total']==3
    assert next(n for n in scoped['nodes'] if n['address']=='a@example.com')['articulation_point']
    assert scoped['metrics']['centrality']=='normalized_undirected_degree'
    assert graph.overview(repo,pid,'owner',as_of='2024-05-06')['message_total']==1


def test_edge_message_cursor_covers_only_current_logical_pair(corpus):
    repo,pid,add=corpus
    expected={add()[0] for _ in range(7)}
    add(to='outside@example.com')
    keys,cursor=[],None
    for _ in range(4):
        data=graph.edge_messages(repo,pid,'owner',from_node='a@example.com',to_node='b@example.com',limit=2,cursor=cursor)
        assert data['total']==7 and len(data['items'])<=2
        keys.extend(m['message_key'] for m in data['items']);cursor=data['next_cursor']
    assert set(keys)==expected and len(keys)==7 and cursor is None
    with pytest.raises(LookupError):graph.edge_messages(repo,pid,'owner',from_node='b@example.com',to_node='outside@example.com')


def test_semantic_scope_uses_shared_ledger_and_node_actors(corpus):
    from services.mail.issue_repository import IssueRepository
    repo,pid,add=corpus
    key,_=add()
    with repo._connection() as c: segment=dict(c.execute('SELECT * FROM mail_text_segments WHERE package_id=? AND message_key=?',(pid,key)).fetchone())
    ledger=IssueRepository(repo)
    ingested=ledger.ingest_checked_findings(pid,'owner',[{'actor':'a@example.com','intent':'request','issue':'Terms review','statement':'Please review terms before approval.',
        'citations':[{'message_key':key,'mime_path':segment['mime_path'],'char_start':0,'quote':'Please review terms before approval.'}]}])
    assert not ingested['rejected']
    issue=ingested['issues'][0]
    add(to='outside@example.com')
    scoped=graph.overview(repo,pid,'owner',issue_id=issue)
    assert scoped['message_total']==1 and scoped['node_total']==2
    page=graph.issue_page(repo,pid,'owner',nodes=['a@example.com','b@example.com'])
    assert page['total']==1 and page['items'][0]['issue_id']==issue
    assert page['items'][0]['review_status']=='unresolved'
    assert graph.issue_page(repo,pid,'owner',nodes=['outside@example.com'])['total']==0


def test_http_overview_and_pages_retain_envelope_and_owner_guard(corpus):
    from fastapi.testclient import TestClient
    from web_compat import SJFXFastAPI
    from services.mail.api import register_mail_routes
    repo,pid,add=corpus
    add(to=', '.join('r%d@example.com'%i for i in range(100)))
    owner=['owner']
    services=SimpleNamespace(repository=repo,owner_resolver=lambda:owner[0])
    app=SJFXFastAPI();register_mail_routes(app,services)
    with TestClient(app) as client:
        base='/api/mail/packages/'+pid+'/relations/network'
        data=client.get(base).json();assert data['ok'] and data['network']['progressive']
        assert len(data['network']['nodes'])==30
        assert client.get(base+'/search',params={'node_query':'r99'}).json()['network']['total']==1
        old=client.get(base+'/neighbors',params={'node':'a@example.com','limit':1}).json()['network']
        stale=client.get(base+'/neighbors',params={'node':'a@example.com','limit':1,'cursor':old['next_cursor'],'view':'person'})
        assert stale.status_code==409
        owner[0]='intruder'
        assert client.get(base).status_code==404


def test_issue_timeline_cursors_bind_sources_actors_as_of_and_message_filters(corpus):
    from services.mail.issue_repository import IssueRepository
    repo,pid,add=corpus
    first,_=add(subject='Visible scope',body='Q-20 approve terms.')
    second,_=add(sender='b@example.com',to='a@example.com',subject='Visible scope',
        body='Q-20 approve terms.',date='Wed, 08 May 2024 12:00:00 +0000')
    outside,_=add(to='outsider@example.com',subject='Outside scope',body='Q-20 approve terms.',
        date='Thu, 09 May 2024 12:00:00 +0000')
    ledger=IssueRepository(repo)
    findings=[]
    for key,actor in ((first,'a@example.com'),(second,'b@example.com'),(outside,'a@example.com')):
        with repo._connection() as c:
            segment=dict(c.execute('SELECT * FROM mail_text_segments WHERE package_id=? AND message_key=?',(pid,key)).fetchone())
        findings.append({'actor':actor,'intent':'request','issue':'Terms review','statement':'Q-20 approve terms.',
            'object_identity':'terms','object_version':'v2','issue_anchor':{'kind':'quotation_id','value':'Q-20','object':'terms'},
            'citations':[{'message_key':key,'mime_path':segment['mime_path'],'char_start':0,'quote':'Q-20 approve terms.'}]})
    ingested=ledger.ingest_checked_findings(pid,'owner',findings)
    assert not ingested['rejected'] and len(ingested['issues'])==1
    issue=ingested['issues'][0]
    overview=graph.overview(repo,pid,'owner',q='Visible scope',as_of='2024-05-08')
    kwargs={'timeline_issue_id':issue,'nodes':['a@example.com','b@example.com'],
        'limit':1,'q':'Visible scope','as_of':'2024-05-08','manifest':overview['manifest']}
    page=graph.issue_timeline(repo,pid,'owner',**kwargs)
    assert page['total']==2 and len(page['items'])==1 and page['next_cursor']
    more=graph.issue_timeline(repo,pid,'owner',cursor=page['next_cursor'],**kwargs)
    assert {a['message_key'] for a in page['items']+more['items']}=={first,second}
    assert more['next_cursor'] is None
    with pytest.raises(ValueError):
        graph.issue_timeline(repo,pid,'owner',cursor=page['next_cursor'],**{**kwargs,'nodes':['a@example.com']})
    with pytest.raises(RuntimeError):
        graph.issue_timeline(repo,pid,'owner',cursor=page['next_cursor'],**{**kwargs,'as_of':'2024-05-07'})
    actor=graph.issue_timeline(repo,pid,'owner',**{**kwargs,'nodes':['b@example.com']})
    assert actor['total']==1 and actor['items'][0]['actor']=='b@example.com'
    with pytest.raises(LookupError):graph.issue_timeline(repo,pid,'intruder',**kwargs)
    from fastapi.testclient import TestClient
    from web_compat import SJFXFastAPI
    from services.mail.api import register_mail_routes
    app=SJFXFastAPI();register_mail_routes(app,SimpleNamespace(repository=repo,owner_resolver=lambda:'owner'))
    with TestClient(app) as client:
        url='/api/mail/packages/'+pid+'/relations/network/issues/'+issue+'/timeline'
        params={'nodes':json.dumps(kwargs['nodes']),'manifest':overview['manifest'],'q':'Visible scope','as_of':'2024-05-08','limit':1}
        response=client.get(url,params=params)
        assert response.status_code==200 and response.json()['total']==2 and response.json()['next_cursor']
        assert client.get(url,params={**params,'nodes':'[{}]'}).status_code==400
        assert client.get(url,params={**params,'as_of':'2024-05-07'}).status_code==409


def test_issue_nodes_are_validated_without_unhashable_input_errors(corpus):
    repo,pid,add=corpus
    add()
    for nodes in ([{}],[['a@example.com']],[None],['outside@example.com'],[]):
        with pytest.raises(ValueError):graph.issue_page(repo,pid,'owner',nodes=nodes)


def test_legacy_metrics_use_iterative_full_scope_articulation(corpus):
    adjacency={str(i):{str(j) for j in (i-1,i+1) if 0<=j<2000} for i in range(2000)}
    assert graph._articulation(adjacency)=={str(i) for i in range(1,1999)}
    repo,pid,add=corpus
    add(to='b@example.com,c@example.com')
    legacy=network(repo,pid,'owner')
    assert legacy['bridge_calculated']
    assert legacy['metrics']['bridge']=='undirected_articulation_point'
    assert legacy['metrics']['scope']=='full_filtered_scope'
