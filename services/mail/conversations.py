"""Deterministic exact conversations and explicitly separate subject candidates."""
from __future__ import annotations
import collections
import hashlib
import json
from datetime import datetime
from config import Config


def stable_id(*parts):
    return hashlib.sha256('\0'.join(map(str,parts)).encode()).hexdigest()[:32]


def rebuild(repository,package_id,owner_id):
    if not repository.get_package(package_id,owner_id):raise ValueError('邮件资料包不存在')
    if not Config.MAIL_ENABLE_CONVERSATIONS:return
    with repository._connection() as c:
        messages=[dict(r) for r in c.execute('SELECT DISTINCT m.* FROM mail_messages m JOIN mail_occurrences o ON o.message_key=m.message_key WHERE o.package_id=?',(package_id,))]
        edges=[dict(r) for r in c.execute("SELECT * FROM mail_thread_edges WHERE package_id=? AND status='exact'",(package_id,))]
        fingerprint=stable_id(sorted((m['message_key'],m['subject_normalized'],m['sent_at_utc']) for m in messages),sorted((e['source_message_key'],e['target_message_key'],e['relation_type']) for e in edges))
        old=c.execute("SELECT fingerprint FROM mail_projection_state WHERE package_id=? AND stage='conversations'",(package_id,)).fetchone()
        if old and old[0]==fingerprint:return
        c.execute('DELETE FROM mail_conversation_members WHERE conversation_id IN (SELECT conversation_id FROM mail_conversations WHERE package_id=?)',(package_id,))
        c.execute('DELETE FROM mail_conversations WHERE package_id=?',(package_id,))
        c.execute("DELETE FROM mail_thread_edges WHERE package_id=? AND status='candidate'",(package_id,))
        by_id={m['message_key']:m for m in messages};parents={k:k for k in by_id}
        def find(k):
            while parents[k]!=k:parents[k]=parents[parents[k]];k=parents[k]
            return k
        children=collections.defaultdict(set);has_parent=set()
        for edge in edges:
            a,b=edge['source_message_key'],edge['target_message_key']
            if a not in parents or b not in parents or a==b:continue
            parents[find(a)]=find(b);children[b].add(a);has_parent.add(a)
        groups=collections.defaultdict(list)
        for k in by_id:groups[find(k)].append(k)
        for members in groups.values():
            roots=sorted((k for k in members if k not in has_parent),key=lambda k:by_id[k]['sent_at_utc'] or '')
            root=(roots or sorted(members,key=lambda k:by_id[k]['sent_at_utc'] or ''))[0]
            cid=stable_id(package_id,'conversation',sorted(members));depths={root:0};queue=collections.deque([root])
            while queue:
                k=queue.popleft()
                for child in children[k]:
                    if child not in depths:depths[child]=depths[k]+1;queue.append(child)
            participant_ids=set();attached=False
            for k in members:
                participant_ids.update(r[0] for r in c.execute('SELECT address_id FROM mail_message_participants WHERE message_key=?',(k,)))
                attached=attached or bool(c.execute('SELECT 1 FROM mail_parts WHERE message_key=? AND is_explicit_attachment=1 LIMIT 1',(k,)).fetchone())
            dates=sorted(by_id[k]['sent_at_utc'] for k in members if by_id[k]['sent_at_utc'])
            language=collections.Counter(by_id[k]['body_language'] for k in members).most_common(1)[0][0]
            c.execute('INSERT INTO mail_conversations VALUES(?,?,?,?,?,?,?,?,?,?,?)',(cid,package_id,root,by_id[root]['subject_normalized'],dates[0] if dates else None,dates[-1] if dates else None,len(members),len(participant_ids),int(attached),language,'exact_chain' if len(members)>1 else 'singleton'))
            c.executemany('INSERT INTO mail_conversation_members VALUES(?,?,?,?)',[(cid,k,depths.get(k,0),int(k==root)) for k in members])
        subjects=collections.defaultdict(list)
        for m in messages:
            if m['subject_normalized'] and m['sent_at_utc']:subjects[m['subject_normalized']].append(m)
        candidate_pairs=set()
        for subject,rows in subjects.items():
            rows.sort(key=lambda r:r['sent_at_utc'])
            # Adjacent same-subject messages form bounded candidate links, never exact unions.
            for a,b in zip(rows,rows[1:]):
                pair=tuple(sorted([find(a['message_key']),find(b['message_key'])]))
                if pair[0]==pair[1] or pair in candidate_pairs:continue
                try:days=(datetime.fromisoformat(b['sent_at_utc'])-datetime.fromisoformat(a['sent_at_utc'])).total_seconds()/86400
                except ValueError:continue
                if 0<=days<=Config.MAIL_SUBJECT_CANDIDATE_WINDOW_DAYS:
                    candidate_pairs.add(pair)
                    c.execute('INSERT INTO mail_thread_edges(edge_id,package_id,source_message_key,target_message_key,relation_type,source_header,status,evidence_json) VALUES(?,?,?,?,?,?,?,?)',(stable_id(package_id,'subject',a['message_key'],b['message_key']),package_id,b['message_key'],a['message_key'],'subject','subject','candidate',json.dumps({'subject_normalized':subject,'days_apart':days})))
        c.execute("INSERT OR REPLACE INTO mail_projection_state VALUES(?,'conversations',?)",(package_id,fingerprint))


def list_conversations(repository,package_id,owner_id):
    if not repository.get_package(package_id,owner_id):raise ValueError('邮件资料包不存在')
    if not Config.MAIL_ENABLE_CONVERSATIONS:return []
    rebuild(repository,package_id,owner_id)
    with repository._connection() as c:return [dict(r) for r in c.execute('SELECT * FROM mail_conversations WHERE package_id=? ORDER BY last_at DESC',(package_id,))]


def detail(repository,conversation_id,owner_id):
    if not Config.MAIL_ENABLE_CONVERSATIONS:return None
    with repository._connection() as c:
        row=c.execute('SELECT v.* FROM mail_conversations v JOIN mail_packages p ON p.package_id=v.package_id WHERE v.conversation_id=? AND p.owner_id=?',(conversation_id,owner_id)).fetchone()
        if not row:return None
        result=dict(row);result['messages']=[dict(r) for r in c.execute('SELECT m.message_key,m.subject_raw,m.sent_at_utc,v.depth,v.is_root FROM mail_conversation_members v JOIN mail_messages m ON m.message_key=v.message_key WHERE v.conversation_id=? ORDER BY m.sent_at_utc',(conversation_id,))]
    keys={m['message_key'] for m in result['messages']}
    result['edges']=[e for e in repository.threads(result['package_id'],owner_id) if e['source_message_key'] in keys or e.get('target_message_key') in keys]
    return result
