"""Auditable mailbox-address graph statistics; entity merging requires confirmation."""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime
from statistics import median
from .conversations import stable_id
from config import Config


def network(repository,package_id,owner_id,date_from=None,date_to=None):
    if not repository.get_package(package_id,owner_id):raise ValueError('邮件资料包不存在')
    if not Config.MAIL_ENABLE_GRAPH_METRICS:return {'nodes':[],'edges':[],'enabled':False}
    summary=repository.communication(package_id,owner_id,date_from=date_from,date_to=date_to)
    edges=summary['items'];nodes={};adj=defaultdict(set);out=defaultdict(set);incoming=defaultdict(set)
    for e in edges:
        a,b=e['from_address'],e['to_address'];out[a].add(b);incoming[b].add(a)
        adj[a].add(b);adj[b].add(a)
        for k in [a,b]:nodes.setdefault(k,{'address':k,'display_name':k,'sent':0,'received':0})
        nodes[a]['sent']+=e['message_count'];nodes[b]['received']+=e['message_count']
        e['bidirectional']=a in out[b] or any(x['from_address']==b and x['to_address']==a for x in edges)
    # Tarjan articulation points over the undirected communication projection.
    times={};low={};bridges=set();counter=[0]
    def visit(u,parent=None):
        counter[0]+=1;times[u]=low[u]=counter[0];children=0
        for v in adj[u]:
            if v==parent:continue
            if v not in times:
                children+=1;visit(v,u);low[u]=min(low[u],low[v])
                if parent is not None and low[v]>=times[u]:bridges.add(u)
            else:low[u]=min(low[u],times[v])
        if parent is None and children>1:bridges.add(u)
    if len(adj)<800:
        for k in adj:
            if k not in times:visit(k)
    with repository._connection() as c:
        mapping={r['normalized_address']:dict(r) for r in c.execute('SELECT a.normalized_address,e.entity_id,e.display_name,e.basis FROM mail_entities e JOIN mail_entity_addresses x ON x.entity_id=e.entity_id JOIN mail_addresses a ON a.address_id=x.address_id WHERE e.package_id=?',(package_id,))}
        latencies=[]
        seen=set()
        for r in c.execute("SELECT e.source_message_key,e.target_message_key,s.sent_at_utc AS reply_time,t.sent_at_utc AS original_time FROM mail_thread_edges e JOIN mail_messages s ON s.message_key=e.source_message_key JOIN mail_messages t ON t.message_key=e.target_message_key WHERE e.package_id=? AND e.status='exact' AND e.relation_type='reply_to'",(package_id,)):
            if date_from and (not r['reply_time'] or r['reply_time']<date_from):continue
            if date_to and (not r['reply_time'] or r['reply_time']>date_to):continue
            pair=(r['source_message_key'],r['target_message_key'])
            if pair in seen:continue
            seen.add(pair)
            try:seconds=(datetime.fromisoformat(r['reply_time'])-datetime.fromisoformat(r['original_time'])).total_seconds()
            except (TypeError,ValueError):continue
            if seconds>=0:latencies.append(seconds)
        for e in edges:
            e['message_keys']=[r[0] for r in c.execute('SELECT e.message_key FROM mail_communication_edges e JOIN mail_addresses a ON a.address_id=e.from_address_id JOIN mail_addresses b ON b.address_id=e.to_address_id WHERE e.package_id=? AND a.normalized_address=? AND b.normalized_address=? AND e.recipient_role=? AND (? IS NULL OR e.sent_at_utc>=?) AND (? IS NULL OR e.sent_at_utc<=?)',(package_id,e['from_address'],e['to_address'],e['recipient_role'],date_from,date_from,date_to,date_to))]
    for k,n in nodes.items():
        n.update(degree=len(adj[k]),centrality=len(adj[k])/max(1,len(nodes)-1),bridge=k in bridges,one_way=not bool(out[k]&incoming[k]))
        if k in mapping:n.update(entity=mapping[k],display_name=mapping[k]['display_name'])
    return {'nodes':sorted(nodes.values(),key=lambda n:-n['degree']),'edges':edges,'count_basis':'logical','reply_latency':{'median_seconds':median(latencies) if latencies else None,'exact_reply_count':len(latencies)},'bridge_calculated':len(adj)<800}


def entities(repository,package_id,owner_id):
    if not repository.get_package(package_id,owner_id):raise ValueError('邮件资料包不存在')
    with repository._connection() as c:return [dict(r) for r in c.execute('SELECT e.*,a.normalized_address FROM mail_entities e JOIN mail_entity_addresses x ON x.entity_id=e.entity_id JOIN mail_addresses a ON a.address_id=x.address_id WHERE e.package_id=?',(package_id,))]


def confirm_entity(repository,package_id,owner_id,name,addresses):
    if not Config.MAIL_ENABLE_GRAPH_METRICS:raise ValueError('实体功能未启用')
    if not repository.get_package(package_id,owner_id):raise ValueError('邮件资料包不存在')
    if not str(name).strip() or not isinstance(addresses,list) or not 1<=len(addresses)<=50:raise ValueError('请填写实体名称和 1–50 个邮箱')
    with repository._connection() as c:
        ids=[]
        for address in set(addresses):
            row=c.execute('SELECT DISTINCT a.address_id FROM mail_addresses a JOIN mail_message_participants p ON p.address_id=a.address_id JOIN mail_occurrences o ON o.message_key=p.message_key WHERE o.package_id=? AND a.normalized_address=?',(package_id,str(address).casefold())).fetchone()
            if not row:raise ValueError('邮箱不在当前资料包')
            if c.execute('SELECT 1 FROM mail_entity_addresses WHERE package_id=? AND address_id=?',(package_id,row[0])).fetchone():raise ValueError('邮箱已归属实体，请勿重复归并')
            ids.append(row[0])
        eid=stable_id(package_id,name,sorted(ids))
        c.execute("INSERT INTO mail_entities(entity_id,package_id,entity_type,display_name,basis,confirmed_by) VALUES(?,?,'person',?,'manual',?)",(eid,package_id,str(name).strip()[:200],owner_id))
        c.executemany("INSERT INTO mail_entity_addresses VALUES(?,?,?,'manual')",[(package_id,eid,a) for a in ids])
    return eid
