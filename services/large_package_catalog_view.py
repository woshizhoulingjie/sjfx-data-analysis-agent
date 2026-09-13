"""Stable node identities and read-time model labels for the isolated catalog."""
from contextlib import closing
import json
from services.large_package_catalog_naming import state


def decorate_files(store, package_id, files):
    if not files:return files
    paths=[f['path'] for f in files]
    with closing(store._connect()) as c:
        rows=c.execute('''SELECT m.path,m.group_id,g.content_revision,l.name,l.description,l.revision,l.evidence
          FROM large_catalog_members m JOIN large_catalog_groups g ON g.package_id=m.package_id AND g.id=m.group_id
          LEFT JOIN lp_catalog_labels l ON l.package_id=m.package_id AND l.group_id=m.group_id
          WHERE m.package_id=? AND m.path IN ('''+','.join('?' for _ in paths)+')',[package_id]+paths).fetchall()
    labels={r['path']:dict(r) for r in rows}
    for f in files:
        label=labels.get(f['path'])
        if not label:continue
        f['catalog_node_id']=label['group_id']
        if label.get('name'):
            f['topic']=label['name']
            f['catalog_name']=label['name']
            f['catalog_description']=label['description']
            f['catalog_naming_status']='model' if label['revision']==label['content_revision'] else 'stale'
            f['catalog_group_evidence']=json.loads(label['evidence'])
    return files


def enhance_catalog(store,package_id,result,limit=200,offset=0,query=''):
    with closing(store._connect()) as c:
        total=c.execute('SELECT COUNT(*) FROM large_catalog_groups WHERE package_id=?',(package_id,)).fetchone()[0]
        where='g.package_id=?';args=[package_id]
        if query:
            where+=" AND (COALESCE(l.name,'') LIKE ? OR g.result LIKE ?)";args+=['%'+query+'%']*2
            total=c.execute('SELECT COUNT(*) FROM large_catalog_groups g LEFT JOIN lp_catalog_labels l ON l.package_id=g.package_id AND l.group_id=g.id WHERE '+where,args).fetchone()[0]
        groups=c.execute('''SELECT g.id,g.result,g.content_revision,l.name,l.description,l.version,l.revision,l.evidence,l.needs_review,l.locked,
          g.member_count files
          FROM large_catalog_groups g LEFT JOIN lp_catalog_labels l ON l.package_id=g.package_id AND l.group_id=g.id
          WHERE '''+where+' ORDER BY files DESC,g.id LIMIT ? OFFSET ?',args+[limit,offset]).fetchall()
        topics=[]
        for group in groups:
            fallback=json.loads(group['result'])
            label=group['name'] or fallback.get('topic') or '内容正在归纳'
            samples=c.execute('''SELECT f.path,f.local_summary FROM large_catalog_members m JOIN files f ON f.package_id=m.package_id AND f.path=m.path
              WHERE m.package_id=? AND m.group_id=? ORDER BY m.path LIMIT 3''',(package_id,group['id'])).fetchall()
            examples=[]
            for row in samples:
                info=json.loads(row['local_summary']).get('portrait',{})
                examples.append({'path':row['path'],'basis':info.get('classification_basis',''),
                    'evidence':(info.get('evidence') or info.get('catalog_evidence') or [])[:2]})
            naming='model' if group['name'] and group['revision']==group['content_revision'] else 'stale' if group['name'] else 'extractive'
            if group['locked']:naming='manual'
            topics.append({'node_id':group['id'],'topic':label,'name':label,'description':group['description'] or '',
                           'version':group['version'] or 0,'files':group['files'],'sample':examples,
                           'basis':group['description'] or '根据代表材料提取的临时内容名称',
                           'naming_status':naming,'needs_review':bool(group['needs_review']),
                           'evidence':json.loads(group['evidence']) if group['evidence'] else []})
        unknown_where="f.package_id=? AND NOT EXISTS(SELECT 1 FROM large_catalog_members m WHERE m.package_id=f.package_id AND m.path=f.path)"
        unknown_args=[package_id]
        if query:
            unknown_where+=' AND f.topic LIKE ?';unknown_args.append('%'+query+'%')
        unknown_total=c.execute('SELECT COUNT(*) FROM (SELECT topic FROM files f WHERE '+unknown_where+' GROUP BY topic)',unknown_args).fetchone()[0]
        unknown=[dict(row) for row in c.execute('SELECT MIN(recognition_status) state,topic,COUNT(*) files FROM files f WHERE '+unknown_where+' GROUP BY topic ORDER BY files DESC,topic LIMIT ? OFFSET ?',unknown_args+[max(0,limit-len(groups)),max(0,offset-total)])]
        labels={'needs_ocr':'等待文字识别','metadata_only':'尚无可读内容','needs_review':'内容线索待归组','needs_more':'需要补充内容',
                'unsupported':'暂不支持的格式','restricted':'受限文件','legacy':'等待建立内容画像','identified':'内容线索待归组'}
        for group in unknown:
            topics.append({'node_id':'preview:'+group['topic'].encode('utf8').hex(),'topic':group['topic'] or labels.get(group['state'],'等待内容识别'),
                           'files':group['files'],'basis':'已有内容线索，尚未完成语义归组','sample':[], 'naming_status':'unread'})
        from services.large_package_catalog_hierarchy import visible
        branches=visible(c,package_id)
        content=c.execute("SELECT COUNT(*) FROM files WHERE package_id=? AND length(COALESCE(json_extract(local_summary,'$.sample'),''))>=20",(package_id,)).fetchone()[0]
        attempted=c.execute("SELECT COUNT(*) FROM files WHERE package_id=? AND quick_status IN ('completed','failed')",(package_id,)).fetchone()[0]
    inventory=(store.get(package_id) or {}).get('counts') or {}
    result['inventory']={key:inventory.get(key) for key in ('inventory_complete','inventory_error_count','inventory_errors')}
    result['smart'].update(topics=topics,branches=branches,topic_total=total+unknown_total,topics_truncated=offset+len(topics)<total+unknown_total,
                           naming=state(store,package_id),node_offset=offset,node_limit=limit,
                           content_files=content,attempted_files=attempted)
    return result
