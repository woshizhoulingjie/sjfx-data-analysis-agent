"""Naming profiles: all-member bounded features plus sampled layout evidence.

Document counts describe retained features, never full-document coverage.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from contextlib import closing
from collections import Counter
from services import large_package_evidence as evidence
from services import large_package_evidence_store as feature_store

LOGGER = logging.getLogger(__name__)

POLICY_VERSION = 'evidence-profile/3'

# 模板检测：12 字窗口的文档频次 ≥30% 且至少出现在 3 份文件里，才算模板。
TEMPLATE_NGRAM = 12
TEMPLATE_MIN_SHARE = 0.30
TEMPLATE_MIN_FILES = 3
TEMPLATE_MAX_PHRASES = 40
TEMPLATE_MAX_LENGTH = 60

# 画像抽取时每组最多读多少份成员文件。模板在多数文件里重复出现，
# 有界抽样足够判定，同时把"每组要读多少 DB"钉死在常数上。
PROFILE_SAMPLE_MEMBERS = 48
PROFILE_TEXT_CHARS = 1500
PROFILE_MAX_TERMS = 24
PROFILE_NGRAM = (2, 4)

# 纯标点/纯数字/单字符重复的 n-gram 不是模板，也不该进画像。
_NON_WORD = re.compile(r'[\s\W_]+', re.UNICODE)

# 词段切分：空白与标点都算边界。语料里的证据文本是"条目/表格行"用空格拼接的，
# 段就是最小的语义单位。
_SEGMENT_SPLIT = re.compile(
    r'[\s，。；：、,.;:!?！？（）()【】\[\]《》〈〉「」『』“”"\''
    r'|/\\—–~@#$%^&*+=·…]+')

# 结构性词：它们是版式的一部分，不是内容主题。
_STRUCTURAL = frozenset({
    '编号', '摘要', '目录', '条目', '备注', '说明', '序号', '图表', '附表',
    '数据来源', '资料来源', '公开资料', '整理', '汇编', '资料', '续表', '合计',
})

_TERM_PATTERNS = {}

# 英文功能词：它们在每篇英文文档里都高频出现，会被 DF 当成"共同主题"。
# 拉丁文路径只能取整词，所以这里必须显式剔除功能词（实测某论文组的画像
# 全是 that/here/work/with/from 这类东西，把真正的主题词挤掉了）。
_EN_STOPWORDS = frozenset('''
a about above after again against all also am an and any are as at be because been before being below between both but by
came can cannot could did do does doing done down during each few for from further had has have having he her here hers
herself him himself his how i if in into is it its itself just like made make many may me might more most much must my
myself no nor not now of off on once one only or other ought our ours ourselves out over own same said she should since
some still such take than that the their theirs them themselves then there these they this those through time to too under
until up upon used using very was way we well were what when where which while who whom why will with within without work
would year years you your yours yourself yourselves
'''.split())

_CACHE = {}
_CACHE_LIMIT = 256

# 学术/出版类样板词：它们不是主题，却因为"每篇论文都有"而在组内高频。
# 实测论文组的画像被 perpetuity / granted / licenseavailab / authorscontrib /
# preprintthis / corresponding / university / sciences 这类东西占满。统计规则
# 区分不了"出版商样板集中在某一批同源论文里"与"主题词集中在本组"——两者长得
# 一样；这一类只能靠词表清掉（类比中文的 _STRUCTURAL）。
# 长词还做**子串**匹配：PDF 抽取常把词粘在一起（'license available' →
# 'licenseavailab'、'authors contributions' → 'authorscontrib'），整词匹配抓不到。
_EN_BOILERPLATE = frozenset('''
abstract affiliation affiliations author authors citation cited copyright correspond
correspondence corresponding declaration declare doi editor funding grant grants granted
institute institutions interest interests isbn issn journal license licensed licenseavailab
manuscript medrxiv permission permissions perpetuity preprint preprints publisher publishers
received reprint repository reserved reviewer reviewers rights sciences springer submission
supplementary university wiley accepted published publication volume issue pages title elsevier
biorxiv
'''.split())
_BOILERPLATE_STEMS = tuple(sorted((word for word in _EN_BOILERPLATE if len(word) >= 6),
                                  key=len, reverse=True))


def _latin_boilerplate(text):
    """判断一个候选单元是不是学术/出版样板。"""
    low = str(text or '').lower()
    if not low or not re.search(r'[a-z]', low):
        return False
    for word in low.split():
        if word in _EN_BOILERPLATE:
            return True
    return any(stem in low for stem in _BOILERPLATE_STEMS)


def _informative(value):
    """过滤掉"看起来像重复但没有任何信息"的片段。

    注意：不能用 `len(set(text)) <= 2` 当判据——2 字词的字符集大小本来就是 2，
    那会把**所有二字词**（"级别""事务"）全部砍掉，画像随即可用性大减。
    真正要排除的是"单字符重复"（"AAAA"）、"版式符号组合"（"、。|"）与英文功能词。
    """
    text = str(value or '')
    if len(set(text)) <= 1:
        return False
    if text.strip().lower() in _EN_STOPWORDS:
        return False
    if _latin_boilerplate(text):
        return False
    stripped = _NON_WORD.sub('', text)
    return len(stripped) >= max(2, (len(text) + 1) // 2)


def _segments(text):
    """把文本切成词段（空白与标点都是边界），丢掉单字符段。"""
    return [seg for seg in _SEGMENT_SPLIT.split(str(text or '')) if len(seg) >= 2]


def _structural_phrase(phrase):
    """整条短语是否只是版式词（"公开资料""数据来源""条目"…）的拼接。"""
    remain = str(phrase or '')
    for word in sorted(_STRUCTURAL, key=len, reverse=True):
        remain = remain.replace(word, '')
    return len(_NON_WORD.sub('', remain)) < 2


# ---------------------------------------------------------------------------
# 取文
# ---------------------------------------------------------------------------
def group_profiles(store, package_id, group_id, limit=PROFILE_SAMPLE_MEMBERS):
    """读取该组成员文件的取文档案（按路径散列稳定抽样，最多 limit 份）。

    返回 [{'path','text','headings','keywords','fields'}]，只读，不写库。
    """
    with closing(store._connect()) as conn:
        paths = [str(r[0]) for r in conn.execute(
            'SELECT path FROM large_catalog_members WHERE package_id=? AND group_id=? ORDER BY path',
            (package_id, group_id))]
    if not paths:
        return []
    if len(paths) > limit:
        # 稳定散列顺序：同一修订号下结果可复现，且不会总取到目录序前几份。
        paths = sorted(paths, key=lambda p: hashlib.sha256(p.encode('utf-8')).hexdigest())[:limit]
    profiles = []
    try:
        with closing(store._connect()) as conn:
            marks = ','.join('?' for _ in paths)
            rows = conn.execute(
                'SELECT path,local_summary FROM files WHERE package_id=? AND sensitive=0 AND path IN (%s)' % marks,
                [package_id] + paths).fetchall()
    except Exception:
        LOGGER.exception('group profile read failed: %s/%s', package_id, group_id)
        return []
    for raw in rows:
        row = store._row(raw)
        info = (row.get('local_summary') or {}).get('portrait') or {}
        parts = [p for p in (info.get('evidence') or [])
                 if isinstance(p, dict) and p.get('text')]
        text = evidence.compact_text(parts, chars=PROFILE_TEXT_CHARS)
        profiles.append({'path': str(row.get('path') or ''),
                         'text': text,
                         'headings': [str(h) for h in (info.get('headings') or [])[:6]],
                         'keywords': [str(k) for k in ((row.get('local_summary') or {}).get('keywords') or [])[:8]],
                         'fields': [str(f) for f in (info.get('fields') or [])[:8]]})
    return profiles


# ---------------------------------------------------------------------------
# L1.2 跨文件模板检测
# ---------------------------------------------------------------------------
def _merge_phrases(seeds, texts, ngram):
    """把互相重叠的高频 n-gram 合并成最长短语。"""
    merged = set()
    for text in texts:
        index, size = 0, len(text)
        while index <= size - ngram:
            if text[index:index + ngram] in seeds:
                end = index + ngram
                while end + 1 <= size and text[end - ngram + 1:end + 1] in seeds:
                    end += 1
                merged.add(text[index:end])
                index = end
            else:
                index += 1
    kept = []
    for phrase in sorted(merged, key=len, reverse=True):
        if len(phrase) > TEMPLATE_MAX_LENGTH:
            continue
        if any(phrase in longer for longer in kept):
            continue
        kept.append(phrase)
        if len(kept) >= TEMPLATE_MAX_PHRASES:
            break
    return kept


def template_terms(texts, ngram=TEMPLATE_NGRAM, min_share=TEMPLATE_MIN_SHARE,
                   min_files=TEMPLATE_MIN_FILES):
    """返回在这批文件里高频重复的短语（页眉/封面/免责声明这类模板）。

    用**文档频次**：每种 n-gram 每份文件只计一次。重复 200 次的单篇文档
    不会因此把某个词顶成模板，而 20 篇独立文档共同支撑的短语会。
    """
    texts = [t for t in (texts or []) if t]
    total = len(texts)
    if total < min_files:
        return []
    need = max(min_files, int(math.ceil(total * float(min_share))))
    counter = {}
    for text in texts:
        for gram in set(text[i:i + ngram] for i in range(0, max(0, len(text) - ngram + 1))):
            if not _informative(gram):
                continue
            counter[gram] = counter.get(gram, 0) + 1
    seeds = set(g for g, count in counter.items() if count >= need)
    if not seeds:
        return []
    # Long, repeated headers often merge with adjacent layout terms and stop
    # matching the boilerplate lexicon as a whole. Recheck their individual
    # segments so the recognizable header terms remain removable.
    found = []
    for phrase in _merge_phrases(seeds, texts, ngram):
        segments = _segments(phrase)
        layout_context = (evidence.is_boilerplate(phrase)
                          or _structural_phrase(phrase)
                          or any(evidence.is_boilerplate(segment)
                                 or _structural_phrase(segment)
                                 for segment in segments))
        if evidence.is_boilerplate(phrase) or _structural_phrase(phrase):
            found.append(phrase)
        for index, segment in enumerate(segments):
            if evidence.is_boilerplate(segment) or _structural_phrase(segment):
                found.append(segment)
            elif layout_context and index > 0 and _informative(segment):
                # Keep the first repeated segment as the collection/topic title;
                # later segments belong to the shared masthead/layout block.
                found.append(segment)
    return list(dict.fromkeys(found))[:TEMPLATE_MAX_PHRASES]


# ---------------------------------------------------------------------------
# L2 组画像：DF 支撑的 c-TF-IDF
# ---------------------------------------------------------------------------
# 方案取舍。BERTopic 的 c-TF-IDF 需要"多个类"作为背景语料来算 IDF，而这里
# 一次只面对一个组。可选做法有三个：
#   (a) 拿本包其它组当背景 —— 要读全部组的画像，代价随组数二次增长；
#   (b) 只用组内 DF 当分数 —— 退化成"重复最多的短语"，正好是模板的特征；
#   (c) 用**本包的有界样本**建一张背景 DF 表（只建一次、按包缓存），
#       组内 DF × 对背景的 IDF。
# 取 (c)：它同时满足"被多篇文档支撑"（组内 DF）与"区别于其他组"（IDF），
# 且成本被钉死在常数上。这就是本文件里 c-TF-IDF 的落地形式：
#     score(term) = df_group(term)/n_group × log(1 + n_bg/(1 + df_bg(term)))
PROFILE_MIN_DF = 2          # 至少被 2 份文档支撑，才算"共同"而不是个别措辞
PROFILE_TOP_TERMS = 24
COMMON_MIN_SHARE = 0.50     # 出现在半数以上文档里 → 共同研究对象
MINORITY_MAX_SHARE = 0.30   # 少数派子题：有支撑但明显不是主体
PACKAGE_SAMPLE_FILES = 300
BACKGROUND_VERSION = 'package-background/1'
# 在整包背景里超过这个比例的短语是**全语料级套话**（同一套汇编的页眉/封面），
# 无论在本组多常见都不具备区分度，直接排除。
BACKGROUND_MAX_SHARE = 0.35
# 尺度无关的"区分度"判据：一个短语在整包背景里已经常见（≥BACKDROP_SHARE）时，
# 它在本组必须**明显更常见**才配称为本组的区分性短语。这一条抓的是出版商许可
# 声明这类样板——实测论文组的高频词是 perpetuity / granted / authorscontrib /
# preprintthis，它们只出现在约 40% 的文件里，位置也不固定，前两条规则都抓不到。
BACKDROP_SHARE = 0.25
DISTINCTIVE_RATIO = 1.5
# 元数据（关键词/标题）判为模板的口径：**本组高频 ∧ 整包背景也高频**。
# 只用"本组高频"会误伤主题——一个同质组里"人工智能与机器学习"本来就该出现在
# 大多数成员的关键词里；只有它在整包背景里同样泛滥，才说明它是这套资料的版式。
META_MIN_SHARE = 0.30
# 背景语料小于这个规模时不做"背景也高频"的判定：样本太少会把噪声当规律，
# 此时保守地不判模板（宁可留着，也不误删主题词）。
META_BACKGROUND_MIN_FILES = 20


def _df_counter(texts, ngram, confine=False):
    """每份文档只计一次的 n-gram 文档频次。

    confine=True 时只在**段内**取 n-gram。为什么需要这个开关：这批语料的证据
    文本是"条目/表格行"用空格拼起来的（'… 关系型数据流管理系统 联合国人权事务
    委员会 表 项目 数量 备注 条目'），跨段拼接会造出 ' 表 项目 数量 备注 ' 这类
    毫无意义的"短语"，还把相邻两个不相干的条目粘成一个（实测 '容器 共識'）。
    段内取词能同时消掉这两类噪声；而模板检测需要**跨段**（页眉本身就是一串段），
    所以两条路径各用各的口径。
    """
    counter = {}
    for text in texts:
        seen = set()
        for chunk in (_segments(text) if confine else [str(text or '')]):
            seen |= _units(chunk, ngram)
        if ngram == PROFILE_NGRAM[0]:
            # 拉丁词组跨空白，必须按整篇取（词段是按空白切的）。
            seen |= _latin_units(str(text or ''))
        for gram in seen:
            if not _informative(gram):
                continue
            counter[gram] = counter.get(gram, 0) + 1
    return counter


_CJK = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]')
_LATIN_WORD = re.compile(r'[A-Za-z][A-Za-z0-9\-_]{3,}')


def _units(chunk, ngram):
    """从一个词段里取候选单元：**CJK 用 n 字窗口，拉丁文用整词/词组**。

    为什么必须分开：对英文文本，2–4 个字符的 n-gram 只会产出 'Cent'、'edia'、
    'died' 这类碎片（实测某论文组的画像全是这种东西，把真正的主题词挤掉了），
    而中文的 2–4 字恰恰就是词。数字串同样不是主题（'1313'、'00014349'）。
    整词只在最小窗口那一趟加入，避免同一次调用里被重复计数。
    """
    text = str(chunk or '')
    found = set()
    if _CJK.search(text):
        for i in range(0, max(0, len(text) - ngram + 1)):
            gram = text[i:i + ngram]
            if _CJK.search(gram):
                found.add(gram)
    if ngram == PROFILE_NGRAM[0]:
        found |= _latin_units(text)
    return found


def _latin_units(text):
    """拉丁文的候选单元：整词 + **真实相邻**的词组。

    英文的主题词大多是词组（machine learning / working memory），只取单词会把
    画像压到只剩几个通用词。两个关键细节：
      · 必须按**原文位置**判断相邻，不能按过滤后的词表相邻——'state of the art
        model' 过滤掉短词后会拼出假短语 'state model'；
      · 必须在**整篇**文本上取，不能只在单个词段上取——词段是按空白切的，
        'machine' 与 'learning' 会落进不同的段（这正是词组一度全部消失的原因）。
    """
    text = str(text or '')
    matches = [(m.start(), m.group().lower()) for m in _LATIN_WORD.finditer(text)]
    found = set(word for _, word in matches)
    for size in (2, 3):
        for i in range(0, max(0, len(matches) - size + 1)):
            window = matches[i:i + size]
            adjacent = all(
                text[window[k][0] + len(window[k][1]):window[k + 1][0]].strip() == ''
                for k in range(size - 1))
            if not adjacent:
                continue
            piece = [word for _, word in window]
            if any(word in _EN_STOPWORDS for word in piece):
                continue
            found.add(' '.join(piece))
    return found


def _layout_phrases(texts, phrases, edge=0.15, min_share=0.7, min_hits=3, min_length=120):
    """出现在大多数文档**开头/结尾固定位置**的短语是页眉页脚，不是主题词。

    为什么还需要这一条：这类页眉页脚常常是"本组高频、整包背景不高频"的
    （同一本书的版权声明、页码、期刊名），背景 DF 规则抓不到；但它们的**位置**
    很固定。只统计长度 ≥120 字符的文本，避免短证据里"整段都在边缘"造成误判。
    """
    flagged = set()
    usable = [t for t in texts if len(t) >= min_length]
    if len(usable) < min_hits:
        return flagged
    for phrase in phrases:
        if not phrase:
            continue
        hits = edges = 0
        for text in usable:
            index = text.find(phrase)
            if index < 0:
                continue
            hits += 1
            if index <= len(text) * edge or index >= len(text) * (1.0 - edge):
                edges += 1
        if hits >= min_hits and edges / float(hits) >= min_share:
            flagged.add(phrase)
    return flagged


def _package_file_count(store, package_id):
    try:
        with closing(store._connect()) as conn:
            return int(conn.execute('SELECT COUNT(*) FROM files WHERE package_id=?',
                                    (package_id,)).fetchone()[0] or 0)
    except Exception:
        return 0


def _background_paths(store, package_id, limit):
    """在**整包范围**内均匀抽样成员路径。

    为什么不能用 `files ORDER BY path LIMIT n`：路径前缀往往对应同一批来源，
    而出版商许可声明这类样板只出现在某一批文件里——按前缀抽样会让背景语料与
    组内语料来源不同，"全语料套话"与 IDF 判据随即失效（实测论文组的
    perpetuity / granted / authorscontrib 就是这样漏过去的：它们在组内占 ~40%，
    在按前缀取的背景里却几乎不出现）。
    """
    try:
        with closing(store._connect()) as conn:
            total = int(conn.execute('SELECT COUNT(*) FROM large_catalog_members '
                                     'WHERE package_id=?', (package_id,)).fetchone()[0] or 0)
            if total <= limit:
                rows = conn.execute('SELECT path FROM large_catalog_members '
                                    'WHERE package_id=? ORDER BY path', (package_id,)).fetchall()
            else:
                stride = max(2, total // limit)
                rows = conn.execute('SELECT path FROM large_catalog_members '
                                    'WHERE package_id=? AND (rowid % ?) = 0 '
                                    'ORDER BY path LIMIT ?',
                                    (package_id, stride, limit)).fetchall()
        return [str(row[0]) for row in rows]
    except Exception:
        LOGGER.exception('background sampling failed: %s', package_id)
        return []


def package_background(store, package_id):
    """One package-wide DF pass over persisted bounded features, not raw files."""
    return feature_store.background(store, package_id)


def _meta_items(row, info):
    """一份文件的元数据条目（关键词/章节/字段），用于判"版式还是主题"。"""
    local = row.get('local_summary') or {}
    items = []
    for source in (local.get('keywords') or [], info.get('headings') or [], info.get('fields') or []):
        for item in source:
            text = re.sub(r'\s+', ' ', str(item or '')).strip()
            if 2 <= len(text) <= 40:
                items.append(text)
    return set(items)


def meta_templates(profiles, background):
    # Treat a metadata phrase as layout noise only when its wording is structural
    # and the package-wide metadata supports that reading. A topic repeated in a
    # small group must not be removed merely because it is common locally.
    background = background or {}
    files = int(background.get('files') or 0)
    if files < 10:
        return []
    meta_df = background.get('meta_df') or background.get('df') or {}
    minimum = max(3, int(math.ceil(files * TEMPLATE_MIN_SHARE)))
    items = {str(item).strip() for p in profiles
             for key in ('headings', 'fields', 'keywords')
             for item in (p.get(key) or []) if str(item).strip()}
    candidates = []
    for item in items:
        support = int(meta_df.get(item, 0) or 0)
        body_support = int((background.get('df') or {}).get(item, 0) or 0)
        # A phrase present in most package metadata but absent from body evidence
        # is a repeated masthead/label, even when a domain-specific organization
        # name is not covered by a hand-maintained boilerplate vocabulary.
        if support >= minimum and body_support < minimum:
            candidates.append(item)
    return sorted(candidates, key=lambda x: (-len(x), x))[:24]


def template_vocabulary(templates):
    """模板词表：模板短语本身 + 它们拆出来的每一个词段。

    为什么必须展开到段：画像按"段"提取短语（不跨空白拼接），而模板短语是跨段的
    （'数据库与数据工程 工程技术中心 编号 摘要 本文汇编…'）。不把段并进词表，
    画像就会把 '工程技术中心'、'编号'、'本文汇编' 当成高频主题词重新捡回来——
    这正是首轮真实数据验证里看到的现象。
    """
    vocab = set()
    for phrase in templates or ():
        text = str(phrase or '').strip()
        if text:
            vocab.add(text)
        for segment in _segments(phrase):
            if not _structural_phrase(segment):
                vocab.add(segment)
    return vocab


def _contains_template(gram, templates):
    return any(gram in phrase for phrase in templates)


def ctfidf_terms(texts, background, templates=()):
    """组画像：区分性短语 / 共同研究对象 / 少数派子题。

    全部基于**文档频次**（每份文档只计一次），因此"一篇文档里重复 200 次的词"
    不会压过"20 篇文档共同支撑的词"；模板短语在计分前就被排除。
    """
    texts = [t for t in (texts or []) if t]
    total = len(texts)
    if total < 1:
        return {'distinctive': [], 'common': [], 'minority': [], 'sampled': 0}
    # 模板短语拆成段后一并排除，否则高频页眉里的小段会被当成主题词捡回来。
    exclude = template_vocabulary(templates) or set(templates or ())
    df_group = {}
    for ngram in range(PROFILE_NGRAM[0], PROFILE_NGRAM[1] + 1):
        for gram, count in _df_counter(texts, ngram, confine=True).items():
            df_group[gram] = max(df_group.get(gram, 0), count)
    bg_df = (background or {}).get('df') or {}
    bg_files = int((background or {}).get('files') or 0) or 1
    def background_term_support(phrase):
        if bg_files < 10:
            return 0
        candidates = [int(count or 0) for term, count in bg_df.items()
                      if len(str(term)) >= 3 and str(term) in phrase]
        return max(candidates or [0])

    scored = []
    blocked = set()
    for gram, count in df_group.items():
        if count < PROFILE_MIN_DF:
            continue
        # 二字词碎片太多（中文里任意两字相邻都会成词，实测 '于国'、'则第'
        # 这类排版残片），要求它被更多文档支撑才认。
        if len(gram) <= 2 and _CJK.search(gram) and count < 3:
            continue
        if _contains_template(gram, exclude):
            continue
        share = count / float(total)
        idf = math.log(1.0 + bg_files / float(1 + bg_df.get(gram, 0)))
        scored.append((share * idf, gram, count, idf))
    scored.sort(key=lambda item: (-item[0], -len(item[1]), item[1]))
    # 先按分数取候选，再合并成更长短语（短 n-gram 便宜且稳健，长短语更好读）
    candidates = [gram for _, gram, _, _ in scored[:PROFILE_TOP_TERMS * 4]]
    support = {gram: count for _, gram, count, _ in scored}
    phrases = _merge_supported(candidates, texts, support)

    def phrase_stats(phrase):
        """Count the complete phrase and use supported background phrase DF."""
        pattern = _term_pattern(phrase)
        exact_df = sum(bool(pattern.search(text)) for text in texts)
        background_df = max(
            [int(bg_df.get(phrase, 0) or 0)]
            + [int(count or 0) for term, count in bg_df.items()
               if len(str(term)) >= 3 and str(term) in phrase]
        )
        idf = math.log(1.0 + bg_files / float(1 + background_df))
        return (exact_df / float(total)) * idf, exact_df

    items = []
    layout = _layout_phrases(texts, [p for p in phrases])
    for phrase in phrases:
        if _structural_phrase(phrase):
            continue
        if phrase in layout and evidence.is_boilerplate(phrase):
            # 固定位置重复的短语是页眉页脚（版权声明、页码、期刊名），不是主题词。
            continue
        # 合并会"跨过"被拉黑的词重建出整包套话（实测：'工程技术'+'术中心'
        # 又能拼回'工程技术中心'），所以合并结果必须再查一遍。
        if any(phrase[i:i + n] in blocked
               for n in (3, PROFILE_NGRAM[1])
               for i in range(0, max(1, len(phrase) - n + 1))):
            continue
        score, count = phrase_stats(phrase)
        if not count:
            continue
        corpus_support = background_term_support(phrase)
        # Terms common across most of the corpus are not distinctive when they
        # only appear in a minority of this group; keep genuine group-wide
        # topics intact and lower (rather than erase) moderate background terms.
        if corpus_support / float(bg_files) >= 0.80 and count / float(total) < 0.80:
            continue
        items.append({'term': phrase, 'df': int(count), 'score': round(float(score), 4)})
    items.sort(key=lambda item: (-item['score'], -len(item['term']), item['term']))
    distinctive, common, minority = [], [], []
    for item in items[:PROFILE_TOP_TERMS]:
        distinctive.append(item)
        share = item['df'] / float(total)
        if share >= COMMON_MIN_SHARE:
            common.append(item)
        elif share <= MINORITY_MAX_SHARE:
            minority.append(item)
    return {'distinctive': distinctive, 'common': common[:8], 'minority': minority[:8],
            'sampled': total, 'background_files': bg_files}


def _merge_supported(candidates, texts, support):
    """把互相重叠、且每个片段都有文档支撑的候选单元合并成最长短语。

    两趟扫描，各管一类：
      · **段内**（默认）：含 CJK 的语料按词段合并，防止把表格行与相邻条目
        粘成假短语（实测 ' 表 项目 数量 备注 '、'容器 共識' 都是这么来的）；
      · **整篇、只认含空格的种子**：拉丁词组本身跨空白（'machine learning'），
        按段扫永远找不到它——这正是词组一度全部消失在画像里的原因。
    另外旧写法只在段内逐字符尝试 PROFILE_NGRAM 规定的 2–4 字符窗口，
    对 'learning'、'memory' 这类整词种子永远匹配不上（实测论文组只剩
    'role'、'care'、'data' 这种 ≤4 字母的词）。现在改成"把每个种子在范围内的
    所有出现位置求并集、再合并首尾相接或重叠的区间"，与种子长度无关。
    """
    seeds = sorted(set(candidates), key=len, reverse=True)
    if not seeds:
        return []
    phrase_seeds = [seed for seed in seeds if ' ' in seed]

    def covers(scope, pool):
        intervals = []
        for seed in pool:
            start = scope.find(seed)
            while start >= 0:
                intervals.append((start, start + len(seed)))
                start = scope.find(seed, start + 1)
        if not intervals:
            return []
        intervals.sort()
        out, (current_start, current_end) = [], intervals[0]
        for start, end in intervals[1:]:
            if start <= current_end:
                current_end = max(current_end, end)
            else:
                out.append(scope[current_start:current_end])
                current_start, current_end = start, end
        out.append(scope[current_start:current_end])
        return out

    merged = set()
    for text in texts:
        for segment in _segments(text):
            merged.update(covers(segment, seeds))
        if phrase_seeds:
            merged.update(covers(str(text or ''), phrase_seeds))
    kept = []
    for phrase in sorted(merged, key=lambda p: (-len(p), p)):
        if len(phrase) > TEMPLATE_MAX_LENGTH:
            continue
        if any(phrase in longer or longer in phrase for longer in kept):
            continue
        kept.append(phrase)
    return kept


def group_profile(store, package_id, group_id, revision):
    feature_store.ensure_package(store, package_id)
    epoch = feature_store.snapshot_key(store, package_id)
    key = ('profile', str(store.db_path), str(package_id), str(group_id), int(revision or 0), epoch, POLICY_VERSION)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    rows = feature_store.group_cards(store, package_id, group_id)
    cards = [card for _,card in rows if card is not None]
    readable = [card for card in cards if card.get('readable')]
    counter = Counter()
    for card in readable:
        counter.update(set(card.get('terms') or []))
    background = package_background(store, package_id)
    bg_count = max(1, int(background.get('files') or 0))
    bg_df = background.get('df') or {}
    total = len(readable)
    items = []
    for term,count in counter.items():
        if count < min(PROFILE_MIN_DF,max(1,total)) or evidence.is_boilerplate(term) or _structural_phrase(term):
            continue
        score = count/max(1,total)*math.log(1+bg_count/(1+bg_df.get(term,0)))
        items.append({'term':term,'df':count,'score':round(score,4), 'support_scope':'retained_features'})
    def compact(pool, limit):
        kept=[]
        for item in pool:
            # Remove nested duplicates only when they have identical support.
            if any(item['term'] in other['term'] and item['df']==other['df'] for other in kept):
                continue
            kept.append(item)
            if len(kept)>=limit:
                break
        return kept
    distinctive=compact(sorted(items,key=lambda x:(-x['score'],-len(x['term']),x['term'])),PROFILE_TOP_TERMS)
    common=compact(sorted((x for x in items if x['df']/max(1,total)>=COMMON_MIN_SHARE),
                          key=lambda x:(-x['df'],-len(x['term']),x['term'])),8)
    minority=compact(sorted((x for x in items if x['df']/max(1,total)<=MINORITY_MAX_SHARE),
                            key=lambda x:(-x['score'],-len(x['term']),x['term'])),8)
    # Layout inspection stays bounded; it is not the population used for DF.
    profiles,templates=group_evidence_profile(store,package_id,group_id,revision)
    value={'distinctive':distinctive,'common':common,'minority':minority,
           'members':len(rows),'feature_files':len(cards),'readable_files':total,
           'missing_features':len(rows)-len(cards),'sampled':total,
           'statistics_scope':'all_available_member_features',
           'coverage':'sampled_content','layout_sampled':len(profiles),
           'background_files':int(background.get('files') or 0),'templates':templates[:12],
           'template_terms':templates[:12],'meta_templates':meta_templates(profiles,background)}
    _cache_put(key,value)
    return value


def _term_pattern(term):
    """把模板短语编译成"忽略空白差异"的正则（带缓存）。

    为什么必须忽略空白：模板短语是在画像文本上抽取的，而证据文本还要经过
    clean_catalog_content 与 tokenizer clip，空白可能不同——实测差别就是短语
    末尾的一个空格。用精确子串匹配会**全部落空**：模板明明在句首，却一个字
    都删不掉（这正是首轮验证里发现的缺陷）。
    """
    key = str(term)
    pattern = _TERM_PATTERNS.get(key)
    if pattern is None:
        parts = [re.escape(ch) for ch in key if not ch.isspace()]
        pattern = re.compile(r'\s*'.join(parts)) if parts else False
        if len(_TERM_PATTERNS) >= 512:
            _TERM_PATTERNS.clear()
        _TERM_PATTERNS[key] = pattern
    return pattern or None


def strip_templates(text, terms):
    """把模板短语从正文里去掉。只删命中的短语，不改动其余文字。"""
    value = str(text or '')
    if not value or not terms:
        return value
    for term in sorted(terms, key=len, reverse=True):
        if not term:
            continue
        if term in value:
            value = value.replace(term, ' ')
            continue
        pattern = _term_pattern(term)
        if pattern is not None:
            value = pattern.sub(' ', value)
    return re.sub(r'\s{2,}', ' ', value).strip()


def is_template_meta(value, templates):
    """判断一个字段/关键词/标题是不是模板的一部分。

    keywords/headings 常常**本身就是模板词**（实测某个 101 成员组的 keywords
    是 ['数据库与数据工程','工程技术中心','编号','摘要','本文汇编','篇资料']，
    整条就是那句页眉）。只剥正文而不剥这些字段，等于把模板又塞回提示词。
    """
    text = re.sub(r'\s+', '', str(value or ''))
    if not text:
        return True
    if text in _STRUCTURAL:
        return True
    for phrase in templates or ():
        flat = re.sub(r'\s+', '', str(phrase or ''))
        if flat and (text in flat or flat in text):
            return True
    return False


def filter_meta(values, templates, limit):
    """过滤模板字段并去重、限量。"""
    kept = []
    for item in values or ():
        text = re.sub(r'\s+', ' ', str(item or '')).strip()
        if not text or is_template_meta(text, templates):
            continue
        if text in kept:
            continue
        kept.append(text)
        if len(kept) >= limit:
            break
    return kept


# ---------------------------------------------------------------------------
# 缓存入口
# ---------------------------------------------------------------------------
def _cache_get(key):
    return _CACHE.get(key)


def _cache_put(key, value):
    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.clear()
    _CACHE[key] = value


def group_evidence_profile(store, package_id, group_id, revision):
    """返回 (profiles, templates)，按（组 + 成员修订号 + 策略版本）缓存。"""
    key = (str(store.db_path), str(package_id), str(group_id), int(revision or 0), feature_store.snapshot_key(store,package_id), POLICY_VERSION)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    profiles = group_profiles(store, package_id, group_id)
    templates = template_terms([p['text'] for p in profiles]) if profiles else []
    value = (profiles, templates)
    _cache_put(key, value)
    return value


def group_templates(store, package_id, group_id, revision):
    """只要模板词典（命名路径用）。"""
    return group_evidence_profile(store, package_id, group_id, revision)[1]


def clear_cache():
    _CACHE.clear()
    feature_store.clear_cache()
