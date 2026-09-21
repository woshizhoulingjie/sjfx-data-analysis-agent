"""Stable node identities and read-time model labels for the isolated catalog."""
from contextlib import closing
import json
import os
from services.large_package_catalog_naming import state


CATALOG_STATE_SQL = """CASE
 WHEN f.recognition_status='identified'
   AND json_extract(f.local_summary,'$.portrait.catalog_status')='classified'
 THEN 'classified'
 WHEN EXISTS (SELECT 1 FROM large_catalog_members cm
   JOIN large_catalog_groups cg ON cg.package_id=cm.package_id AND cg.id=cm.group_id
   JOIN lp_catalog_labels cl ON cl.package_id=cg.package_id AND cl.group_id=cg.id
   WHERE cm.package_id=f.package_id AND cm.path=f.path
     AND cl.name<>'' AND (cl.revision=cg.content_revision OR cl.locked=1)
     AND cm.group_id NOT LIKE 'km-other-%'
     AND cm.input_hash=json_extract(f.local_summary,'$.portrait.catalog_input_hash'))
 THEN 'classified'
 WHEN EXISTS (SELECT 1 FROM large_catalog_members cm JOIN large_catalog_groups cg
   ON cg.package_id=cm.package_id AND cg.id=cm.group_id
   WHERE cm.package_id=f.package_id AND cm.path=f.path
     AND cm.group_id NOT LIKE 'km-other-%' AND cm.group_id NOT LIKE 'km-pending-%'
     AND cm.input_hash=json_extract(f.local_summary,'$.portrait.catalog_input_hash'))
 THEN 'grouped'
 ELSE COALESCE(json_extract(f.local_summary,'$.portrait.catalog_status'),'legacy') END"""


def decorate_files(store, package_id, files):
    if not files:return files
    paths=[f['path'] for f in files]
    with closing(store._connect()) as c:
        rows=c.execute('''SELECT m.path,m.group_id,g.content_revision,l.name,l.description,l.revision,l.evidence,l.locked
          FROM large_catalog_members m JOIN large_catalog_groups g ON g.package_id=m.package_id AND g.id=m.group_id
          LEFT JOIN lp_catalog_labels l ON l.package_id=m.package_id AND l.group_id=m.group_id
          WHERE m.package_id=? AND m.path IN ('''+','.join('?' for _ in paths)+')',[package_id]+paths).fetchall()
        derived = dict(c.execute('SELECT f.path,' + CATALOG_STATE_SQL +
                                ' FROM files f WHERE f.package_id=? AND f.path IN (' +
                                ','.join('?' for _ in paths) + ')', [package_id] + paths))
    labels={r['path']:dict(r) for r in rows}
    for f in files:
        f['catalog_state'] = derived.get(f['path'], 'legacy')
        label=labels.get(f['path'])
        if not label:continue
        f['catalog_node_id']=label['group_id']
        if label.get('name'):
            f['topic']=label['name']
            f['catalog_name']=label['name']
            f['catalog_description']=label['description']
            f['catalog_naming_status']='manual' if label['locked'] else 'model' if label['revision']==label['content_revision'] else 'stale'
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
          (SELECT COUNT(*) FROM large_catalog_members m JOIN files f
           ON f.package_id=m.package_id AND f.path=m.path
           WHERE m.package_id=g.package_id AND m.group_id=g.id) files
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
        catalog_states = dict(c.execute('SELECT ' + CATALOG_STATE_SQL +
                                       ',COUNT(*) FROM files f WHERE f.package_id=? GROUP BY 1', (package_id,)))
    inventory=(store.get(package_id) or {}).get('counts') or {}
    result['inventory']={key:inventory.get(key) for key in ('inventory_complete','inventory_error_count','inventory_errors')}
    result['smart'].update(topics=topics,branches=branches,topic_total=total+unknown_total,
                           catalog_states=catalog_states,
                           theme_total=sum(b.get('status') != 'pending' for b in branches),
                           leaf_total=total, hierarchy='theme_leaf_files',
                           topics_truncated=offset+len(topics)<total+unknown_total,
                           naming=state(store,package_id),node_offset=offset,node_limit=limit,
                           content_files=content,attempted_files=attempted)
    return result


def catalog_tree(store, package_id):
    """轻量智能目录骨架：主题 → 叶组，供导出中心的文件选择树使用。

    为什么不复用 `enhance_catalog`：那个读取口服务于目录**浏览**，每个叶组
    都带描述、代表性证据和样本文件，实测 177 个叶组约 2.2 MB。导出树只需要
    身份、名称和数量，所以这里只读这三列。层级仍由 `visible` 统一给出，
    因此树上的节点编号与 `export_selection` 认可的集合完全一致——
    前端因此可以放心地把勾选的节点直接回传为 node_ids。
    """
    package = store.get(package_id) or {}
    with closing(store._connect()) as c:
        groups = {}
        for row in c.execute(
            "SELECT g.id, l.name,"
            " (SELECT COUNT(*) FROM large_catalog_members m JOIN files f"
            "  ON f.package_id=m.package_id AND f.path=m.path"
            "  WHERE m.package_id=g.package_id AND m.group_id=g.id) AS files"
            " FROM large_catalog_groups g LEFT JOIN lp_catalog_labels l"
            " ON l.package_id=g.package_id AND l.group_id=g.id"
            " WHERE g.package_id=?", (package_id,)
        ):
            groups[str(row['id'])] = {
                'node_id': str(row['id']),
                'name': row['name'] or '内容正在归纳',
                'files': int(row['files'] or 0),
            }
        from services.large_package_catalog_hierarchy import visible
        branches = visible(c, package_id)
        # **不在任何"真实叶组"里的文件才算未归组。**
        #
        # 原来只统计"没有任何成员行"的文件，于是 `km-other-` / `km-pending-`
        # 系统桶里的文件**既不出现在主题树里、也不计入这个提示** —— 目录看起来
        # 比实际更完整。这不是理论问题：实测 4 个包全部漏报 ——
        #   lp-605781 提示 0，实际有 58 个文件不在任何主题里；
        #   lp-9b8edcc6 提示 296，实际 361；lp-702f7a 提示 0，实际 20；
        #   lp-b09821 提示 0，实际 4。
        # 口径与主题树保持一致：树只列真实叶组，这里也只把真实叶组算作"已归组"。
        ungrouped = c.execute(
            "SELECT COUNT(*) FROM files f WHERE f.package_id=?"
            " AND NOT EXISTS(SELECT 1 FROM large_catalog_members m"
            " WHERE m.package_id=f.package_id AND m.path=f.path"
            "   AND m.group_id NOT LIKE 'km-other-%'"
            "   AND m.group_id NOT LIKE 'km-pending-%')", (package_id,)
        ).fetchone()[0]
    themes = []
    for branch in branches:
        leaves = [groups[gid] for gid in (branch.get('children') or []) if gid in groups]
        if not leaves:
            continue
        themes.append({
            'node_id': branch.get('node_id'),
            'name': branch.get('name') or '未命名主题',
            'files': sum(leaf['files'] for leaf in leaves),
            'status': branch.get('status') or 'current',
            'leaves': leaves,
        })
    try:
        counts = store.counts(package_id) or {}
    except Exception:
        counts = {}
    return {
        'kind': 'large_package',
        'package_id': package_id,
        # packages 表没有 name 列；历史上展示名一律取原始目录名（见 list_history），
        # 这里沿用同一口径，避免同一个包在两个页面显示成不同的名字。
        'name': os.path.basename(str(package.get('root_path') or '').rstrip('/')) or package_id,
        'file_count': int(counts.get('total_files') or 0),
        'theme_total': len(themes),
        'leaf_total': sum(len(item['leaves']) for item in themes),
        'ungrouped_files': int(ungrouped or 0),
        'themes': themes,
    }
