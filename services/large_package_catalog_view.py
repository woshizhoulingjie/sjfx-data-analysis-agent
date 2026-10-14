"""Stable node identities and read-time model labels for the isolated catalog."""
from contextlib import closing
import json
import os
from services.large_package_catalog_naming import state
from services.large_package_catalog_review import (leaf_review_facts, summarize_reviews,
                                                   provisional_label, provisional_label_sql)
# 批1 第 28 条：未归类节点的名字只有一处定义（hierarchy），这里复用。
# 改动前 view.py 自己写了第三个名字 '内容正在归纳'，与 tree.py 的
# '独立主题与待归并目录'、hierarchy.py 的 '待归并主题' 指向同一个 node_id。
from services.large_package_catalog_hierarchy import UNASSIGNED_BRANCH_NAME as UNASSIGNED_FALLBACK_NAME


CATALOG_STATE_SQL = """CASE
 WHEN json_extract(f.local_summary,'$.portrait.catalog_model')='extractive-semantic-label/1'
   AND EXISTS (SELECT 1 FROM large_catalog_members cm
     JOIN large_catalog_groups cg ON cg.package_id=cm.package_id AND cg.id=cm.group_id
     WHERE cm.package_id=f.package_id AND cm.path=f.path
       AND (cm.input_hash<>json_extract(f.local_summary,'$.portrait.catalog_input_hash')
            OR cg.status<>'named'))
 THEN 'pending'
 WHEN json_extract(f.local_summary,'$.portrait.catalog_model')='extractive-semantic-label/1'
   AND json_extract(f.local_summary,'$.portrait.catalog_status')='classified'
   AND EXISTS (SELECT 1 FROM large_catalog_members cm
     JOIN large_catalog_groups cg ON cg.package_id=cm.package_id AND cg.id=cm.group_id
     WHERE cm.package_id=f.package_id AND cm.path=f.path
       AND cm.group_id NOT LIKE 'km-other-%' AND cm.group_id NOT LIKE 'km-pending-%'
       AND cm.input_hash=json_extract(f.local_summary,'$.portrait.catalog_input_hash')
       AND cg.status='named'
       AND json_extract(cg.result,'$.model')='extractive-semantic-label/1'
       AND COALESCE(json_extract(cg.result,'$.topic'),'')<>''
       AND json_array_length(cg.result,'$.quotes')>0
       AND NOT EXISTS (SELECT 1 FROM lp_catalog_labels cl
         WHERE cl.package_id=cg.package_id AND cl.group_id=cg.id))
 THEN 'classified'
 WHEN EXISTS (SELECT 1 FROM large_catalog_members cm
   JOIN large_catalog_groups cg ON cg.package_id=cm.package_id AND cg.id=cm.group_id
   LEFT JOIN lp_catalog_labels cl ON cl.package_id=cg.package_id AND cl.group_id=cg.id
   WHERE cm.package_id=f.package_id AND cm.path=f.path
     AND cm.group_id NOT LIKE 'km-other-%' AND cm.group_id NOT LIKE 'km-pending-%'
     AND cm.input_hash=json_extract(f.local_summary,'$.portrait.catalog_input_hash')
     AND (cl.name IS NULL OR cl.name='' OR
          (cl.revision<>cg.content_revision AND COALESCE(cl.locked,0)=0) OR
          (""" + provisional_label_sql() + """)))
 THEN 'grouped'
 WHEN f.recognition_status='identified'
   AND json_extract(f.local_summary,'$.portrait.catalog_status')='classified'
 THEN 'classified'
 WHEN EXISTS (SELECT 1 FROM large_catalog_members cm
   JOIN large_catalog_groups cg ON cg.package_id=cm.package_id AND cg.id=cm.group_id
   JOIN lp_catalog_labels cl ON cl.package_id=cg.package_id AND cl.group_id=cg.id
   WHERE cm.package_id=f.package_id AND cm.path=f.path
     AND cl.name<>'' AND (cl.revision=cg.content_revision OR cl.locked=1)
     AND cm.group_id NOT LIKE 'km-other-%'
     AND cm.group_id NOT LIKE 'km-pending-%'
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
        rows=c.execute('''SELECT m.path,m.group_id,g.result,g.content_revision,l.name,l.model,l.description,l.revision,l.evidence,l.locked,l.needs_review
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
        f['catalog_naming_status']=_naming_status(label)
        facts = leaf_review_facts(label['result'], label['needs_review'],
                                  model=label['model'], content_revision=label['content_revision'])
        f.update({'catalog_' + key: value for key, value in facts.items()})
        if label.get('name'):
            f['topic']=_display_name(label['group_id'], label['name'], label['result'])
            f['catalog_name']=f['topic']
            f['catalog_description']=label['description']
            f['catalog_naming_status']=_naming_status(label)
            f['catalog_group_evidence']=json.loads(label['evidence'])
    return files


def _json_object(value):
    """把 `local_summary` / `result` 这类"可能是空串、也可能是坏 JSON"的列读成 dict。

    批1 第 32 条顺手补上的健壮性：改动前 `json.loads(group['result'])` 直接抛
    ValueError 就会让**整个目录页 500**（`result` 列在旧行上可以是空串）。
    """
    if isinstance(value,dict):return value
    try:
        data=json.loads(value or '{}')
    except (ValueError,TypeError):
        return {}
    return data if isinstance(data,dict) else {}


def _display_name(group_id, name, result):
    fallback = _json_object(result)
    if str(group_id).startswith('km-other-') and fallback.get('hold_reason') == 'outliers':
        return '离群内容待归并'
    return name or fallback.get('topic') or UNASSIGNED_FALLBACK_NAME


def _naming_status(row):
    if provisional_label(row):
        return 'pending'
    if row['locked']:
        return 'manual'
    if not row['name']:
        return 'extractive'
    if row['revision'] != row['content_revision']:
        return 'stale'
    if str(row['model'] or '') in {'deterministic', 'fallback-naming', 'extractive-semantic-label/1'}:
        return 'extractive'
    return 'model'


def _presentation_branches(branches, groups):
    """Add disjoint display sections without changing branch selection scopes."""
    by_id = {row['node_id']: row for row in groups}
    output = []
    for raw in branches:
        branch = dict(raw)
        if branch.get('node_id') == 'branch:unassigned':
            independent = set(branch.get('independent_leaf_ids', []))
            missing = independent | set(branch.get('semantic_pending_leaf_ids', []))
            review = {gid for gid in missing if by_id.get(gid, {}).get('needs_review')}
            unnamed = {gid for gid in missing - review
                       if by_id.get(gid, {}).get('naming_status') in {'pending', 'extractive', 'stale'}}
            sections = {'display_independent': independent - review - unnamed,
                        'pending_name_independent': unnamed,
                        'review_independent': review,
                        'display_semantic_pending': missing - independent - review - unnamed}
            for key, ids in sections.items():
                branch[key + '_leaf_ids'] = sorted(ids)
                branch[key + '_files'] = sum(by_id.get(gid, {}).get('files', 0) for gid in ids)
        output.append(branch)
    return output


def _presentation_stats(stats, branches):
    buckets = [b for b in branches if b.get('node_id') == 'branch:unassigned']
    for target, source in (('display_pending_theme_files', 'display_semantic_pending_files'),
                           ('display_independent_files', 'display_independent_files'),
                           ('pending_name_independent_files', 'pending_name_independent_files'),
                           ('review_independent_files', 'review_independent_files')):
        stats[target] = sum(b.get(source, 0) for b in buckets)


def _pending_reason(package):
    """A5：把"这份目录还没跑完"如实告诉界面，避免用户以为是命名失败。

    为什么必须有它：`cancelled`/`paused` 的包会留下半成品目录（实测 4 个包共
    26,054 个文件），没有名字的组会显示成"内容线索：<原始片段>"，看起来像命名
    出了错，实际是任务被取消/暂停。只读，不写任何东西。
    """
    status=str((package or {}).get('status') or '')
    phase=str((package or {}).get('phase') or '')
    if status=='cancelled':
        return {'code':'cancelled','status':status,'phase':phase,
                'message':'该包已取消：目录是取消时的中间结果，部分条目尚未命名'}
    if status=='paused':
        return {'code':'paused','status':status,'phase':phase,
                'message':'该包已暂停：目录尚未跑完，恢复后会继续命名'}
    return {}


def catalog_stats(c, package_id, branches, quality_out=None):
    """Package-wide counts use live file identities, never cached member_count."""
    if not c.in_transaction:
        c.execute('BEGIN')
    rows = [dict(row) for row in c.execute(
        "SELECT g.id,g.result,l.name,l.model,l.needs_review,l.revision,l.locked,g.content_revision,"
        " (SELECT COUNT(*) FROM large_catalog_members m JOIN files f"
        " ON f.package_id=m.package_id AND f.path=m.path"
        " WHERE m.package_id=g.package_id AND m.group_id=g.id) files"
        " FROM large_catalog_groups g LEFT JOIN lp_catalog_labels l"
        " ON l.package_id=g.package_id AND l.group_id=g.id WHERE g.package_id=?", (package_id,))]
    live = {str(row['id']): row for row in rows if row['files']}
    semantic = {gid: row for gid, row in live.items()
                if not gid.startswith(('km-other-', 'km-pending-'))}
    named = {gid: row for gid, row in semantic.items() if row['name']
             and (row['locked'] or row['revision'] == row['content_revision'])
             and not provisional_label(row)}
    temporary = {gid: row for gid, row in named.items() if _naming_status(row) == 'extractive'}
    themes = [b for b in branches if b.get('kind', 'theme') == 'theme'
              and b.get('node_id') != 'branch:unassigned']
    themed = {gid for b in themes for gid in b.get('children', [])}
    independent = {gid for b in branches for gid in b.get('independent_leaf_ids', [])}
    independent &= set(semantic) - themed
    pending = set(semantic) - themed - independent
    members = {}
    for gid, path in c.execute('SELECT m.group_id,m.path FROM large_catalog_members m JOIN files f '
                              'ON f.package_id=m.package_id AND f.path=m.path WHERE m.package_id=?',
                              (package_id,)):
        members.setdefault(str(gid), set()).add(path)
    def files(gids):
        return len(set().union(*(members.get(gid, set()) for gid in set(gids))))
    all_paths = {row[0] for row in c.execute('SELECT path FROM files WHERE package_id=?', (package_id,))}
    partitions = {}
    assigned = set()
    for key, gids in (('merged_theme', themed), ('independent', independent),
                      ('pending_theme', pending),
                      ('system_bucket', {gid for gid in live if gid.startswith('km-other-')}),
                      ('staging', {gid for gid in live if gid.startswith('km-pending-')})):
        paths = set().union(*(members.get(gid, set()) for gid in gids)) - assigned
        partitions[key] = len(paths)
        assigned.update(paths)
    failed_paths = {row[0] for row in c.execute("SELECT path FROM files WHERE package_id=? AND quick_status='failed'", (package_id,))}
    partitions['parse_failed'] = len(failed_paths - assigned)
    partitions['ungrouped'] = len(all_paths - assigned - failed_paths)
    quick = dict(c.execute('SELECT quick_status,COUNT(*) FROM files WHERE package_id=? GROUP BY quick_status',
                           (package_id,)))
    total_files = sum(quick.values())
    member_files = files(live)
    grouped_files = files(semantic)
    content = c.execute("SELECT COUNT(*) FROM files WHERE package_id=? AND "
                        "length(COALESCE(json_extract(local_summary,'$.sample'),''))>=20",
                        (package_id,)).fetchone()[0]
    reasons = [dict(row) for row in c.execute(
        "SELECT COALESCE(NULLIF(error,''),'未记录解析错误详情') reason,COUNT(*) files"
        " FROM files WHERE package_id=? AND quick_status='failed' GROUP BY 1 ORDER BY files DESC,reason",
        (package_id,))]
    quality = summarize_reviews(rows, members, branches)
    if quality_out is not None:
        quality_out.update(quality)
    return {'total_files': total_files, 'all_group_nodes': len(live), 'leaf_nodes': len(semantic),
            'theme_nodes': sum(b.get('status') != 'pending' for b in themes),
            'root_theme_nodes': sum(not b.get('parent_id') and b.get('status') != 'pending' for b in themes),
            'grouped_files': grouped_files, 'member_files': member_files, 'ungrouped_files': total_files - member_files,
            'merged_theme_files': partitions['merged_theme'], 'independent_files': partitions['independent'],
            'pending_theme_files': partitions['pending_theme'],
            'system_bucket_files': partitions['system_bucket'],
            'staging_files': partitions['staging'], 'file_partitions': partitions,
            'named_leaf_nodes': len(named), 'named_files': files(named),
            'pending_naming_leaf_nodes': len(semantic) - len(named),
            'pending_naming_files': files(set(semantic) - set(named)),
            'temporary_leaf_nodes': len(temporary), 'temporary_named_files': files(temporary),
            **{key: quality[key] for key in ('review_leaf_nodes', 'review_leaf_files',
              'review_theme_nodes', 'review_root_theme_nodes', 'review_theme_files',
              'review_files', 'review_reason_counts')},
            'quality_revision': quality['revision'],
            'model_named_leaf_nodes': sum(_naming_status(row) == 'model' for row in named.values()),
            'quick_failed_files': quick.get('failed', 0), 'quick_completed_files': quick.get('completed', 0),
            'quick_pending_files': total_files - quick.get('failed', 0) - quick.get('completed', 0),
            'quick_failed_reasons': reasons, 'content_files': int(content or 0)}


def _publication_metadata(c, package_id):
    row = c.execute('SELECT payload FROM lp_catalog_tree WHERE package_id=?', (package_id,)).fetchone()
    payload = _json_object(row[0]) if row else {}
    if not payload.get('publication_schema'):
        return {}
    from services.large_package_catalog_tree import publication_state
    state = publication_state(c, package_id, payload)
    represented = set(state['covered_leaf_ids']) | set(state['independent_leaf_ids'])
    scope = represented | set(state['missing_leaf_ids'])
    live_members = list(c.execute('SELECT m.group_id,m.path FROM large_catalog_members m '
                                           'JOIN files f ON f.package_id=m.package_id AND f.path=m.path '
                                           'WHERE m.package_id=?', (package_id,)))
    paths = {path for gid, path in live_members if gid in represented}
    scope_paths = {path for gid, path in live_members if gid in scope}
    return {'version': payload.get('catalog_version'), 'snapshot_id': payload.get('snapshot_id'),
            'state': 'complete' if state['complete'] else 'partial',
            'covered_files': len(paths), 'scope_files': len(scope_paths),
            'covered_leaf_groups': len(represented),
            'uncovered_leaf_groups': len(state['missing_leaf_ids']),
            'current_snapshot_id': state['current_snapshot_id']}


def enhance_catalog(store,package_id,result,limit=200,offset=0,query=''):
    # 批1 第 32 条：每个叶组的样本数。改动前对每个 group 单独发一条
    # `SELECT ... LIMIT 3`（默认 limit=200 → 200 次额外查询），这里改成
    # 一次窗口函数查询（见下），行数上限由本常量决定。
    sample_limit=max(0,int(os.getenv('SJFX_LP_CATALOG_SAMPLE_ROWS','3') or 3))
    # 批1 第 32 条：`sample[].evidence` 默认**不再随目录页下发**。
    # 改动前注释自述"177 个叶组约 2.2 MB"，而前端节点卡片只渲染
    # `item.path` / `item.basis`（`static/large-package-result.js:286`），
    # evidence 是白送的体积。需要它的调用方显式设
    # `SJFX_LP_CATALOG_SAMPLE_EVIDENCE=1`。
    sample_evidence=str(os.getenv('SJFX_LP_CATALOG_SAMPLE_EVIDENCE','0') or '0').strip().lower() in {'1','true','yes','on'}
    with closing(store._connect()) as c:
        c.execute('BEGIN')
        where=('g.package_id=? AND EXISTS(SELECT 1 FROM large_catalog_members m JOIN files f '
               'ON f.package_id=m.package_id AND f.path=m.path WHERE m.package_id=g.package_id AND m.group_id=g.id)')
        args=[package_id]
        total=c.execute('SELECT COUNT(*) FROM large_catalog_groups g WHERE '+where,args).fetchone()[0]
        if query:
            where+=" AND (COALESCE(l.name,'') LIKE ? OR g.result LIKE ?)";args+=['%'+query+'%']*2
            total=c.execute('SELECT COUNT(*) FROM large_catalog_groups g LEFT JOIN lp_catalog_labels l ON l.package_id=g.package_id AND l.group_id=g.id WHERE '+where,args).fetchone()[0]
        groups=c.execute('''SELECT g.id,g.result,g.content_revision,l.name,l.model,l.description,l.version,l.revision,l.evidence,l.needs_review,l.locked,
          (SELECT COUNT(*) FROM large_catalog_members m JOIN files f
           ON f.package_id=m.package_id AND f.path=m.path
           WHERE m.package_id=g.package_id AND m.group_id=g.id) files
          FROM large_catalog_groups g LEFT JOIN lp_catalog_labels l ON l.package_id=g.package_id AND l.group_id=g.id
          WHERE '''+where+' ORDER BY files DESC,g.id LIMIT ? OFFSET ?',args+[limit,offset]).fetchall()
        # 批1 第 32 条：**一次**取回本页所有叶组的代表样本（原来每组一条查询）。
        # 窗口函数按 group 分区、每条路径只留前 `sample_limit` 行；行数上限是
        # `len(groups) × sample_limit`，不再随叶组数线性放大查询次数。
        samples={}
        if groups and sample_limit:
            marks=','.join('?' for _ in groups)
            for row in c.execute(
                    "SELECT group_id,path,local_summary FROM ("
                    " SELECT m.group_id AS group_id,f.path AS path,f.local_summary AS local_summary,"
                    "  ROW_NUMBER() OVER (PARTITION BY m.group_id ORDER BY m.path) AS rn"
                    " FROM large_catalog_members m JOIN files f ON f.package_id=m.package_id AND f.path=m.path"
                    " WHERE m.package_id=? AND m.group_id IN ("+marks+")) WHERE rn<=?",
                    [package_id]+[g['id'] for g in groups]+[sample_limit]):
                samples.setdefault(str(row['group_id']),[]).append(row)
        topics=[]
        for group in groups:
            fallback=_json_object(group['result'])
            review = leaf_review_facts(fallback, group['needs_review'],
                                       model=group['model'], content_revision=group['content_revision'])
            label=_display_name(group['id'], group['name'], group['result'])
            examples=[]
            for row in samples.get(str(group['id']),[]):
                info=_json_object(row['local_summary']).get('portrait',{})
                item={'path':row['path'],'basis':info.get('classification_basis','')}
                if sample_evidence:
                    item['evidence']=(info.get('evidence') or info.get('catalog_evidence') or [])[:2]
                examples.append(item)
            naming=_naming_status(group)
            topics.append({'node_id':group['id'],'topic':label,'name':label,'description':group['description'] or '',
                           'version':group['version'] or 0,'files':group['files'],'sample':examples,
                           'basis':group['description'] or '根据代表材料提取的临时内容名称',
                           'naming_status':naming, **review,
                           'kind':'system' if group['id'].startswith('km-other-') else 'staging' if group['id'].startswith('km-pending-') else 'leaf',
                           'holding_reason':fallback.get('hold_reason', ''),
                           'evidence':json.loads(group['evidence']) if group['evidence'] else []})
        unknown_where="f.package_id=? AND NOT EXISTS(SELECT 1 FROM large_catalog_members m WHERE m.package_id=f.package_id AND m.path=f.path)"
        unknown_args=[package_id]
        if query:
            unknown_where+=' AND f.topic LIKE ?';unknown_args.append('%'+query+'%')
        unknown_total=c.execute('SELECT COUNT(*) FROM (SELECT topic FROM files f WHERE '+unknown_where+' GROUP BY topic)',unknown_args).fetchone()[0]
        # 批1 第 31 条：**两段流的游标必须显式对齐，不能靠"减去上一段的长度"近似。**
        #
        # 改动前是 `LIMIT max(0,limit-len(groups)) OFFSET max(0,offset-total)`：
        # `offset` 被当成"叶组 + unknown"的全局游标，而叶组那段已经被
        # `LIMIT ? OFFSET ?` 用掉了一部分 —— 于是 unknown 的起点既可能与上一页的
        # 结束位置重叠（同一话题重复出现），也可能跳过一段（空白页）。
        # 实测口径：25 个叶组 + 30 个未归组话题、limit=10 翻到第 5 页时，
        # 节点会重复出现。
        #
        # 现在把游标语义写死成一句：**先叶组、后 unknown**。
        #   · `offset < total`  → 本页游标还在叶组段里，unknown 一个都不取；
        #   · 叶组取完（`groups` 不满 limit）→ 从 unknown 段的**第 0 个**接着取，
        #     取 `limit-len(groups)` 个。这样"分组页 + 未归组页"首尾相接、不重不漏。
        # 前端 `topics_truncated`（= 本页未取满）语义不变。
        unknown_tail=max(0,limit-len(groups))
        unknown_offset=0 if offset<total else max(0,offset-total)
        unknown=[dict(row) for row in c.execute(
            "SELECT MIN(recognition_status) state,topic,COUNT(*) files,"
            " SUM(quick_status='failed') quick_failed_files,"
            " MIN(CASE WHEN quick_status='failed' THEN NULLIF(error,'') END) error"
            " FROM files f WHERE "+unknown_where+' GROUP BY topic ORDER BY files DESC,topic LIMIT ? OFFSET ?',
            unknown_args+[unknown_tail,unknown_offset])]
        labels={'needs_ocr':'等待文字识别','metadata_only':'尚无可读内容','needs_review':'内容线索待归组','needs_more':'需要补充内容',
                'unsupported':'暂不支持的格式','restricted':'受限文件','legacy':'等待建立内容画像','identified':'内容线索待归组'}
        for group in unknown:
            failed = int(group['quick_failed_files'] or 0)
            all_failed = failed == group['files']
            error = group['error'] or ('未记录解析错误详情' if failed else '')
            label = '内容预览失败' if all_failed else group['topic'] or labels.get(group['state'],'等待内容识别')
            basis = ('快速解析失败：'+error if all_failed else
                     '有 %d 个文件快速解析失败：%s' % (failed, error) if failed else
                     '已有内容线索，尚未完成语义归组')
            topics.append({'node_id':'preview:'+str(group['topic'] or '').encode('utf8').hex(),'topic':label,'name':label,
                           'files':group['files'],'basis':basis,'sample':[], 'naming_status':'failed' if all_failed else 'unread',
                           'state':'failed' if all_failed else group['state'], 'error':error,
                           'quick_failed_files':failed, 'kind':'ungrouped'})
        from services.large_package_catalog_hierarchy import visible
        branches=visible(c,package_id)
        # 批1 第 30 条：**同一个数字只能有一个来源。**
        #
        # 改动前 `navigation` 用缓存列 `g.member_count`、`topics` 用实时 COUNT ——
        # 同一响应里同一个叶组的文件数有两个来源，合并/切分之后缓存没同步时，
        # 导航上显示的数字与点进去看到的数字不一样（导航挂在树里读的是
        # `navigation`，而主题卡片读的是 `topics`；前端
        # `static/large-package-result.js:212` 把两者合成一张表按 node_id 取值）。
        # 现在 navigation 也走实时 COUNT，与 topics 完全同口径。
        # 分页不做截断：navigation 是前端的节点索引（byId），截断会让主题树缺节点。
        navigation_rows=c.execute(
            'SELECT g.id,g.result,g.content_revision,l.name,l.model,l.revision,l.locked,l.needs_review,'
            ' (SELECT COUNT(*) FROM large_catalog_members m JOIN files f'
            '  ON f.package_id=m.package_id AND f.path=m.path'
            '  WHERE m.package_id=g.package_id AND m.group_id=g.id) files'
            ' FROM large_catalog_groups g LEFT JOIN lp_catalog_labels l'
            ' ON l.package_id=g.package_id AND l.group_id=g.id'
            ' WHERE g.package_id=? ORDER BY g.id',(package_id,)).fetchall()
        navigation=[]
        for r in navigation_rows:
            files=int(r['files'] or 0)
            if files<=0:continue
            label=_display_name(r['id'], r['name'], r['result'])
            naming_status = _naming_status(r)
            navigation.append(dict(node_id=r['id'],name=label,topic=label,files=files,
                                   naming_status=naming_status, **leaf_review_facts(r['result'], r['needs_review'],
                                       model=r['model'], content_revision=r['content_revision']),
                                   holding_reason=_json_object(r['result']).get('hold_reason', ''),
                                   kind='system' if r['id'].startswith('km-other-') else 'staging' if r['id'].startswith('km-pending-') else 'leaf'))
        content=c.execute("SELECT COUNT(*) FROM files WHERE package_id=? AND length(COALESCE(json_extract(local_summary,'$.sample'),''))>=20",(package_id,)).fetchone()[0]
        attempted=c.execute("SELECT COUNT(*) FROM files WHERE package_id=? AND quick_status IN ('completed','failed')",(package_id,)).fetchone()[0]
        catalog_states = dict(c.execute('SELECT ' + CATALOG_STATE_SQL +
                                       ',COUNT(*) FROM files f WHERE f.package_id=? GROUP BY 1', (package_id,)))
        quality = {}
        stats = catalog_stats(c, package_id, branches, quality_out=quality)
        branches = _presentation_branches(branches, navigation)
        _presentation_stats(stats, branches)
        publication = _publication_metadata(c, package_id)
    catalog_pending=_pending_reason(store.get(package_id) or {})
    inventory=(store.get(package_id) or {}).get('counts') or {}
    result['inventory']={key:inventory.get(key) for key in ('inventory_complete','inventory_error_count','inventory_errors')}
    result['smart'].update(catalog_pending=catalog_pending,
                           topics=topics,branches=branches,navigation=navigation,topic_total=total+unknown_total,
                           catalog_states=catalog_states,
                           stats=stats, catalog_quality=quality, catalog_publication=publication, theme_total=stats['theme_nodes'],
                           leaf_total=stats['leaf_nodes'], hierarchy='recursive_content_tree',
                           topics_truncated=offset+len(topics)<total+unknown_total,
                           naming={**state(store,package_id), 'named_nodes':stats['named_leaf_nodes'],
                                   'named_files':stats['named_files'], 'total_nodes':stats['leaf_nodes'],
                                   'pending_nodes':stats['pending_naming_leaf_nodes']},
                           node_offset=offset,node_limit=limit,
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
        c.execute('BEGIN')
        groups = {}
        for row in c.execute(
            "SELECT g.id,g.result,g.content_revision,l.name,l.model,l.revision,l.locked,l.needs_review,"
            " (SELECT COUNT(*) FROM large_catalog_members m JOIN files f"
            "  ON f.package_id=m.package_id AND f.path=m.path"
            "  WHERE m.package_id=g.package_id AND m.group_id=g.id) AS files"
            " FROM large_catalog_groups g LEFT JOIN lp_catalog_labels l"
            " ON l.package_id=g.package_id AND l.group_id=g.id"
            " WHERE g.package_id=?", (package_id,)
        ):
            groups[str(row['id'])] = {
                'node_id': str(row['id']),
                'name': _display_name(row['id'], row['name'], row['result']),
                'files': int(row['files'] or 0),
                'naming_status': _naming_status(row),
                **leaf_review_facts(row['result'], row['needs_review'],
                                   model=row['model'], content_revision=row['content_revision']),
            }
        from services.large_package_catalog_hierarchy import visible
        branches = visible(c, package_id)
        branches = _presentation_branches(branches, groups.values())
        stats = catalog_stats(c, package_id, branches)
        _presentation_stats(stats, branches)
        publication = _publication_metadata(c, package_id)
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
            'kind': branch.get('kind', 'theme'),
            'independent_files': branch.get('independent_files', 0),
            'pending_files': branch.get('pending_files', 0),
            **{key: value for key, value in branch.items() if key.startswith((
                'display_', 'pending_name_independent_', 'review_independent_'))},
            'leaves': leaves,
        })
    # Branch children remain flattened for selection; display recursively without duplicate leaves.
    by_id={item['node_id']:item for item in themes}
    for branch in branches:
        item=by_id.get(branch['node_id'])
        if item is None:continue
        direct=branch.get('child_nodes')
        item['children']=[by_id[k] for k in (direct or []) if k in by_id]
        if direct is not None:item['leaves']=[groups[k] for k in direct if k in groups]
    themes=[by_id[b['node_id']] for b in branches if not b.get('parent_id') and b['node_id'] in by_id]
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
        'theme_total': stats['theme_nodes'],
        'leaf_total': stats['leaf_nodes'],
        'ungrouped_files': int(ungrouped or 0),
        'stats': stats,
        'catalog_publication': publication,
        'themes': themes,
    }
