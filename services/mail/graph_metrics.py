"""Auditable mailbox-address graph statistics; entity merging requires confirmation."""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime
from statistics import median
import json
import uuid
from .conversations import stable_id
from .people import authorize, filtered_messages, _role_cte
from .normalization import normalize_address
from config import Config


def network(repository,package_id,owner_id,date_from=None,date_to=None, *, q='', has_attachment=None, participant=None, view='mailbox'):
    if not repository.get_package(package_id,owner_id):raise ValueError('邮件资料包不存在')
    if not Config.MAIL_ENABLE_GRAPH_METRICS:return {'nodes':[],'edges':[],'enabled':False}
    if view not in {'mailbox','person'}:raise ValueError('人物视角仅支持 mailbox 或 person')
    scope,args,filters=filtered_messages(package_id,date_from=date_from,date_to=date_to,q=q,has_attachment=has_attachment,participant=participant)
    cte=_role_cte(scope)
    with repository._connection() as c:
        mapping={r['normalized_address']:dict(r) for r in c.execute("SELECT a.normalized_address,e.entity_id,e.display_name,e.basis FROM mail_entities e JOIN mail_entity_addresses x ON x.entity_id=e.entity_id AND x.package_id=e.package_id JOIN mail_addresses a ON a.address_id=x.address_id WHERE e.package_id=? AND e.basis='manual'",(package_id,))}
        roles=[dict(r) for r in c.execute(cte+' SELECT r.*,m.sent_at_utc FROM role_rows r JOIN mail_messages m ON m.message_key=r.message_key ORDER BY r.message_key,r.address,r.role',args)]
    nodes={};sets={};by_message=defaultdict(list);adj=defaultdict(set);out=defaultdict(set);incoming=defaultdict(set)
    for row in roles:
        address=row['address'];entity=mapping.get(address)
        key=entity['entity_id'] if view=='person' and entity else address
        if key not in nodes:
            nodes[key]={'address':key,'display_name':entity['display_name'] if entity else address,'addresses':set(),'entity_id':entity['entity_id'] if entity else None,'sent_relations':0,'received_relations':0}
            sets[key]={name:set() for name in ('sent','direct','cc','bcc','received','total')}
        node=nodes[key];node['addresses'].add(address)
        role=row['role'];message=row['message_key'];sets[key]['total'].add(message)
        if role=='from':sets[key]['sent'].add(message)
        else:
            sets[key]['received'].add(message);sets[key]['direct' if role=='to' else role].add(message)
        by_message[message].append((key,address,role,row['sent_at_utc']))
    edge_sets={};relation_sets=defaultdict(set)
    for message,parts in by_message.items():
        senders={(key,address) for key,address,role,_ in parts if role=='from'}
        recipients={(key,address,role) for key,address,role,_ in parts if role!='from'}
        sent_at=parts[0][3]
        for a,from_address in senders:
            for b,to_address,role in recipients:
                edge=edge_sets.setdefault((a,b,role),{'from_address':a,'to_address':b,'recipient_role':role,'message_keys':set(),'times':[],'relations':set()})
                edge['message_keys'].add(message)
                if sent_at:edge['times'].append(sent_at)
                relation=(message,from_address,to_address,role)
                edge['relations'].add(relation);relation_sets[(a,'sent')].add(relation);relation_sets[(b,'received')].add(relation)
                out[a].add(b);incoming[b].add(a)
                if a!=b:adj[a].add(b);adj[b].add(a)
    edges=[]
    for (a,b,role),edge in sorted(edge_sets.items()):
        times=edge.pop('times');relations=edge.pop('relations');keys=sorted(edge['message_keys'])
        edge.update(message_keys=keys,message_count=len(keys),recipient_relation_count=len(relations),first_sent=min(times) if times else None,last_sent=max(times) if times else None,bidirectional=a in out[b])
        edges.append(edge)
    from .network_service import _articulation
    bridges=_articulation(adj)
    with repository._connection() as c:
        latencies=[]
        seen=set()
        for r in c.execute("SELECT e.source_message_key,e.target_message_key,s.sent_at_utc AS reply_time,t.sent_at_utc AS original_time FROM mail_thread_edges e JOIN mail_messages s ON s.message_key=e.source_message_key JOIN mail_messages t ON t.message_key=e.target_message_key WHERE e.package_id=? AND e.status='exact' AND e.relation_type='reply_to'",(package_id,)):
            if r['source_message_key'] not in by_message or r['target_message_key'] not in by_message:continue
            pair=(r['source_message_key'],r['target_message_key'])
            if pair in seen:continue
            seen.add(pair)
            try:seconds=(datetime.fromisoformat(r['reply_time'])-datetime.fromisoformat(r['original_time'])).total_seconds()
            except (TypeError,ValueError):continue
            if seconds>=0:latencies.append(seconds)
    for k,n in nodes.items():
        n.update({name:len(keys) for name,keys in sets[k].items()})
        n.update(addresses=sorted(n['addresses']),sent_relations=len(relation_sets[(k,'sent')]),received_relations=len(relation_sets[(k,'received')]))
        n.update(degree=len(adj[k]),centrality=len(adj[k])/max(1,len(nodes)-1),bridge=k in bridges,articulation_point=k in bridges,one_way=not bool(out[k]&incoming[k]))
        if k in mapping:n.update(entity=mapping[k],display_name=mapping[k]['display_name'])
    return {'nodes':sorted(nodes.values(),key=lambda n:(-n['degree'],n['address'])),'edges':edges,'count_basis':'logical','node_count_basis':'distinct_message_key','edge_count_basis':'distinct_message_key_per_recipient_role','view':view,'filters':filters,'reply_latency':{'median_seconds':median(latencies) if latencies else None,'exact_reply_count':len(latencies)},'bridge_calculated':True,
        'metrics':{'scope':'full_filtered_scope','centrality':'normalized_undirected_degree','bridge':'undirected_articulation_point','interpretation':'收发结构指标不表示现实组织、权力、阅读或信息传播'}}


def entities(repository,package_id,owner_id):
    if not repository.get_package(package_id,owner_id):raise ValueError('邮件资料包不存在')
    with repository._connection() as c:return [dict(r) for r in c.execute('SELECT e.*,a.normalized_address FROM mail_entities e JOIN mail_entity_addresses x ON x.entity_id=e.entity_id JOIN mail_addresses a ON a.address_id=x.address_id WHERE e.package_id=?',(package_id,))]


def confirm_entity(repository,package_id,owner_id,name,addresses):
    if not Config.MAIL_ENABLE_GRAPH_METRICS:raise ValueError('实体功能未启用')
    if not repository.get_package(package_id,owner_id):raise ValueError('邮件资料包不存在')
    if not isinstance(name,str) or not name.strip() or len(name.strip())>200 or not isinstance(addresses,list) or not 1<=len(addresses)<=50:raise ValueError('请填写实体名称和 1–50 个邮箱')
    if any(not isinstance(address,str) or not normalize_address(address) for address in addresses):raise ValueError('请填写有效的邮箱地址')
    addresses=sorted({normalize_address(address) for address in addresses})
    with repository._connection() as c:
        ids=[]
        for address in addresses:
            row=c.execute("SELECT DISTINCT a.address_id FROM mail_addresses a JOIN mail_message_participants p ON p.address_id=a.address_id JOIN mail_visible_occurrences o ON o.message_key=p.message_key WHERE o.package_id=? AND a.normalized_address=? AND p.role IN ('from','to','cc','bcc')",(package_id,address)).fetchone()
            if not row:raise ValueError('邮箱不在当前资料包')
            if c.execute('SELECT 1 FROM mail_entity_addresses WHERE package_id=? AND address_id=?',(package_id,row[0])).fetchone():raise ValueError('邮箱已归属实体，请勿重复归并')
            ids.append(row[0])
        eid=stable_id(package_id,name.strip(),sorted(ids))
        c.execute("INSERT INTO mail_entities(entity_id,package_id,entity_type,display_name,basis,confirmed_by) VALUES(?,?,'person',?,'manual',?)",(eid,package_id,str(name).strip()[:200],owner_id))
        c.executemany("INSERT INTO mail_entity_addresses VALUES(?,?,?,'manual')",[(package_id,eid,a) for a in ids])
        _audit(c,package_id,eid,owner_id,'confirmed',{'display_name':name.strip(),'addresses':addresses})
    return eid


def _audit(c,package_id,entity_id,owner_id,action,payload):
    c.execute('INSERT INTO mail_entity_audit(audit_id,package_id,entity_id,actor_id,action,payload_json) VALUES(?,?,?,?,?,?)',
        (uuid.uuid4().hex,package_id,entity_id,owner_id,action,json.dumps(payload,ensure_ascii=False)))


def revoke_entity(repository,package_id,owner_id,entity_id,reason=''):
    authorize(repository,package_id,owner_id)
    if not isinstance(entity_id,str) or not entity_id:raise ValueError('请选择要撤销的人物归并')
    with repository._connection() as c:
        row=c.execute('SELECT * FROM mail_entities WHERE package_id=? AND entity_id=?',(package_id,entity_id)).fetchone()
        if not row:raise LookupError('人物归并不存在或不属于当前资料包')
        addresses=[r[0] for r in c.execute('SELECT a.normalized_address FROM mail_entity_addresses ea JOIN mail_addresses a ON a.address_id=ea.address_id WHERE ea.package_id=? AND ea.entity_id=? ORDER BY a.normalized_address',(package_id,entity_id))]
        _audit(c,package_id,entity_id,owner_id,'revoked',{'display_name':row['display_name'],'addresses':addresses,'reason':str(reason or '')[:1000]})
        c.execute('DELETE FROM mail_entity_addresses WHERE package_id=? AND entity_id=?',(package_id,entity_id))
        c.execute('DELETE FROM mail_entities WHERE package_id=? AND entity_id=?',(package_id,entity_id))
    return {'entity_id':entity_id,'addresses':addresses,'revoked':True}


def entity_history(repository,package_id,owner_id,entity_id=None,*,offset=0,limit=50):
    authorize(repository,package_id,owner_id)
    offset,limit=max(0,int(offset)),max(1,min(100,int(limit)))
    where='package_id=?';args=[package_id]
    if entity_id:where+=' AND entity_id=?';args.append(str(entity_id))
    with repository._connection() as c:
        total=c.execute('SELECT COUNT(*) FROM mail_entity_audit WHERE '+where,args).fetchone()[0]
        items=[dict(r) for r in c.execute('SELECT * FROM mail_entity_audit WHERE '+where+' ORDER BY created_at DESC,rowid DESC LIMIT ? OFFSET ?',[*args,limit,offset])]
    for item in items:item['payload']=json.loads(item.pop('payload_json'))
    return {'items':items,'total':total,'offset':offset,'limit':limit,'package_id':package_id}
