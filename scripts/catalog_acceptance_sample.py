#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""大包智能目录 · 人工盲评验收评审集（**只读库 + 零模型调用**）。

做什么：从生产库里**分层抽样**若干组，为每组生成一张人工盲评卡（Markdown），
同时落一份自动指标 JSON（每项都带样本量）。评审者只看卡片判断"名称与成员是否切合"。

为什么是抽样：全量 7000+ 组人工看不过来；只要层设计对，抽样就能给出**带样本量**的
比例估计。分层维度取 `包 × 组规模桶 × 有无当前标签`——这三件事各自决定评审者看到
什么：不同包的语料性质不同、规模决定"这一组还能不能概括"、有无标签决定这张卡
有没有名字可评。层内抽样用**逐层独立种子**，所以改样本量或增删包不会打乱其它层
已抽中的组，两批评审集可以逐层对照。

抽样范围里两个刻意的取舍（都能用参数改回去）：
  · **默认抽全部组，不只是叶组**（`--leaves-only` 可退回只抽叶组）。实测本库里
    被主题层覆盖的组有 2000+ 个且几乎全是有名字的组（有名组 3035 个里只有 1003 个
    还是叶子），只抽叶组会让 986/1003 的名额落在同一个包上，评审集退化成单包样本；
    而且"叶不叶"随主题层重建而变（两次运行之间就从 5137 变成了 5151），拿它当抽样
    范围会牺牲可复现性。叶组信息没有丢：每包的叶组数、单元素组占比照全量报在
    `packages` 里，每张卡也记了 `is_leaf`。
  · **系统桶（km-other-/km-pending-）默认纳入抽样**：它们常挂着上千成员却完全没有
    名字，正是验收要看的东西；它们的质心按设计就是空的，而本脚本根本不读质心。

只读保证（本脚本对生产库零写入）：
  · 数据库一律 `sqlite3.connect('file:...?mode=ro', uri=True)` + `busy_timeout`；
  · **不复用 `LargePackageStore`**：它的 `__init__` 会 `CREATE TABLE IF NOT EXISTS`
    并 `PRAGMA journal_mode=WAL`，对生产库来说是写操作。这里只复刻 profile 模块
    需要的那两个接口（见 `ReadOnlyCatalog`）；
  · 不 import `services.vllm`，不 new 任何客户端，不产生任何 HTTP 请求；
  · 向量一列**从不读取**（`anchor` 只取 `length(anchor)>0` 派生出的布尔值），
    成员只按 (package_id, group_id) 走索引取路径，文件画像按主键 (package_id, path) 取。

用法（在项目根目录；`--out` 不填就打到 stdout）：
    .venv/bin/python -m scripts.catalog_acceptance_sample --groups 150 \
        --out /tmp/catalog_review.md --metrics /tmp/catalog_review.json
    .venv/bin/python -m scripts.catalog_acceptance_sample --groups 6 --db /tmp/fixture.db

进度信息一律走 stderr，stdout 只放 Markdown——这样省略 `--out` 时可以直接重定向。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 生产库路径写死是刻意的：本脚本不该为了拿路径去 import config（那会读 .env、
# 可能触发目录创建等副作用），审查工具必须只认显式路径。
DEFAULT_DB = (os.environ.get('SJFX_CATALOG_DB')
              or '/home/zhoulingjie/.local/state/sjfx-data-analysis-agent/large_package.db')
DEFAULT_SEED = 20240920
SIZE_BUCKETS = ((1, 1, '1'), (2, 4, '2-4'), (5, 9, '5-9'), (10, 29, '10-29'), (30, None, '30+'))
SYSTEM_BUCKET_PREFIXES = ('km-other-', 'km-pending-')
EXCERPT_MAX, EXCERPT_MIN, EXCERPT_LIMIT = 5, 3, 120
NO_LABEL = '（无当前标签）'
KIND_LABEL = {'real': '真名', 'status': '状态名', 'none': '无标签'}
REQUIRED_TABLES = ('large_catalog_groups', 'large_catalog_members', 'lp_catalog_labels',
                   'lp_catalog_branches', 'files', 'packages')

# 名称词面支持度的口径与"留出成员审计"那轮判决性实验一致：名称取汉字 3-gram，
# 丢掉"含两个及以上停用字"的 gram（'的了在'这类拼出来的假 gram），再看它在
# 「本组证据 + 模板词」里命中多少。它在本语料上判别力很弱（当时真名中位数 0.143），
# 所以只当**分布参考**报出来，不当作对错判据。
NAME_STOP = set('研究分析方法技术系统应用综述机制影响特征相关基于以及和与的了在对为及其等资料汇编目录')
NAME_NGRAM = 3
_CJK = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]')
_SENTENCE = re.compile(r'[^。！？!?；;\n]{2,}')
_HTML = re.compile(r'<[^>]{0,400}>')


# ---------------------------------------------------------------------------
# 只读连接壳
# ---------------------------------------------------------------------------
class ReadOnlyCatalog:
    """复刻 `LargePackageStore` 的 `_connect()/_row()`，但不建表、不写库。

    profile 模块只用这两个接口，保持同形就够用；`_row` 比原实现更严一点：原实现
    只对"解析失败"的 JSON 兜默认值，解析成列表时（`local_summary='[]'`）下游
    `.get()` 会炸。审查工具不该被一条脏数据打断，所以这里非 dict 一律当空。
    """

    def __init__(self, db_path):
        self.db_path = str(db_path)

    def _connect(self):
        # `?`/`#` 在 URI 里有含义，路径里出现时必须转义，否则 mode=ro 会被忽略。
        uri = 'file:%s?mode=ro' % self.db_path.replace('?', '%3f').replace('#', '%23')
        conn = sqlite3.connect(uri, uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        # 生产库正在被 worker 写：宁可等，也不要因瞬时锁冲突误报"库读不了"。
        conn.execute('PRAGMA busy_timeout=30000')
        return conn

    @staticmethod
    def _row(row):
        if row is None:
            return None
        item = dict(row)
        for key in ('local_summary', 'deep_summary', 'counts'):
            if isinstance(item.get(key), str):
                try:
                    value = json.loads(item[key])
                except Exception:
                    value = {}
                item[key] = value if isinstance(value, dict) else {}
        return item


# ---------------------------------------------------------------------------
# 抽样：包 × 规模桶 × 有无当前标签
# ---------------------------------------------------------------------------
def size_bucket(member_count):
    count = int(member_count or 0)
    for low, high, tag in SIZE_BUCKETS:
        if count >= low and (high is None or count <= high):
            return tag
    return SIZE_BUCKETS[-1][2]


def allocate(total, weights):
    """最大余数法分配名额；**每个非空层先保底 1**。

    为什么保底：层的存在本身就是信息（"这个包里有没有 30+ 的大组"），纯比例分配
    会让小层永远抽不到、指标里也就永远看不见它；名额不足层数时，名额给权重大的层。
    """
    items = sorted(weights.items(), key=lambda kv: (-kv[1], str(kv[0])))
    if total <= 0 or not items:
        return {}
    if total < len(items):
        return {key: 1 for key, _ in items[:total]}
    quota = {key: 1 for key, _ in items}
    left = total - len(items)
    base = float(sum(weight for _, weight in items)) or 1.0
    exact = {key: left * weight / base for key, weight in items}
    for key in quota:
        quota[key] += int(exact[key])
    # 余数按小数部分从大到小补；key 参与排序，保证同权重时结果也可复现。
    rest = max(0, total - sum(quota.values()))
    order = sorted(items, key=lambda kv: (-(exact[kv[0]] % 1.0), str(kv[0])))
    for key, _ in order[:rest]:
        quota[key] += 1
    return quota


def plan_sample(groups, total, seed):
    """返回 (抽中的组, 层计划)；层 = (包, 规模桶, 有无标签)，抽样在层内做。

    包名额按**有名组数**比例分配（大包多分），再给每个包保底 1 个名额（小包也看得见）。
    权重写成 `有名组数 + 组数×1e-6`：整数部分决定比例，小数部分只在有名组数打平时
    按包规模排序——不然"名额不够分"时会按字典序挑中几个最小最没信息量的包。
    层内按 **√层规模** 加权：纯比例会让单元素组层吃掉半个评审集（本库 7192 个组里
    4617 个是单元素组），而单元素组能提供的信息只有"它本不该单独成组"一条。
    这只影响抽样效率、不影响估计有效性——每层的抽中概率都记进 metrics，
    报告时按层权重还原成全量口径（`share_weighted`）。
    """
    cells, named, sizes = {}, {}, {}
    for group in groups:
        pid = group['package_id']
        cells.setdefault((pid, group['bucket'], group['has_label']), []).append(group)
        sizes[pid] = sizes.get(pid, 0) + 1
        if group['has_label']:
            named[pid] = named.get(pid, 0) + 1
    packages = sorted(sizes)
    quota = allocate(total, {pid: named.get(pid, 0) + sizes[pid] * 1e-6 for pid in packages})

    picked, plan = [], []
    for package_id in packages:
        want = quota.get(package_id, 0)
        local = {key: value for key, value in cells.items() if key[0] == package_id}
        if not want or not local:
            continue
        share = allocate(want, {key: len(value) ** 0.5 for key, value in local.items()})
        # √权重 + 保底会让小层分到超过它自身规模的名额，直接截断会**凭空丢掉名额**
        # （实测：某包要 16 个却只抽到 15 个）。所以把截断省下的名额再发给还有余量的层，
        # 优先补"离抽满最远"的那个——包名额只要够，就一定抽满。
        counts = {key: min(count, len(local[key])) for key, count in share.items()}
        left = want - sum(counts.values())
        while left > 0:
            room = [key for key in counts if counts[key] < len(local[key])]
            if not room:
                break
            best = max(room, key=lambda k: (len(local[k]) - counts[k], -counts[k], str(k)))
            counts[best] += 1
            left -= 1
        for key, count in sorted(counts.items(), key=lambda kv: str(kv[0])):
            items = sorted(local[key], key=lambda g: g['group_id'])
            draw = _rng(seed, key).sample(items, count)
            for group in draw:
                # 事后分层权重：本层全量组数 / 本层抽中组数。
                group['weight'] = len(items) / float(len(draw))
            picked.extend(draw)
            plan.append({'package_id': key[0], 'bucket': key[1], 'has_label': key[2],
                         'frame': len(items), 'drawn': len(draw)})
    picked.sort(key=lambda g: (g['package_id'], -int(g['member_count'] or 0), g['group_id']))
    return picked, plan


def _rng(seed, key):
    """层内独立随机数：换样本量或增删包时，其它层已抽中的组**不变**，便于两批对照。"""
    return random.Random('%s|%s|%s|%s' % (seed, key[0], key[1], key[2]))


def covered_group_ids(conn):
    """主题层 children 覆盖到的组 id 集合。

    任务给的判法是 `instr(children, g.id)>0`，这里改成把 children 当 JSON 解析：
    分支表只有百来行，解析一次就能拿到精确集合，而子串判法在 id 互为前缀时会误判
    （`km-x-0004` 会命中 `km-x-00048`）。解析失败才退回子串扫描。
    """
    covered = set()
    for row in conn.execute('SELECT package_id, children FROM lp_catalog_branches'):
        raw = str(row['children'] or '')
        try:
            kids = json.loads(raw)
        except Exception:
            kids = re.findall(r'"([^"]+)"', raw)
        for kid in kids if isinstance(kids, list) else ():
            covered.add((str(row['package_id']), str(kid)))
    return covered


def load_groups(conn, include_system, min_members, leaves_only, package_status=None):
    """一次取回全部分组行（不含 anchor/vector），返回 (全部组, 可评审组)。

    为什么整表取回：分层配额要**全量**的 (包, 规模桶, 有无标签) 计数，逐组查库要
    7000+ 次往返；组表本身只有几百字节一行，一次取回更便宜，而向量/质心一列不读。

    `package_status` 是**可选的包状态过滤**（逗号分隔，取自 `packages.status`）。
    为什么需要：生产库里"已取消"的包会留下几千个半成品组（实测 4,872 个组里只有
    986 个有名字），它们混进来会把"无标签占比"从个位数抬到过半——那是取消操作的
    结果，不是流水线的质量。默认不过滤（保持"看到的即是库里的全部"），需要评"跑完
    的目录"时用 `--package-status completed,waiting_for_selection`。
    注意过滤用**内连接**，所以没有 `packages` 行的孤立组会被排除——这正是过滤的
    应有语义（拿不到状态的组无法判定），不过默认不开过滤时不会有这个副作用。
    """
    covered = covered_group_ids(conn)
    where, params = '', []
    if package_status:
        marks = ','.join('?' for _ in package_status)
        where = ' WHERE p.status IN (%s)' % marks
        params = list(package_status)
    rows = conn.execute(
        'SELECT g.package_id, g.id, g.member_count, g.status, g.content_revision,'
        '       length(g.anchor) > 0 AS anchored, g.representative,'
        '       p.status AS package_status,'
        '       l.version, l.name, l.description, l.needs_review'
        '  FROM large_catalog_groups g'
        # 用 LEFT JOIN 取包状态：缺 packages 行的孤立组也要留着（右连接会把它整行丢掉），
        # 过滤条件放在 WHERE 里，所以不过滤时这一列只是信息。
        '  LEFT JOIN packages p ON p.id = g.package_id'
        '  LEFT JOIN lp_catalog_labels l'
        '    ON l.package_id = g.package_id AND l.group_id = g.id'
        # 新鲜度条件必须写在 ON 里：写进 WHERE 会把"没有当前标签"的组整行滤掉，
        # 而那正好是本工具要单独计数的一层。
        '   AND (l.revision = g.content_revision OR l.locked = 1)'
        + where, params).fetchall()
    groups, reviewable = [], []
    for row in rows:
        item = dict(row)
        item['group_id'] = str(item.pop('id'))
        item['member_count'] = int(item['member_count'] or 0)
        item['bucket'] = size_bucket(item['member_count'])
        item['has_label'] = bool(str(item['name'] or '').strip())
        item['is_leaf'] = (item['package_id'], item['group_id']) not in covered
        item['is_system'] = str(item['group_id']).startswith(SYSTEM_BUCKET_PREFIXES)
        groups.append(item)
        if (item['member_count'] >= max(1, min_members)
                and (include_system or not item['is_system'])
                and (not leaves_only or item['is_leaf'])):
            reviewable.append(item)
    return groups, reviewable


# ---------------------------------------------------------------------------
# 取文与卡片
# ---------------------------------------------------------------------------
def material_text(ctx, package_id, paths):
    """用命名路径自己的取文接口补成员正文，返回 {path: text}。

    为什么需要兜底：库里有**两代 `local_summary`**。旧一代的 `portrait` 没有
    `evidence` 字段（实测整个 wx 包的 portrait 只有 catalog_* 键），profile 模块按
    `portrait.evidence` 取文只能拿到空串——整包会变成空卡片。而命名路径自己是用
    `large_package_catalog.material()` 取文的（见 `naming.representatives`），
    这里就按同一条路取，两边看到的材料口径一致；截断仍用 profile 模块的
    `PROFILE_TEXT_CHARS`，免得一边 1500 字一边 6000 字把文档频次比歪。
    """
    from services.large_package_catalog import material
    limit = int(ctx['profile'].PROFILE_TEXT_CHARS)
    marks = ','.join('?' for _ in paths)
    rows = ctx['conn'].execute('SELECT * FROM files WHERE package_id=? AND path IN (%s)' % marks,
                               [package_id] + list(paths)).fetchall()
    texts = {}
    for raw in rows:
        row = ctx['store']._row(raw)
        value = None
        try:
            source = material(row)
            value = (source[0] if isinstance(source, tuple) else source) if source else None
        except Exception:
            value = None
        text = str((value or {}).get('text') or '') if isinstance(value, dict) else ''
        texts[str(row.get('path'))] = re.sub(r'\s+', ' ', text).strip()[:limit]
    return texts


def group_view(ctx, group):
    """一张卡需要的全部派生材料，按组缓存（卡片正文与指标共用同一次计算）。

    画像仍走 profile 模块：`group_evidence_profile` 取成员画像与模板词，
    `ctfidf_terms` 用**本包有界背景**算 c-TF-IDF。不重写这套算法，只把"成员文本"
    这一步补全（见 material_text）——否则旧一代画像的包全是空卡片。
    """
    key = (group['package_id'], group['group_id'], int(group['content_revision'] or 0))
    if key in ctx['views']:
        return ctx['views'][key]
    profile = ctx['profile']
    profiles, templates = profile.group_evidence_profile(ctx['store'], key[0], key[1], key[2])
    texts = {item['path']: item['text'] for item in profiles}
    missing = [path for path, text in texts.items() if not text]
    if missing:
        texts.update(material_text(ctx, key[0], missing))
    value = profile.ctfidf_terms(list(texts.values()),
                                 profile.package_background(ctx['store'], key[0]), templates)
    sampled = int(value.get('sampled') or 0)
    terms = []
    if sampled:
        for item in (value.get('distinctive') or []):
            if item.get('term'):
                terms.append({'term': str(item['term']), 'df': int(item.get('df') or 0),
                              'share': int(item.get('df') or 0) / float(sampled)})
    view = {'members': [{'path': path, 'text': text} for path, text in texts.items()],
            'templates': list(templates or ()), 'terms': terms[:12], 'sampled': sampled,
            'blob': ' '.join(list(texts.values()) + list(templates or ()))}
    ctx['views'][key] = view
    return view


def name_kind(name, naming):
    """真名 / 状态名 / 无标签。

    状态名（"内容主题不统一（待细分）"等）是诊断结论而不是主题名，混在一起统计会把
    "系统已经识别出问题"算成"命名成功"。判据直接用命名模块的 `is_status_name`，
    不在这里复制清单——清单会随模块漂移。
    """
    text = str(name or '').strip()
    if not text:
        return 'none'
    return 'status' if naming.is_status_name(text) else 'real'


def generic_hit(name, naming):
    """复用 naming 的正则，不另造泛化词表：口径必须与写入侧完全一致。

    两个口径分开报：`word` 是名称里出现泛化词（"XX政策汇编"这类可能是诚实描述），
    `only` 是整个名称就是泛化词（"资料汇编"），后者才是硬伤。
    """
    text = str(name or '')
    return {'word': bool(naming._GENERIC_WORD.search(text)),
            'only': bool(naming._GENERIC_ONLY.match(text.strip()))}


def name_grams(name, size=NAME_NGRAM):
    chars = ''.join(ch for ch in str(name or '') if _CJK.search(ch))
    grams = set()
    for index in range(0, max(0, len(chars) - size + 1)):
        gram = chars[index:index + size]
        if sum(1 for ch in gram if ch in NAME_STOP) >= 2:
            continue
        grams.add(gram)
    return grams


def literal_support(name, blob, size=NAME_NGRAM):
    """名称的汉字 n-gram 有多大比例字面出现在本组材料里；名称没有汉字时返回 None。"""
    grams = name_grams(name, size)
    if not grams:
        return None
    return sum(1 for gram in grams if gram in blob) / float(len(grams))


def cjk_ratio(blob):
    """材料里的汉字占比。词面命中率只在同语言语料上才有意义（英文包恒为 0），
    所以指标里对"汉字占比 ≥0.3"的子集单独报一份，口径同那轮词面实验。"""
    if not blob:
        return 0.0
    return len(_CJK.findall(blob)) / float(len(blob))


def clean_display(value):
    """摘录的展示清洗：去 HTML 标签、压空白。**不参与任何指标计算**——
    指标仍按 profile 模块的原文口径算；展示与计算分开，免得清洗规则悄悄改掉数字。
    """
    return re.sub(r'\s+', ' ', _HTML.sub(' ', str(value or ''))).strip()


def sentence_with(text, term, limit=EXCERPT_LIMIT):
    """截取包含该短语的片段：优先整句，句子比 limit 还长时以短语为中心开窗。

    为什么必须开窗：证据文本常是"一整段没有标点"的拼接（实测合成语料就是这样），
    只取句首会把短语甩到窗口之外，评审者看到的是一句与名称无关的话。
    """
    if not term:
        return ''
    for match in _SENTENCE.finditer(text):
        piece = match.group()
        if term not in piece:
            continue
        if len(piece) <= limit:
            return clean_display(piece)[:limit]
        at = piece.find(term)
        start = max(0, min(at - (limit - len(term)) // 2, len(piece) - limit))
        window = clean_display(piece[start:start + limit])[:limit]
        return ('…' if start > 0 else '') + window
    return ''


def lead_sentence(text, limit=EXCERPT_LIMIT):
    match = _SENTENCE.search(clean_display(text))
    return (match.group() if match else clean_display(text))[:limit]


def pick_excerpts(members, terms, want=EXCERPT_MAX):
    """挑 3–5 条摘录，**每份成员文件最多一条**，优先命中本组辨识性短语的句子。

    为什么要"不同文件"：评审者判断的是"这批成员是不是同一件事"，同一份文件给三段
    只能证明那一份，给不出组级判断。辨识短语一条都没命中时（空画像、或语料被模板
    占满）才退回文件自己的首句——宁可给弱证据，也不给空卡片。
    """
    rows, used_paths = [], set()
    for item in terms:
        if len(rows) >= want:
            break
        for member in members:
            if member['path'] in used_paths or not member['text']:
                continue
            sentence = sentence_with(clean_display(member['text']), item['term'])
            if not sentence:
                continue
            used_paths.add(member['path'])
            rows.append({'path': member['path'], 'text': sentence, 'term': item['term'],
                         'df': item['df'], 'share': item['share']})
            break
    for member in members:
        if len(rows) >= want:
            break
        if member['path'] in used_paths or not member['text']:
            continue
        used_paths.add(member['path'])
        rows.append({'path': member['path'], 'text': lead_sentence(member['text']),
                     'term': '', 'df': 0, 'share': None})
    return rows


def support_split(name, view, cited_paths):
    """把组内材料分成"模型读过的"和"没读过的"两堆，各算一次名称字面支持度。

    这是第 2 项接进流水线的同一个口径（`naming.support_score`），这里对**已经发布的
    标签**重算一遍，用来看"名称是不是只照着被引用的那几份写"。

    **只作为排序参考**：本语料上这个差值的判别力很低（实测真名 p50 0.143 / 假名
    p90 0.105，50% 真名会被误判），所以它出现在指标里、**不出现在盲评卡上**——
    卡片一旦印上它，评审者就会被我们的分数带着走，盲评就不再是盲评。
    """
    if not view['members']:
        return None
    cited, held = [], []
    for member in view['members']:
        if not member['text']:
            continue
        (cited if member['path'] in cited_paths else held).append(member['text'])
    return {'cited': literal_support(name, ' '.join(cited)) if cited else None,
            'held_out': literal_support(name, ' '.join(held)) if held else None,
            'cited_members': len(cited), 'held_out_members': len(held)}


def label_churn(conn):
    """小改动稳定性（零调用代理指标）：同一组被反复重命名的程度。

    真要做"小改动稳定性"得给同一组喂扰动后的输入再命名一次，那是模型调用，
    而这个脚本的硬约束是零调用。**在本系统里被改名的次数**（标签历史表的行数）
    是它的代理：成员每微调一次就换一个名字，正是"不稳定"在生产库里的直接痕迹。

    为什么不用 `labels.version`：那个数字会被**旧系统导入**带进来（实测有组
    version=69,333 而历史表只有 6 行——那是导入时带过来的修订号，不是被改名
    六万次）。用历史行数才如实反映"本系统改了几次"。
    """
    rows = conn.execute('SELECT package_id, group_id, COUNT(*) AS n '
                        'FROM lp_catalog_label_history GROUP BY package_id, group_id').fetchall()
    counts = [int(r['n'] or 0) for r in rows]
    labels = conn.execute('SELECT COUNT(*) AS n FROM lp_catalog_labels').fetchone()
    recent = conn.execute('SELECT COUNT(*) AS n FROM lp_catalog_labels WHERE updated > ?',
                          (time.time() - 86400,)).fetchone()
    newest = conn.execute('SELECT COUNT(*) AS n FROM lp_catalog_label_history WHERE created > ?',
                          (time.time() - 3600,)).fetchone()
    one = sum(1 for n in counts if n <= 1)
    two = sum(1 for n in counts if n == 2)
    many = sum(1 for n in counts if n >= 3)
    return {'labels': int(labels['n'] or 0), 'groups_with_history': len(counts),
            'renamed_once': two, 'renamed_twice_plus': many,
            'never_renamed': one,
            'renamed_share': (round((two + many) / float(len(counts)), 4) if counts else None),
            'max_renames': max(counts) if counts else 0,
            'labels_updated_last_24h': int(recent['n'] or 0),
            'renames_last_hour': int(newest['n'] or 0)}


def cited_evidence(conn, package_id, group_id):
    """该组被命名时引用的证据条数（`labels.evidence` 是 JSON 数组）。

    单独查这一列、而不是在取组表时一并取：evidence 平均几十 KB，全量取回要白白多占
    上百 MB 内存，而这个指标只需要抽中组的那 150 行。
    """
    row = conn.execute('SELECT evidence FROM lp_catalog_labels WHERE package_id=? AND group_id=?',
                       (package_id, group_id)).fetchone()
    if not row:
        return None, 'no_label', set()
    try:
        value = json.loads(row['evidence'])
    except Exception:
        return None, 'bad_json', set()
    if not isinstance(value, list):
        return None, 'ok', set()
    # 标签里的 evidence 存的是**完整证据对象**（含 source_path），所以能直接分出
    # "模型读过哪些成员"，这正是留出成员支撑检查需要的分母。
    paths = {str(item.get('source_path')) for item in value
             if isinstance(item, dict) and item.get('source_path')}
    return len(value), 'ok', paths


def package_label(root_path):
    """包的展示名：`packages` 表没有 name 列，用根目录名代替。"""
    return os.path.basename(str(root_path or '').rstrip('/')) or '?'


def code(value):
    """行内代码：反引号没法转义，去掉它；换行/制表压成空格，但**不动普通空格**——
    真实路径里有连续空格（实测 '000025_特  力Ａ_2023年报.pdf'），折叠了就不再是原路径，
    评审者按路径找文件会找不到。"""
    return '`%s`' % re.sub(r'[\r\n\t]+', ' ', str(value or '')).replace('`', "'")[:160]


def cell(value):
    """表格单元：竖线必须转义，否则一行会多出一列。"""
    return re.sub(r'\s+', ' ', str(value or '')).replace('|', '\\|').strip()


def member_rows(conn, package_id, group_id):
    """成员表里**实际**有多少行。分组的 row.member_count 是会过期的计数（worker 正在
    重建分组时尤其明显，实测有组声称 5 个成员而成员表里一行都没有），两者不一致时
    卡片要说明，否则评审者会以为"卡是空的"是工具的问题。走 (package_id,group_id) 索引。"""
    return int(conn.execute('SELECT COUNT(*) FROM large_catalog_members '
                            'WHERE package_id=? AND group_id=?', (package_id, group_id)).fetchone()[0] or 0)


def build_card(index, total, group, ctx):
    """一张盲评卡：只呈现当前目录状态，不透露任何产出来源信息。"""
    pid, gid = group['package_id'], group['group_id']
    view = group_view(ctx, group)
    count, evidence_state, cited_paths = cited_evidence(ctx['conn'], pid, gid)
    rows_in_table = member_rows(ctx['conn'], pid, gid)
    excerpts = pick_excerpts(view['members'], view['terms'])
    representative = group.get('representative') or (view['members'][0]['path'] if view['members'] else '')

    lines = ['## 第 %d 张 / 共 %d 张' % (index, total), '',
             '| 字段 | 值 |', '| --- | --- |',
             '| 组 | %s |' % code(gid),
             '| 所属包 | %s（%s） |' % (code(pid), cell(package_label(group.get('root_path')))),
             '| 成员数 | %d |' % group['member_count'],
             '| 组状态 | %s |' % cell(group.get('status')),
             '| 标签版本 | %s |' % ('v%s' % group['version'] if group.get('version') is not None else NO_LABEL),
             '| 复核标记 | %s |' % ('是' if group.get('needs_review') else '否'),
             '| 命名引用证据 | %s |' % ('%d 条 / %d 个成员' % (count, group['member_count'])
                                        if count is not None else '—'),
             '| 代表文件 | %s |' % code(representative), '']
    if rows_in_table != group['member_count']:
        lines += ['（注：目录里记的成员数是 %d，成员表实际 %d 行——计数已过期或分组正在重建。）'
                  % (group['member_count'], rows_in_table), '']
    lines += ['**名称**：%s' % (cell(group.get('name')) or NO_LABEL), '',
             '**描述**：%s' % (cell(group.get('description')) if group.get('has_label') else NO_LABEL), '',
             '**辨识性短语**（区分度降序；支持度 = 出现在多少份**抽样**成员里）', '']
    if view['terms']:
        lines += ['| 短语 | 支持 |', '| --- | --- |']
        for item in view['terms'][:10]:
            lines.append('| %s | %d/%d（%.0f%%） |' % (
                cell(item['term']), item['df'], view['sampled'], 100.0 * item['share']))
    else:
        lines.append('（本组没有可用画像：成员画像缺失，或语料被模板占满）')
    lines += ['', '**成员证据摘录**（每份文件最多一条，均取自本组成员）', '']
    for item in excerpts:
        hit = ('　命中“%s”（支持 %d/%d）' % (cell(item['term']), item['df'], view['sampled'])
               if item['term'] else '　（未命中辨识短语，取该文件首句）')
        lines += ['- %s%s' % (code(item['path']), hit), '  > %s' % cell(item['text']), '']
    if not excerpts:
        lines += ['（该组没有任何可读的成员画像，无法提供摘录）', '']
    elif len(excerpts) < EXCERPT_MIN:
        lines += ['（该组只有 %d 份可读成员画像，摘录不足 %d 条）' % (len(excerpts), EXCERPT_MIN), '']
    if view['templates']:
        lines += ['（已识别为模板/页眉的短语：%s）' % cell('、'.join(view['templates'][:6])), '']
    lines += ['**人工结论（请填写）**', '',
              '- [ ] 切合　- [ ] 部分切合　- [ ] 不切合',
              '- 问题类型（可多选）：- [ ] 分组错　- [ ] 命名错　- [ ] 过泛　- [ ] 证据不足　- [ ] 其他',
              '- 备注：', '', '---', '']
    row = {
        'group_id': gid, 'package_id': pid, 'member_count': group['member_count'],
        'member_rows': rows_in_table,
        'bucket': group['bucket'], 'status': group.get('status'), 'version': group.get('version'),
        'needs_review': bool(group.get('needs_review')), 'is_system': group['is_system'],
        'is_leaf': group['is_leaf'], 'anchored': bool(group.get('anchored')),
        'name': group.get('name') or '',
        'name_kind': name_kind(group.get('name'), ctx['naming']),
        'evidence_cited': count, 'evidence_state': evidence_state,
        'evidence_coverage': (count / float(group['member_count'])
                              if (count is not None and group['member_count']) else None),
        'profile_sample': view['sampled'], 'excerpts': len(excerpts),
        'cjk_evidence': cjk_ratio(view['blob']) >= 0.3,
        'literal_support': (literal_support(group.get('name'), view['blob'])
                            if group['has_label'] else None),
        # 只进指标、不进卡片（见 support_split 的说明）。
        'support_split': (support_split(group.get('name'), view, cited_paths)
                          if group['has_label'] else None),
        'weight': group.get('weight'), 'seconds': None,
    }
    return '\n'.join(lines), row


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------
def quantiles(values):
    """p10/p50/p90 + 样本量。空样本返回 n=0，而不是 0.0——0.0 会被误读成"实测为零"。"""
    items = sorted(float(v) for v in values if v is not None)
    if not items:
        return {'n': 0, 'p10': None, 'p50': None, 'p90': None}

    def pick(q):
        return round(items[min(len(items) - 1, int(len(items) * q))], 4)

    return {'n': len(items), 'p10': pick(.10), 'p50': pick(.50), 'p90': pick(.90)}


def share(rows, hit, weight=False):
    """一个比例 + 它的样本量。weight=True 时按事后分层权重还原成全量口径。"""
    if not rows:
        return {'hits': 0, 'n': 0, 'share': None, 'share_weighted': None}
    chosen = [row for row in rows if hit(row)]
    value = None
    total_weight = sum(float(row.get('weight') or 0) for row in rows)
    if weight and total_weight > 0:
        value = sum(float(row['weight']) for row in chosen) / total_weight
    return {'hits': len(chosen), 'n': len(rows), 'share': round(len(chosen) / float(len(rows)), 4),
            'share_weighted': None if value is None else round(value, 4)}


def package_metrics(groups, sampled):
    """每个包的**全量**结构指标（不是抽样估计）：叶组数、单元素组占比、系统桶成员数。

    这一块刻意用全量：只是对一个几千行的表做聚合，没有抽样误差，评审时可以直接当
    事实用；标了 `scope: 全量` 以免和上面那些抽样估计混在一起读。
    """
    drawn = {}
    for row in sampled:
        drawn[row['package_id']] = drawn.get(row['package_id'], 0) + 1
    table = {}
    for group in groups:
        item = table.setdefault(group['package_id'], {
            'package_id': group['package_id'], 'scope': '全量', 'groups': 0, 'leaf_groups': 0,
            'named_groups': 0, 'named_leaves': 0, 'singleton_leaves': 0, 'empty_groups': 0,
            'topic_covered_groups': 0, 'system_bucket_groups': 0, 'system_bucket_members': 0})
        item['groups'] += 1
        item['named_groups'] += int(group['has_label'])
        item['empty_groups'] += int(group['member_count'] == 0)
        if group['is_system']:
            item['system_bucket_groups'] += 1
            item['system_bucket_members'] += group['member_count']
        elif not group['is_leaf']:
            item['topic_covered_groups'] += 1
        else:
            item['leaf_groups'] += 1
            item['singleton_leaves'] += int(group['member_count'] == 1)
            item['named_leaves'] += int(group['has_label'])
    for item in table.values():
        item['singleton_share'] = (round(item['singleton_leaves'] / float(item['leaf_groups']), 4)
                                   if item['leaf_groups'] else None)
        item['sampled_groups'] = drawn.get(item['package_id'], 0)
    return sorted(table.values(), key=lambda item: -item['groups'])


def stale_label_count(conn):
    """挂在**零成员组**上的当前标签数：目录里出现永远点不开的条目，属结构性问题。"""
    row = conn.execute(
        'SELECT COUNT(*) FROM large_catalog_groups g JOIN lp_catalog_labels l'
        '  ON l.package_id = g.package_id AND l.group_id = g.id'
        ' WHERE g.member_count = 0 AND (l.revision = g.content_revision OR l.locked = 1)'
    ).fetchone()
    return int(row[0] or 0)


def build_metrics(groups, frame, sampled, rows, plan, ctx, args, elapsed, integrity):
    named = [row for row in rows if row['name_kind'] != 'none']
    # 事后分层权重只能覆盖**抽到过的层**；样本小的时候会有整层没抽到，
    # 这时 share_weighted 只在那部分层上成立——把覆盖率一起报出来，别让人误读成全量。
    covered = sum(item['frame'] for item in plan)
    coverage = round(covered / float(len(frame)), 4) if frame else 0.0
    metrics = {
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'scope': {'db': args.db, 'groups_requested': args.groups, 'seed': args.seed,
                  'min_members': args.min_members,
                  'frame': 'leaves-only' if args.leaves_only else 'all-groups',
                  'package_status': sorted({g['package_status'] for g in frame
                                            if g.get('package_status')}) or None,
                  'include_system_buckets': not args.exclude_system_buckets,
                  'elapsed_seconds': round(elapsed, 1)},
        'sampling': {
            'note': '以下比例都是**抽样估计**，不是全量；n 是抽中的组数，share_weighted 是'
                    '按事后分层权重还原的全量口径（只能还原到层，层内仍是随机抽样）',
            'frame_groups': len(frame), 'frame_packages': len({g['package_id'] for g in frame}),
            'drawn': len(rows), 'leaf_groups_all': sum(1 for g in groups if g['is_leaf']),
            'weighted_coverage': coverage, 'strata': plan,
        },
        'name_kind': {
            'definition': '真名=有当前标签且不是诊断状态名；状态名=命名模块 STATUS_NAMES 里的规范名；'
                          '无标签=没有当前标签（revision 不匹配且未 locked）',
            'sample': len(rows), 'status_names': sorted(ctx['naming'].STATUS_NAMES.values()),
            'real': share(rows, lambda r: r['name_kind'] == 'real', weight=True),
            'status': share(rows, lambda r: r['name_kind'] == 'status', weight=True),
            'none': share(rows, lambda r: r['name_kind'] == 'none', weight=True),
        },
        'generic_words': {
            'definition': '复用命名模块的正则：word=名称含泛化词，only=整个名称就是泛化词',
            'sample': len(named),
            'word': share(named, lambda r: generic_hit(r['name'], ctx['naming'])['word']),
            'only': share(named, lambda r: generic_hit(r['name'], ctx['naming'])['only']),
        },
        'literal_support': {
            'definition': '名称汉字 %d-gram 在「本组证据+模板词」里的字面命中比例；口径同留出成员'
                          '审计那轮词面实验，判别力弱，只作分布参考' % NAME_NGRAM,
            'all': quantiles([row['literal_support'] for row in named]),
            'cjk_evidence': quantiles([row['literal_support'] for row in named if row['cjk_evidence']]),
            'no_gram': sum(1 for row in named if row['literal_support'] is None),
        },
        'held_out_support': {
            'definition': '把组内材料分成「命名时被引用的」与「没读过的」两堆，各算名称汉字的'
                          '字面支持度（口径同 naming.support_score）。cited 高、held_out 低'
                          '说明名称只照着被引用的那几份写。**判别力很低**（实测 50% 真名会被'
                          '误判），只作排序参考，不构成结论；也不印在盲评卡上，免得带着评审者走。',
            'cited': quantiles([(r.get('support_split') or {}).get('cited') for r in rows]),
            'held_out': quantiles([(r.get('support_split') or {}).get('held_out') for r in rows]),
            'flagged': sum(1 for r in rows
                           if (r.get('support_split') or {}).get('cited') is not None
                           and (r.get('support_split') or {}).get('held_out') is not None
                           and r['support_split']['cited'] >= 0.5
                           and r['support_split']['held_out'] < 0.5 * r['support_split']['cited']),
            'n': sum(1 for r in rows if r.get('support_split')),
        },
        'label_churn': dict(ctx.get('churn') or {}, definition=(
            '小改动稳定性（零调用代理）：真做稳定性要给同一组喂扰动输入再命名一次，'
            '那是模型调用。版本次数是「成员微调就换一个名字」在生产库里的直接痕迹。')),
        'evidence_coverage': dict({
            'definition': '命名时引用的证据条数 / 该组成员数；越小说明命名当时看到的成员越少',
        }, **quantiles([row['evidence_coverage'] for row in named])),
        'evidence_state': {state: sum(1 for row in rows if row['evidence_state'] == state)
                           for state in ('ok', 'no_label', 'bad_json')},
        'card_quality': {
            'excerpts_per_card': quantiles([row['excerpts'] for row in rows]),
            'cards_without_excerpts': sum(1 for row in rows if not row['excerpts']),
            'cards_without_terms': sum(1 for row in rows if not row['profile_sample']),
            'groups_without_anchor': sum(1 for row in rows if not row['anchored']),
            'seconds_per_card': quantiles([row['seconds'] for row in rows]),
            'sampled_leaf_groups': sum(1 for row in rows if row['is_leaf']),
            'sampled_system_buckets': sum(1 for row in rows if row['is_system']),
            'member_count_mismatch': sum(1 for row in rows
                                         if row['member_rows'] != row['member_count']),
        },
        'integrity': integrity,
        'packages': package_metrics(groups, rows),
        'warnings': [],
    }
    if len(rows) < args.groups:
        metrics['warnings'].append('实际抽中 %d 组，少于请求的 %d 组（可评审层不足，或被层数保底吃掉）'
                                   % (len(rows), args.groups))
    if not 100 <= args.groups <= 200:
        metrics['warnings'].append('样本量 %d 不在建议区间 100–200，抽样误差会明显变大' % args.groups)
    if coverage < 0.99 and rows:
        metrics['warnings'].append('事后分层权重只覆盖了可评审组的 %.1f%%（其余层没抽到），'
                                   'share_weighted 只在该覆盖范围内成立' % (100.0 * coverage))
    if integrity['zero_member_labels']:
        metrics['warnings'].append('有 %d 个当前标签挂在**零成员组**上（目录里出现点不开的条目）'
                                   % integrity['zero_member_labels'])
    if metrics['evidence_state']['bad_json']:
        metrics['warnings'].append('%d 个抽中组的 evidence 字段不是合法 JSON'
                                   % metrics['evidence_state']['bad_json'])
    if metrics['card_quality']['cards_without_excerpts']:
        metrics['warnings'].append('%d 张卡没有证据摘录（成员画像缺失）'
                                   % metrics['card_quality']['cards_without_excerpts'])
    if metrics['card_quality']['member_count_mismatch']:
        metrics['warnings'].append('%d 个抽中组的 member_count 与成员表行数不一致'
                                   '（计数过期或分组正在重建）'
                                   % metrics['card_quality']['member_count_mismatch'])
    return metrics


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='大包智能目录：分层抽样的盲评验收评审集（只读、零模型调用）')
    parser.add_argument('--db', default=DEFAULT_DB, help='SQLite 路径（一律以 mode=ro 打开）')
    parser.add_argument('--groups', type=int, default=150, help='抽样的组数（建议 100–200）')
    parser.add_argument('--seed', type=int, default=DEFAULT_SEED, help='抽样种子（默认固定，保证可复现）')
    parser.add_argument('--min-members', type=int, default=1, help='纳入抽样的最小成员数')
    parser.add_argument('--leaves-only', action='store_true',
                        help='只抽未被主题层覆盖的叶组（默认抽全部组：本库有名组里多数已被主题层'
                             '覆盖，只抽叶组会让评审集退化成单包样本，且叶组集合随主题层重建而变）')
    parser.add_argument('--exclude-system-buckets', action='store_true',
                        help='不抽 km-other-/km-pending- 系统桶（默认纳入：系统桶里常有上千成员'
                             '却完全没有名字，正是验收要看的东西；其质心按设计为空，本脚本不读质心）')
    parser.add_argument('--package-status', default=None,
                        help='只评这些状态的包（逗号分隔，取自 packages.status，如 '
                             'completed,waiting_for_selection）。默认不过滤：已取消的包会留下'
                             '几千个半成品组，它们会把"无标签占比"从个位数抬到过半，'
                             '那是取消操作的结果而不是流水线质量——评"跑完的目录"时请显式指定')
    parser.add_argument('--out', default=None, help='评审卡写入该文件（默认 stdout）')
    parser.add_argument('--metrics', default=None, help='自动指标写入该 JSON 文件')
    return parser.parse_args(argv)


def open_catalog(db_path):
    if not os.path.exists(db_path):
        raise SystemExit('数据库不存在：%s' % db_path)
    store = ReadOnlyCatalog(db_path)
    conn = store._connect()
    have = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [name for name in REQUIRED_TABLES if name not in have]
    if missing:
        conn.close()
        raise SystemExit('这个库里没有大包目录表（缺 %s），无法评审：%s' % (', '.join(missing), db_path))
    return store, conn


def render_header(args, frame, drawn):
    """评审集说明页：只讲怎么填，不讲这些卡从哪来。"""
    statuses = [s.strip() for s in str(args.package_status or '').split(',') if s.strip()]
    scope = ['- 抽样层：包 × 组规模桶（1 / 2–4 / 5–9 / 10–29 / 30+）× 有无当前标签']
    if statuses:
        # 说明"这一批评的是哪一类包"。不写清的话，把"取消后留下的半成品目录"和
        # "跑完的目录"混在一起看，比例会被前者主导（实测差 50 个百分点）。
        scope.append('- 只评这些状态的包：%s（其它状态的包，例如已取消留下的半成品，未纳入）'
                     % '、'.join(statuses))
    scope.append('- 库：%s（只读打开；本评审集不修改任何数据）' % args.db)
    scope.append('')
    return '\n'.join([
        '# 大包智能目录 · 人工盲评卡（抽样 %d 组）' % drawn, '',
        '- 生成时间：%s' % time.strftime('%Y-%m-%d %H:%M:%S'),
        '- 抽样范围：%d 个包、%d 个可评审组中分层抽取 %d 个（seed=%d，同参数可复现）'
        % (len({g['package_id'] for g in frame}), len(frame), drawn, args.seed)] + scope + [
        '## 填写说明', '',
        '1. 只看卡片里的**成员证据**，判断给出的**名称**是否概括了这些成员共同讲的事；'
        '不要求逐字命中，允许概括与改写。',
        '2. 名称写着“%s”表示这一组当前没有可用标签；此时只判断分组是否合理，'
        '并在“问题类型”里勾选最贴近的一项。' % NO_LABEL,
        '3. 一张卡只选一个切合度；“问题类型”可多选；判断不了就写进备注。',
        '4. 摘录每条都标了来源文件、均取自本组成员；不足 3 条时卡片会注明原因。', '',
        '---', ''])


def log(message):
    """进度一律走 stderr：stdout 留给 Markdown，重定向时不会被进度行污染。"""
    sys.stderr.write(message + '\n')
    sys.stderr.flush()


def main(argv=None):
    args = parse_args(argv)
    started = time.monotonic()
    from services import large_package_catalog_naming as naming
    from services import large_package_catalog_profile as profile

    store, conn = open_catalog(args.db)
    log('库=%s' % args.db)
    statuses = [s.strip() for s in str(args.package_status or '').split(',') if s.strip()]
    groups, frame = load_groups(conn, not args.exclude_system_buckets, args.min_members,
                                args.leaves_only, statuses)
    log('组 %d 个（叶组 %d），可评审 %d 个（范围=%s%s）' % (
        len(groups), sum(1 for g in groups if g['is_leaf']), len(frame),
        '仅叶组' if args.leaves_only else '全部组',
        '，包状态=' + ','.join(statuses) if statuses else ''))
    if not frame:
        log('没有可评审的组：库可能是空的，或全部被 --min-members / --leaves-only / '
            '--exclude-system-buckets 过滤掉')
    roots = {str(row['id']): row['root_path'] for row in conn.execute('SELECT id, root_path FROM packages')}
    for group in frame:
        group['root_path'] = roots.get(group['package_id'], '')
    integrity = {
        'groups_total': len(groups),
        'leaf_groups_total': sum(1 for g in groups if g['is_leaf']),
        'leaf_groups_without_label': sum(1 for g in groups if g['is_leaf'] and not g['has_label']),
        'zero_member_labels': stale_label_count(conn),
    }
    # 必须在 conn.close() **之前**算：版本次数指标要查 labels 表，而连接是在卡片
    # 生成完之后才关的（第一版把它放进 build_metrics 里，那边连接已经关了）。
    churn = label_churn(conn)

    picked, plan = plan_sample(frame, min(args.groups, len(frame)), args.seed)
    log('抽样 %d 组，覆盖 %d 个层' % (len(picked), len(plan)))
    ctx = {'store': store, 'conn': conn, 'profile': profile, 'naming': naming, 'views': {},
           'churn': churn}

    cards, rows = [], []
    for index, group in enumerate(picked, 1):
        card_started = time.monotonic()
        card, row = build_card(index, len(picked), group, ctx)
        row['seconds'] = round(time.monotonic() - card_started, 2)
        cards.append(card)
        rows.append(row)
        log('[%3d/%3d] %s 成员=%-5d %-4s 摘录=%d 用时 %.1fs' % (
            index, len(picked), group['group_id'][:22], group['member_count'],
            KIND_LABEL[row['name_kind']], row['excerpts'], row['seconds']))
    conn.close()

    document = '\n'.join([render_header(args, frame, len(picked))] + cards)
    if not cards:
        document += '\n（本次没有抽到任何组）\n'
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as handle:
            handle.write(document)
        log('评审卡已写入 %s' % args.out)
    else:
        sys.stdout.write(document)
        sys.stdout.flush()

    elapsed = time.monotonic() - started
    if args.metrics:
        metrics = build_metrics(groups, frame, picked, rows, plan, ctx, args, elapsed, integrity)
        with open(args.metrics, 'w', encoding='utf-8') as handle:
            json.dump(metrics, handle, ensure_ascii=False, indent=2)
        log('指标已写入 %s' % args.metrics)
    log('完成：%d 张卡，总耗时 %.1fs（平均 %.2fs/组）' % (
        len(picked), elapsed, elapsed / max(1, len(picked))))
    return 0


if __name__ == '__main__':
    sys.exit(main())
