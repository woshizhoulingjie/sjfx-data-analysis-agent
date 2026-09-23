"""Versioned multilevel navigation; every leaf has one parent and keeps its files."""
import hashlib
import json
import math
import time
from contextlib import closing
import numpy as np
from services.large_package_catalog_quality import unit


def init(c):
    c.execute('CREATE TABLE IF NOT EXISTS lp_catalog_tree(package_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,payload TEXT NOT NULL)')
    c.execute('CREATE TRIGGER IF NOT EXISTS lp_tree_delete AFTER DELETE ON packages BEGIN DELETE FROM lp_catalog_tree WHERE package_id=OLD.id; END')


def fingerprint(leaves):
    value=[(x['group_id'],x['content_revision'],x['version'],x['name']) for x in sorted(leaves,key=lambda x:x['group_id'])]
    return hashlib.sha256(('tree/2'+json.dumps(value,ensure_ascii=False)).encode()).hexdigest()


def build(store,package_id,round_state,cancelled=lambda:False,model_client=None):
    from services import large_package_catalog_naming as naming
    from services.large_package_catalog_topics import name_topics
    from services.large_package_semantic import _kmeans_fit
    with closing(store._connect()) as c:
        leaves=[dict(r) for r in c.execute('''SELECT g.id group_id,g.content_revision,g.anchor,
          g.member_count files,l.name,l.description,l.version FROM large_catalog_groups g JOIN lp_catalog_labels l
          ON l.package_id=g.package_id AND l.group_id=g.id WHERE g.package_id=? AND g.member_count>0
          AND (l.revision=g.content_revision OR l.locked=1) AND g.id NOT LIKE 'km-other-%'
          AND g.id NOT LIKE 'km-pending-%' ORDER BY g.id''',(package_id,))]
        old=c.execute('SELECT fingerprint,payload FROM lp_catalog_tree WHERE package_id=?',(package_id,)).fetchone()
    signature=fingerprint(leaves)
    previous=json.loads(old['payload']) if old else {}
    if old and old[0]==signature and (not previous.get('pending_names') or previous.get('attempted_round')==round_state['id']):return False
    if not leaves or cancelled():return False
    vectors={}
    for leaf in leaves:
        v=np.frombuffer(leaf['anchor'],dtype=np.float32)
        if len(v) and np.isfinite(v).all() and np.linalg.norm(v)>1e-8:vectors[leaf['group_id']]=unit(v)
    model_client=model_client or naming.client()
    frontier=[dict(x,leaf_ids=[x['group_id']],child_nodes=[]) for x in leaves]
    nodes={};terminal=[]
    # No quota-based file movement. A heterogeneous frontier may remain wider.
    for level in range(4):
        if cancelled():return False
        eligible=[x for x in frontier if x['group_id'] in vectors]
        terminal.extend(x for x in frontier if x['group_id'] not in vectors)
        if len(eligible)<3:
            terminal.extend(eligible);break
        X=np.vstack([vectors[x['group_id']] for x in eligible])
        k=max(1,int(math.ceil(len(X)/10)))
        centers=_kmeans_fit(X,k,20,seed=23)
        labels=(X@centers.T).argmax(axis=1)
        buckets={}
        for item,lab in zip(eligible,labels):buckets.setdefault(int(lab),[]).append(item)
        accepted={}
        for lab,items in buckets.items():
            center=unit(np.mean([vectors[x['group_id']] for x in items],axis=0))
            support=[float(vectors[x['group_id']]@center) for x in items]
            if len(items)<2 or min(support)<.65:
                terminal.extend(items)
            else:accepted[lab]=items
        if not accepted:break
        names=name_topics(store,package_id,accepted,vectors,round_state,cancelled,model_client) if model_client else {}
        next_level=[]
        for lab,items in sorted(accepted.items()):
            children=sorted(x['group_id'] for x in items)
            leaf_ids=sorted(p for x in items for p in x['leaf_ids'])
            gid='topic:'+hashlib.sha256('|'.join(children).encode()).hexdigest()[:24]
            name=names.get(lab)
            # A title supported by only one child must not pretend to describe the parent.
            supported=sum((naming.name_support(name or '',x['name']+' '+x.get('description','')) or 0)>0 for x in items)
            pending_name=not name
            review=pending_name or supported<math.ceil(len(items)*.6)
            # Lexical absence cannot disprove a semantic parent (synonyms and abstractions).
            # Preserve the validated title and show its review status; never replace it just for wording.
            if pending_name:name='内容分组（%d个子目录）'%len(items)
            scope='；'.join(x['name'] for x in items)[:900]
            node=dict(group_id=gid,node_id=gid,name=name,description=scope,files=sum(x['files'] for x in items),
                      leaf_ids=leaf_ids,children=leaf_ids,child_nodes=children,parent_id=None,depth=0,
                      content_revision=signature,version=1,name_pending=pending_name,status='needs_review' if review else 'current')
            nodes[gid]=node;vectors[gid]=unit(np.mean([vectors[x['group_id']] for x in items],axis=0))
            for item in items:
                if item['group_id'] in nodes:nodes[item['group_id']]['parent_id']=gid
            next_level.append(node)
        if len(next_level)<=12:
            terminal.extend(next_level);break
        frontier=next_level
    else:terminal.extend(frontier)
    for node in nodes.values():
        parent=node['parent_id'];depth=0
        while parent:
            depth+=1;parent=nodes[parent]['parent_id']
        node['depth']=depth
    # Lowest branches remain a partition for legacy counters and invariant checks.
    bottom=[n for n in nodes.values() if not any(k in nodes for k in n['child_nodes'])]
    revisions={x['group_id']:x['content_revision'] for x in leaves}
    payload={'nodes':list(nodes.values()),'revisions':revisions,'versions':{x['group_id']:x['version'] for x in leaves},
             'names':{x['group_id']:x['name'] for x in leaves},'attempted_round':round_state['id'],
             'pending_names':any(n.get('name_pending') for n in nodes.values())}
    with store._lock,closing(store._connect()) as c:
        c.execute('BEGIN IMMEDIATE')
        p=c.execute('SELECT status,control_state FROM packages WHERE id=?',(package_id,)).fetchone()
        if not p or any(v in {'paused','cancelled'} for v in p) or cancelled():c.rollback();return False
        current=[dict(r) for r in c.execute('''SELECT g.id group_id,g.content_revision,l.version,l.name FROM large_catalog_groups g
          JOIN lp_catalog_labels l ON l.package_id=g.package_id AND l.group_id=g.id WHERE g.package_id=? AND g.member_count>0
          AND (l.revision=g.content_revision OR l.locked=1) AND g.id NOT LIKE 'km-other-%' AND g.id NOT LIKE 'km-pending-%' ''',(package_id,))]
        if fingerprint(current)!=signature:c.rollback();return False
        c.execute('DELETE FROM lp_catalog_branches WHERE package_id=?',(package_id,))
        c.executemany('INSERT INTO lp_catalog_branches VALUES(?,?,?,?,?,?,?)',[(package_id,n['node_id'],n['name'],
             json.dumps(n['leaf_ids']),json.dumps({p:revisions[p] for p in n['leaf_ids']}),round_state['id'],time.time()) for n in bottom])
        c.execute('INSERT OR REPLACE INTO lp_catalog_tree VALUES(?,?,?)',(package_id,signature,json.dumps(payload,ensure_ascii=False)))
        c.execute("UPDATE lp_catalog_rounds SET hierarchy_status='completed' WHERE package_id=? AND id=?",(package_id,round_state['id']))
        c.commit()
    store._catalog_cache.pop(package_id,None)
    return True


def visible(c,package_id):
    record=c.execute('SELECT payload FROM lp_catalog_tree WHERE package_id=?',(package_id,)).fetchone()
    if not record:return None
    payload=json.loads(record[0]);nodes=payload['nodes']
    groups={r['id']:dict(r) for r in c.execute('''SELECT g.id,g.content_revision,l.version,l.name,g.member_count files FROM large_catalog_groups g
      LEFT JOIN lp_catalog_labels l ON l.package_id=g.package_id AND l.group_id=g.id WHERE g.package_id=? AND g.member_count>0''',(package_id,))}
    result=[];covered=set();ids={n['node_id'] for n in nodes}
    for node in nodes:
        children=[p for p in node['children'] if p in groups]
        if not children:continue
        stale=len(children)!=len(node['children']) or any(groups[p]['content_revision']!=payload['revisions'].get(p)
              or groups[p]['version']!=payload['versions'].get(p) or groups[p]['name']!=payload.get('names',{}).get(p) for p in children)
        result.append(dict(node,children=children,files=sum(groups[p]['files'] for p in children),status='stale' if stale else node['status']))
        covered.update(children)
    alive={n['node_id'] for n in result}
    for n in result:
        n['child_nodes']=[i for i in n['child_nodes'] if i in alive or i in groups]
    missing=sorted(set(groups)-covered)
    if missing:result.append(dict(node_id='branch:unassigned',name='独立主题与待归并目录',children=missing,child_nodes=missing,
                parent_id=None,depth=0,files=sum(groups[p]['files'] for p in missing),status='pending'))
    return result


def stale(c,package_id):
    record=c.execute('SELECT fingerprint,payload FROM lp_catalog_tree WHERE package_id=?',(package_id,)).fetchone()
    if not record:return None
    leaves=[dict(r) for r in c.execute('''SELECT g.id group_id,g.content_revision,l.version,l.name FROM large_catalog_groups g
      JOIN lp_catalog_labels l ON l.package_id=g.package_id AND l.group_id=g.id WHERE g.package_id=? AND g.member_count>0
      AND (l.revision=g.content_revision OR l.locked=1) AND g.id NOT LIKE 'km-other-%' AND g.id NOT LIKE 'km-pending-%' ''',(package_id,))]
    if record['fingerprint']!=fingerprint(leaves):return True
    payload=json.loads(record['payload'])
    latest=c.execute('SELECT id FROM lp_catalog_rounds WHERE package_id=? ORDER BY started DESC LIMIT 1',(package_id,)).fetchone()
    return bool(payload.get('pending_names') and latest and latest[0]!=payload.get('attempted_round'))
