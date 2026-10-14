"""L1 取文改造的回归测试：模板语言剥离、正文质量闸门、状态与名称分离、指纹稳定。

只覆盖确定性代码，不依赖模型服务、网络或生产数据库。
"""
import itertools
import json
import sqlite3
import time
import threading
import unittest
from unittest.mock import patch

import numpy as np

from services import large_package_catalog as catalog
from services import large_package_semantic as semantic
from services import large_package_catalog_naming as naming
from services import large_package_catalog_profile as profile
from services import large_package_evidence_store as feature_store


class StripMarkupTests(unittest.TestCase):
    def test_html_source_is_reduced_to_text(self):
        html = ('<!DOCTYPE html><html><head><title>标题</title><style>a{color:red}</style></head>'
                '<body><div class="x">数据库事务隔离与并发控制</div><p>两阶段锁协议</p></body></html>')
        out = catalog.strip_markup(html)
        self.assertIn('数据库事务隔离与并发控制', out)
        self.assertIn('两阶段锁协议', out)
        self.assertNotIn('<div', out)
        self.assertNotIn('color:red', out)

    def test_plain_text_is_untouched(self):
        text = '普通正文，没有任何标签'
        self.assertEqual(catalog.strip_markup(text), text)

    def test_low_tag_density_is_not_treated_as_markup(self):
        text = '不等式 a < b > c 的写法，以及 x<y 的比较'
        self.assertEqual(catalog.strip_markup(text), text)


class EvidenceQualityTests(unittest.TestCase):
    def test_normal_text_is_usable(self):
        ok, reason = semantic.evidence_quality({'text': '数据库事务隔离级别决定并发事务之间的可见性。' * 3})
        self.assertTrue(ok)
        self.assertEqual(reason, '')

    def test_garbled_text_is_rejected(self):
        ok, reason = semantic.evidence_quality({'text': '\u0b80\u0b81\u0b82\u0b83' * 200})
        self.assertFalse(ok)
        self.assertEqual(reason, 'garbled')

    def test_empty_text_without_hints_is_rejected(self):
        ok, reason = semantic.evidence_quality({'text': '短', 'fields': [], 'headings': []})
        self.assertFalse(ok)
        self.assertEqual(reason, 'too_little_text')

    def test_fields_alone_keep_a_file_usable(self):
        ok, _ = semantic.evidence_quality({'text': '短', 'fields': ['销售额'], 'headings': []})
        self.assertTrue(ok)


class ValidateStatusTests(unittest.TestCase):
    EVIDENCE = [
        {'id': 'E1', 'text': ('事务隔离级别决定了并发事务之间的可见性，可重复读通过多版本并发控制'
                              '避免不可重复读，串行化通过加锁保证可串行化调度。') * 3,
         'headings': ['数据库系统'], 'fields': ['隔离级别'], 'keywords': ['事务']},
        {'id': 'E2', 'text': ('两阶段锁协议保证冲突可串行化，死锁检测依赖等待图；分布式事务使用'
                              '两阶段提交，协调者故障时参与者可能阻塞。') * 3,
         'headings': ['并发控制'], 'fields': ['两阶段锁'], 'keywords': ['死锁']},
    ]

    def _validate(self, name, ids=('E1', 'E2'), evidence=None):
        body = {'name': name, 'description': '共同范围说明文字，用于通过长度校验。',
                'evidence_ids': list(ids), 'outliers': [], 'needs_review': False,
                'purity': 'same'}
        return naming.validate(json.dumps(body, ensure_ascii=False),
                               {'evidence': evidence if evidence is not None else self.EVIDENCE})

    def test_placeholder_becomes_status_not_name(self):
        for raw in ('无共同主题', '缺乏共同主题的杂乱文本片段集合', '需审查：内容杂乱无共同主题'):
            out = self._validate(raw)
            self.assertEqual(out['naming_status'], 'mixed')
            self.assertEqual(out['name'], naming.STATUS_NAMES['mixed'])
            self.assertTrue(out['needs_review'])
            self.assertEqual(out['model_name'], raw)

    def test_pure_generic_name_becomes_status(self):
        out = self._validate('资料汇编')
        self.assertEqual(out['naming_status'], 'generic')
        self.assertEqual(out['name'], naming.STATUS_NAMES['generic'])
        self.assertTrue(out['needs_review'])

    def test_specific_name_stays_coherent(self):
        out = self._validate('数据库事务隔离与并发控制')
        self.assertEqual(out['naming_status'], 'coherent')
        self.assertEqual(out['name'], '数据库事务隔离与并发控制')

    def test_thin_evidence_keeps_the_model_name_but_flags_review(self):
        # 2026-09-20 改：引用证据短**不再**丢掉模型给的名字。
        # 旧行为把 9 个组改成同一个状态名，制造了同名条目并扔掉可用名字（实测）。
        out = self._validate('某某主题研究', ids=('E1',),
                             evidence=[{'id': 'E1', 'text': '短', 'headings': []}])
        self.assertEqual(out['naming_status'], 'thin_evidence')
        self.assertEqual(out['name'], '某某主题研究', '名字要保留，不能被状态名顶掉')
        self.assertTrue(out['needs_review'], '证据偏短必须让人看一眼')

    def test_no_evidence_text_at_all_still_degrades_to_a_status_name(self):
        out = self._validate('某某主题研究', ids=('E1',),
                             evidence=[{'id': 'E1', 'text': '', 'headings': []}])
        self.assertEqual(out['naming_status'], 'insufficient_evidence')
        self.assertEqual(out['name'], naming.STATUS_NAMES['insufficient_evidence'])

    def test_identifier_must_come_from_cited_evidence(self):
        # 编号只能来自**被引用**的证据：E3 里有 SEV-SNP，但本次只引用了 E1。
        # 2026-09-20 改了失败方式（见 naming.validate 的注释）：不再整组作废，而是把
        # 这个编号从名字/描述里删掉并标复核——目录里照样不会出现无法核对的编号，
        # 但这一组仍然有名字（整组作废 + 重试同一提示 = 永远没有名字）。
        evidence = self.EVIDENCE + [{'id': 'E3', 'text': 'AMD SEV-SNP 内存一致性漏洞分析 2024。', 'headings': []}]
        out = self._validate('SEV-SNP内存一致性分析', ids=('E1',), evidence=evidence)
        self.assertNotIn('SEV-SNP', out['name'])
        self.assertTrue(out['needs_review'])

    def test_generic_word_is_flagged_but_name_is_kept(self):
        out = self._validate('就业与消费政策汇编')
        self.assertEqual(out['naming_status'], 'coherent')
        self.assertEqual(out['name'], '就业与消费政策汇编')
        self.assertTrue(out['needs_review'])

    def test_long_enumeration_is_flagged_for_review(self):
        # §4.6：顿号 ≥3 的名称在罗列而不是概括 → 复核，但保留名称（不硬拒）。
        out = self._validate('就业政策、消费升级、服务业资金、私募监管')
        self.assertEqual(out['naming_status'], 'coherent')
        self.assertTrue(out['needs_review'])
        # 一到两个顿号是正常的层次表达，不该被当成清单。
        self.assertFalse(self._validate('湿地生态特征、鸟类栖息地及保护名录')['needs_review'])
        out2 = self._validate('数据库事务隔离与并发控制')
        self.assertFalse(out2['needs_review'])

    def test_status_name_is_recognised(self):
        self.assertTrue(naming.is_status_name(naming.STATUS_NAMES['mixed']))
        self.assertTrue(naming.is_status_name(naming.STATUS_NAMES['generic']))
        self.assertFalse(naming.is_status_name('数据库事务隔离与并发控制'))


class MaterialDigestTests(unittest.TestCase):
    @staticmethod
    def _row(keywords):
        return {'sensitive': 0, 'deep_status': 'idle', 'deep_summary': {},
                'local_summary': {'keywords': keywords,
                                  'portrait': {'evidence': [{'kind': 'text', 'text': '正文内容' * 120}],
                                               'fields': ['字段A'], 'headings': ['标题A']}}}

    def test_keywords_are_exposed_to_naming(self):
        value, _ = catalog.material(self._row(['关键词甲', '关键词乙']))
        self.assertEqual(value['keywords'], ['关键词甲', '关键词乙'])

    def test_vector_fingerprint_ignores_keywords(self):
        _, first = catalog.material(self._row(['甲']))
        _, second = catalog.material(self._row(['完全不同的关键词']))
        self.assertEqual(first, second)


class GroupGeometryTests(unittest.TestCase):
    @staticmethod
    def _conn():
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        conn.execute('CREATE TABLE large_catalog_members('
                     'package_id TEXT, group_id TEXT, path TEXT, vector BLOB)')
        return conn

    @staticmethod
    def _vector(*values):
        return np.asarray(values, dtype=np.float32).tobytes()

    def test_centre_and_representative_come_from_members(self):
        conn = self._conn()
        conn.executemany('INSERT INTO large_catalog_members VALUES(?,?,?,?)', [
            ('p', 'g', 'a.txt', self._vector(1.0, 0.0)),
            ('p', 'g', 'b.txt', self._vector(0.98, 0.02)),
            ('p', 'g', 'c.txt', self._vector(0.0, 1.0)),
        ])
        anchor, representative = naming._group_geometry(conn, 'p', 'g')
        self.assertIsNotNone(anchor)
        vector = np.frombuffer(anchor, dtype=np.float32)
        self.assertAlmostEqual(float(np.linalg.norm(vector)), 1.0, places=5)
        self.assertIn(representative, {'a.txt', 'b.txt'})

    def test_group_without_vectors_returns_none(self):
        conn = self._conn()
        conn.execute('INSERT INTO large_catalog_members VALUES(?,?,?,?)', ('p', 'g', 'a.txt', b''))
        self.assertEqual(naming._group_geometry(conn, 'p', 'g'), (None, None))


class TemplateDetectionTests(unittest.TestCase):
    HEADER = '人工智能与机器学习 工程技术中心 编号 摘要 本文汇编 篇资料 目录 条目'
    TOPICS = ['数据库事务隔离与并发控制', '蛋白质折叠与分子动力学模拟', '城市轨道交通线网规划',
              '湿地生态修复与鸟类栖息地', '方言音系的历史演变', '锂离子电池热失控机理',
              '供应链金融的风险定价', '儿童语言习得的韵律线索', '古建筑木构的抗震性能',
              '海洋微塑料的迁移路径']

    def test_cross_file_header_is_detected(self):
        texts = [self.HEADER + ' 第%d篇：%s的具体研究。' % (i, self.TOPICS[i]) for i in range(len(self.TOPICS))]
        terms = profile.template_terms(texts)
        self.assertTrue(terms, '跨文件重复的页眉必须被识别为模板')
        self.assertTrue(any('工程技术中心' in t for t in terms))

    def test_term_repeated_within_one_file_is_not_a_template(self):
        texts = [('销售额 ' * 300)] + ['第%d份材料正文' % i for i in range(1, 10)]
        self.assertEqual(profile.template_terms(texts), [])

    def test_distinct_texts_have_no_templates(self):
        texts = [self.TOPICS[i] + '的第%d项独立结论。' % i for i in range(len(self.TOPICS))]
        self.assertEqual(profile.template_terms(texts), [])

    def test_two_files_are_not_enough_evidence(self):
        self.assertEqual(profile.template_terms([self.HEADER, self.HEADER]), [])

    def test_strip_templates_removes_only_the_template(self):
        text = self.HEADER + ' 正文：事务隔离级别与两阶段锁协议。'
        cleaned = profile.strip_templates(text, [self.HEADER])
        self.assertNotIn('工程技术中心', cleaned)
        self.assertIn('事务隔离级别', cleaned)

    def test_strip_templates_ignores_whitespace_differences(self):
        # 实测缺陷：模板短语是在画像文本上抽取的，证据文本经过 clean/clip 后
        # 空白可能不同（差一个空格），精确子串匹配会全部落空。
        term = '数据库与数据工程 工程技术中心 编号 摘要 本文汇编 篇资料。 目录 '
        text = '数据库与数据工程 工程技术中心 编号 摘要 本文汇编 篇资料。 目录 正文：事务隔离级别。'
        cleaned = profile.strip_templates(text, [term])
        self.assertNotIn('工程技术中心', cleaned)
        self.assertIn('事务隔离级别', cleaned)

    def test_template_metadata_is_filtered(self):
        header = '数据库与数据工程 工程技术中心 编号 摘要 本文汇编 篇资料，围绕数据库相关内容进行整理。'
        self.assertTrue(profile.is_template_meta('工程技术中心', [header]))
        self.assertTrue(profile.is_template_meta('摘要', [header]))
        self.assertTrue(profile.is_template_meta('数据来源', []))
        self.assertFalse(profile.is_template_meta('事务隔离级别', [header]))
        kept = profile.filter_meta(['工程技术中心', '摘要', '事务隔离级别', '事务隔离级别'], [header], 5)
        self.assertEqual(kept, ['事务隔离级别'])

    def test_strip_templates_without_terms_is_a_no_op(self):
        text = self.HEADER + ' 正文内容'
        self.assertEqual(profile.strip_templates(text, []), text)


class _FakeConn(object):
    def __init__(self, real):
        self._real = real

    def execute(self, *args, **kwargs):
        return self._real.execute(*args, **kwargs)

    def close(self):
        # Keep the single in-memory SQLite connection alive between calls.
        pass

    def __getattr__(self, name):
        return getattr(self._real, name)


class _FakeStore(object):
    """Minimal in-memory store with the schema used by feature extraction."""

    def __init__(self):
        self.db_path = ':memory:catalog-profile-test'
        self._lock = threading.RLock()
        self.real = sqlite3.connect(':memory:')
        self.real.row_factory = sqlite3.Row
        self.real.execute('CREATE TABLE packages(id TEXT PRIMARY KEY)')
        self.real.execute(
            'CREATE TABLE files(package_id TEXT, path TEXT, local_summary TEXT, '
            'deep_summary TEXT, deep_status TEXT, sensitive INTEGER, size INTEGER, '
            'modified_at_ns INTEGER, updated_at REAL, PRIMARY KEY(package_id,path))')
        self.real.execute(
            'CREATE TABLE large_catalog_members(package_id TEXT, group_id TEXT, path TEXT)')
        self.real.execute(
            'CREATE TABLE large_catalog_groups(package_id TEXT, id TEXT, content_revision INTEGER, updated_at REAL)')
        self.real.execute(
            'CREATE TABLE lp_catalog_rounds(id TEXT, package_id TEXT, started REAL)')
        self.real.execute('INSERT INTO packages VALUES(?)', ('p',))
        feature_store.init(self.real)
        self.real.commit()

    def add_group(self, group_id, revision=1, updated=0.0):
        self.real.execute('INSERT INTO large_catalog_groups VALUES(?,?,?,?)',
                          ('p', group_id, revision, updated))
        self.real.commit()

    def add(self, path, evidence_text, keywords=None):
        self.real.execute('INSERT INTO large_catalog_members VALUES(?,?,?)', ('p', 'g', path))
        payload = json.dumps({'keywords': keywords or [],
                              'portrait': {'evidence': [{'kind': 'text', 'text': evidence_text}],
                                           'headings': ['标题'], 'fields': ['字段']}},
                             ensure_ascii=False)
        self.real.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)',
                          ('p', path, payload, '{}', 'idle', 0, len(evidence_text), 0, time.time()))
        self.real.commit()

    def _connect(self):
        return _FakeConn(self.real)

    def _row(self, raw):
        row = dict(raw)
        for field in ('local_summary', 'deep_summary'):
            if isinstance(row.get(field), str):
                row[field] = json.loads(row[field])
        return row


class GroupProfileTests(unittest.TestCase):
    def setUp(self):
        profile.clear_cache()

    def test_profiles_carry_text_and_structured_fields(self):
        store = _FakeStore()
        store.add('a.txt', '数据库事务隔离级别决定并发可见性。', ['事务', '隔离级别'])
        got = profile.group_profiles(store, 'p', 'g')
        self.assertEqual(len(got), 1)
        self.assertIn('数据库事务', got[0]['text'])
        self.assertEqual(got[0]['keywords'], ['事务', '隔离级别'])

    def test_result_is_cached_by_revision(self):
        store = _FakeStore()
        store.add('a.txt', '数据库事务隔离级别决定并发可见性。')
        first = profile.group_evidence_profile(store, 'p', 'g', 7)
        store.add('b.txt', '另一份材料讲的是别的内容。')
        second = profile.group_evidence_profile(store, 'p', 'g', 7)
        self.assertIs(first, second, '同一修订号必须复用缓存')
        third = profile.group_evidence_profile(store, 'p', 'g', 8)
        self.assertEqual(len(third[0]), 2, '修订号变化后应重新读取')


class ProfileTermTests(unittest.TestCase):
    def test_document_frequency_beats_single_file_repetition(self):
        texts = ['数据库事务隔离与并发控制的具体做法。' + '销售额 ' * 200,
                 '数据库事务隔离与并发控制的工程取舍。',
                 '数据库事务隔离与并发控制的测试方法。']
        out = profile.ctfidf_terms(texts, {'files': 100, 'df': {'数据库': 20}})
        terms = [x['term'] for x in out['distinctive']]
        self.assertTrue(terms)
        self.assertTrue(any('数据库' in t for t in terms), '被 3 份文档支撑的短语必须入选')
        self.assertFalse(any('销售额' in t for t in terms), '单篇重复 200 次的词不算共同主题')

    def test_template_phrases_are_excluded_from_the_profile(self):
        header = '人工智能与机器学习 工程技术中心 编号 摘要 本文汇编 篇资料'
        texts = [header + ' 关于%s的讨论。' % topic
                 for topic in ('事务隔离', '并发控制', '锁协议', '多版本')]
        out = profile.ctfidf_terms(texts, {'files': 10, 'df': {}}, [header])
        joined = ' '.join(x['term'] for x in out['distinctive'])
        self.assertNotIn('工程技术中心', joined)

    def test_unsupported_phrases_are_not_reported(self):
        out = profile.ctfidf_terms(['数据库事务隔离级别', '海洋珊瑚白化机理研究'],
                                   {'files': 10, 'df': {}})
        self.assertEqual(out['distinctive'], [])

    def test_background_frequency_lowers_the_score(self):
        texts = ['常见短语在甲文出现。', '常见短语在乙文出现。', '常见短语在丙文出现。',
                 '罕见短语在丁文出现。', '罕见短语在戊文出现。', '罕见短语在己文出现。']
        # 背景里 30% 出现：还没到"整包套话"的 50% 门槛，但分数应明显低于背景罕见的词。
        out = profile.ctfidf_terms(texts, {'files': 100, 'df': {'常见短语': 30}})
        scores = {}
        for item in out['distinctive']:
            if '常见短语' in item['term']:
                scores['common'] = item['score']
            if '罕见短语' in item['term']:
                scores['rare'] = item['score']
        self.assertIn('common', scores)
        self.assertIn('rare', scores)
        self.assertGreater(scores['rare'], scores['common'],
                           '同样被支撑的短语，背景里越常见越不该成为本组的区分性短语')

    def test_common_and_minority_are_split_by_support(self):
        texts = (['共同主题甲共同主题甲共同主题甲少数子题乙。'] * 4
                 + ['共同主题甲共同主题甲共同主题甲另种子题丙。'] * 2)
        out = profile.ctfidf_terms(texts, {'files': 20, 'df': {}})
        self.assertTrue(out['common'])
        self.assertTrue(all(x['df'] >= 4 for x in out['common']))
        self.assertEqual(out['sampled'], 6)


class GroupProfileEntryTests(unittest.TestCase):
    def setUp(self):
        profile.clear_cache()

    def test_group_profile_shape_and_cache(self):
        store = _FakeStore()
        for i in range(4):
            store.add('doc%d.txt' % i, '数据库事务隔离与并发控制要点之%d，系统性能测评。' % i)
        first = profile.group_profile(store, 'p', 'g', 3)
        self.assertIn('distinctive', first)
        self.assertIn('templates', first)
        self.assertEqual(first['sampled'], 4)
        self.assertIs(first, profile.group_profile(store, 'p', 'g', 3))

    def test_empty_group_profile_is_safe(self):
        store = _FakeStore()
        out = profile.group_profile(store, 'p', 'g', 1)
        self.assertEqual(out['distinctive'], [])
        self.assertEqual(out['sampled'], 0)


class MetaTemplateTests(unittest.TestCase):
    @staticmethod
    def _profiles(keywords_list):
        return [{'path': 'd%d' % i, 'text': '正文', 'keywords': kw, 'headings': [], 'fields': []}
                for i, kw in enumerate(keywords_list)]

    def test_boilerplate_needs_background_support_too(self):
        profiles = self._profiles([['工程技术中心']] * 4)
        background = {'files': 100, 'df': {}, 'meta_df': {'工程技术中心': 90}}
        self.assertIn('工程技术中心', profile.meta_templates(profiles, background))

    def test_group_theme_is_not_boilerplate(self):
        profiles = self._profiles([['人工智能与机器学习']] * 4)
        background = {'files': 100, 'df': {}, 'meta_df': {'人工智能与机器学习': 3}}
        self.assertNotIn('人工智能与机器学习', profile.meta_templates(profiles, background))

    def test_small_background_disables_the_judgement(self):
        profiles = self._profiles([['工程技术中心']] * 4)
        background = {'files': 3, 'df': {}, 'meta_df': {'工程技术中心': 3}}
        self.assertEqual(profile.meta_templates(profiles, background), [])

    def test_corpus_wide_phrases_are_dropped_from_the_profile(self):
        texts = ['工程技术中心甲', '工程技术中心乙', '事务隔离级别丙', '事务隔离级别丁']
        background = {'files': 100, 'df': {'工程技术': 90, '工程技术中心': 90}, 'meta_df': {}}
        out = profile.ctfidf_terms(texts, background)
        terms = [x['term'] for x in out['distinctive']]
        # 真正要守的：整包套话被剔除，本组特有内容保留。
        # （合并短语的确切长度取决于哪些子串仍有支撑，因此断言"包含"而不是等值。）
        self.assertTrue(any('事务隔离' in t for t in terms))
        self.assertFalse(any('工程技术中心' in t for t in terms))

    def test_phrases_do_not_cross_segment_boundaries(self):
        # 语料的证据文本是"条目/表格行"用空格拼起来的；跨段拼接会造出
        # ' 表 项目 数量 备注 ' 这类假短语，还把相邻两个不相干的条目粘成一个。
        texts = ['关系型数据流管理系统 联合国人权事务委员会 表 项目 数量 备注 条目'] * 4
        out = profile.ctfidf_terms(texts, {'files': 10, 'df': {}, 'meta_df': {}})
        terms = [x['term'] for x in out['distinctive']]
        self.assertTrue(terms)
        self.assertTrue(all(' ' not in t for t in terms), '短语不得跨段拼接')
        self.assertTrue(any('关系型数据流管理系统' in t for t in terms))

    def test_template_vocabulary_expands_cross_segment_phrases(self):
        vocab = profile.template_vocabulary(['数据库与数据工程 工程技术中心 编号 摘要 本文汇编'])
        self.assertIn('工程技术中心', vocab)
        self.assertIn('数据库与数据工程', vocab)
        self.assertNotIn('编号', vocab, '纯结构性词不必进词表')

    def test_template_segment_terms_are_excluded_from_the_profile(self):
        header = '数据库与数据工程 工程技术中心 编号 摘要 本文汇编 篇资料'
        texts = [header + ' 事务隔离级别与并发控制' for _ in range(4)]
        out = profile.ctfidf_terms(texts, {'files': 10, 'df': {}, 'meta_df': {}}, [header])
        terms = [x['term'] for x in out['distinctive']]
        self.assertFalse(any('工程技术中心' in t for t in terms),
                         '页眉拆出的段不能当成主题词捡回来')

    def test_english_stopwords_are_not_profile_terms(self):
        texts = ['that here with from work role data point model'] * 3 + ['transaction isolation control'] * 3
        out = profile.ctfidf_terms(texts, {'files': 20, 'df': {}, 'meta_df': {}})
        terms = [x['term'] for x in out['distinctive']]
        for stop in ('that', 'here', 'with', 'from'):
            self.assertFalse(any(t == stop for t in terms), '%s 是功能词，不该当主题' % stop)

    def test_latin_fragments_are_not_units(self):
        # 拉丁文只取整词：'Wikipedia' 不该被切成 'edia'/'Wiki' 这类碎片。
        units = profile._units('Wikipedia is a free encyclopedia', profile.PROFILE_NGRAM[0])
        self.assertIn('wikipedia', units)
        self.assertNotIn('edia', units)
        self.assertNotIn('kip', units)

    def test_short_cjk_fragments_need_more_support(self):
        # 二字残片（'于国'、'则第'）只在 2 份文档出现时不应入选。
        texts = ['于国务院第某次会议', '于国某项政策说明', '完全不同的内容甲', '完全不同的内容乙']
        out = profile.ctfidf_terms(texts, {'files': 20, 'df': {}, 'meta_df': {}})
        self.assertFalse(any(x['term'] == '于国' for x in out['distinctive']))

    def test_long_latin_words_and_phrases_survive_the_merge(self):
        # 旧实现的合并在段内只尝试 2–4 字符窗口，于是 'learning'、'memory'
        # 这类 7–8 字母的词永远进不了画像，只剩 'role'、'care'、'data'。
        texts = (['machine learning models for clinical prediction'] * 3
                 + ['working memory and neural network analysis'] * 3)
        out = profile.ctfidf_terms(texts, {'files': 20, 'df': {}, 'meta_df': {}})
        terms = ' | '.join(x['term'] for x in out['distinctive'])
        self.assertIn('machine learning', terms)
        self.assertIn('prediction', terms)

    def test_latin_phrases_require_real_adjacency(self):
        # 过滤掉短词后不能拼假短语：'state of the art model' 不该产出 'state model'。
        units = profile._units('state of the art model', profile.PROFILE_NGRAM[0])
        self.assertIn('state', units)
        self.assertIn('model', units)
        self.assertNotIn('state model', units)
        self.assertNotIn('of the', units)

    def test_latin_phrases_with_stopwords_are_skipped(self):
        units = profile._units('training with models', profile.PROFILE_NGRAM[0])
        self.assertIn('training', units)
        self.assertIn('models', units)
        self.assertNotIn('training with', units)

    def test_latin_adjacent_words_do_form_phrases(self):
        units = profile._units('machine learning models', profile.PROFILE_NGRAM[0])
        self.assertIn('machine learning', units)
        self.assertIn('machine learning models', units)

    def test_academic_boilerplate_is_not_a_profile_term(self):
        # 实测论文组的画像曾被许可声明占满；统计规则区分不了"样板集中在同源论文里"
        # 与"主题词集中在本组"，只能靠词表清掉。
        for value in ('perpetuity', 'granted', 'licenseavailab', 'authorscontrib',
                      'correspondence', 'university', 'preprintthis'):
            self.assertTrue(profile._latin_boilerplate(value), value)
        self.assertFalse(profile._latin_boilerplate('neural network'))
        self.assertFalse(profile._latin_boilerplate('working memory'))

    def test_boilerplate_phrases_are_dropped_from_terms(self):
        texts = (['granted perpetuity license authors contributions correspondence'] * 3
                 + ['neural network decoding of working memory'] * 3)
        out = profile.ctfidf_terms(texts, {'files': 20, 'df': {}, 'meta_df': {}})
        terms = ' | '.join(x['term'] for x in out['distinctive'])
        self.assertIn('neural network', terms)
        self.assertNotIn('perpetuity', terms)

    def test_pure_structural_phrases_are_dropped(self):
        self.assertTrue(profile._structural_phrase('公开资料整理'))
        self.assertTrue(profile._structural_phrase('数据来源'))
        self.assertFalse(profile._structural_phrase('事务隔离级别'))


class EvidenceSelectionTests(unittest.TestCase):
    """L3：MMR + 强制子区域覆盖 + 自适应样本数。"""

    @staticmethod
    def _matrix(seed=7):
        rng = np.random.default_rng(seed)
        base = np.eye(3, dtype=np.float32)
        rows, groups = [], []
        for label, (vec, count) in enumerate([(base[0], 10), (base[1], 6), (base[2], 2)]):
            for _ in range(count):
                v = vec + rng.normal(scale=0.05, size=3).astype(np.float32)
                rows.append(v / np.linalg.norm(v))
                groups.append(label)
        matrix = np.vstack(rows)
        anchor = matrix.mean(axis=0)
        anchor = anchor / np.linalg.norm(anchor)
        return matrix, matrix @ anchor, np.asarray(groups)

    def test_selection_covers_every_subregion(self):
        matrix, relevance, groups = self._matrix()
        picked = naming.select_evidence(matrix, relevance, 6, 0.7)
        self.assertEqual({int(groups[i]) for i in picked}, {0, 1, 2},
                         '每个子区域都必须至少选到一份，少数派不能被挤掉')

    def test_first_pick_is_the_centre(self):
        matrix, relevance, _ = self._matrix()
        picked = naming.select_evidence(matrix, relevance, 6, 0.7)
        self.assertEqual(picked[0], int(np.argmax(relevance)))

    def test_boundary_is_kept_as_a_challenge(self):
        matrix, relevance, _ = self._matrix()
        picked = naming.select_evidence(matrix, relevance, 6, 0.7)
        self.assertIn(int(np.argmin(relevance)), picked)

    def test_selection_is_unique_and_complete(self):
        matrix, relevance, _ = self._matrix()
        picked = naming.select_evidence(matrix, relevance, 9, 0.7)
        self.assertEqual(len(picked), len(set(picked)))
        self.assertEqual(len(picked), 9)

    def test_small_group_returns_every_member(self):
        matrix, relevance, _ = self._matrix()
        self.assertEqual(len(naming.select_evidence(matrix, relevance, 999, 0.7)), len(relevance))

    def test_adaptive_count_follows_heterogeneity(self):
        self.assertEqual(naming.desired_sample_count(0.97, 6, 14), 6)
        self.assertEqual(naming.desired_sample_count(0.92, 6, 14), 10)
        self.assertEqual(naming.desired_sample_count(0.85, 6, 14), 14)
        self.assertEqual(naming.desired_sample_count(None, 6, 14), 14)

    def test_adaptive_count_prefers_the_package_baseline(self):
        # 同一份语料里"基线本来就高"时要按相对量判：cssj1 的组内余弦 0.97
        # 如果基线只有 0.90，那它在**这份语料里**偏高，给最少样本即可。
        self.assertEqual(naming.desired_sample_count(0.97, 6, 14, baseline=0.90), 6)
        self.assertEqual(naming.desired_sample_count(0.97, 6, 14, baseline=0.96), 10)
        self.assertEqual(naming.desired_sample_count(0.97, 6, 14, baseline=0.99), 14)
        # 有基线时不再看绝对阈值：0.85 在基线 0.78 的语料里其实偏同质（差 +0.07）。
        self.assertEqual(naming.desired_sample_count(0.85, 6, 14, baseline=0.78), 6)

    def test_mmr_weight_changes_the_pick(self):
        matrix, relevance, _ = self._matrix()
        relevance_first = naming.select_evidence(matrix, relevance, 4, 1.0)   # 只看相关性
        diversity_first = naming.select_evidence(matrix, relevance, 4, 0.1)  # 只看多样性
        self.assertNotEqual(relevance_first, diversity_first)


class NamingCostAccountTests(unittest.TestCase):
    """账①（消除重复命名 / 结构维护门控）与账②（命名并发路数）。"""

    def setUp(self):
        naming._MAINTENANCE_STATE.clear()

    def test_bundle_key_is_stable_and_revision_scoped(self):
        first = naming.bundle_key('p', 'g', 7, 'owner')
        self.assertEqual(first, naming.bundle_key('p', 'g', 7, 'owner'))
        self.assertNotEqual(first, naming.bundle_key('p', 'g', 8, 'owner'))
        self.assertNotEqual(first, naming.bundle_key('p', 'other', 7, 'owner'))
        self.assertNotEqual(first, naming.bundle_key('p', 'g', 7, 'another-owner'))

    def test_bundle_key_has_no_neighbour_input(self):
        # 键的入参里没有邻组名：相邻组改名不会让本组缓存失效（2.1 次/组的主因）。
        import inspect
        params = list(inspect.signature(naming.bundle_key).parameters)
        self.assertEqual(params, ['package_id', 'group_id', 'revision', 'owner_id'])

    def test_maintenance_runs_once_per_signature(self):
        store = _FakeStore()
        store.add_group('g1', 1, 100.0)
        with patch.object(naming, 'duplicate_name_groups', return_value={}), \
                patch.object(naming, 'normalize_leaves'), \
                patch.object(naming, 'prune_history'), \
                patch.object(naming, 'label_singletons'), \
                patch('services.large_package_semantic.split_oversized') as split:
            self.assertTrue(naming.maintain_catalog(store, 'p'))
            self.assertFalse(naming.maintain_catalog(store, 'p'),
                             '目录签名未变时不应再跑一遍结构维护')
            self.assertEqual(split.call_count, 1)

    def test_maintenance_reruns_after_a_structural_change(self):
        store = _FakeStore()
        store.add_group('g1', 1, 100.0)
        with patch.object(naming, 'duplicate_name_groups', return_value={}), \
                patch.object(naming, 'normalize_leaves'), \
                patch.object(naming, 'prune_history'), \
                patch.object(naming, 'label_singletons'), \
                patch('services.large_package_semantic.split_oversized') as split:
            self.assertTrue(naming.maintain_catalog(store, 'p'))
            store.add_group('g2', 1, 200.0)          # 结构变了（多了组）
            naming._MAINTENANCE_STATE.clear()        # 模拟新进程
            self.assertTrue(naming.maintain_catalog(store, 'p'))
            self.assertEqual(split.call_count, 2)

    def test_naming_concurrency_defaults_to_two_and_is_capped(self):
        from config import Config
        self.assertEqual(Config.LP_NAMING_CONCURRENCY, 2)
        self.assertEqual(naming.naming_concurrency(), 2)
        with patch.object(Config, 'LP_NAMING_CONCURRENCY', 9):
            self.assertEqual(naming.naming_concurrency(), 4, '上限 4，避免挤压前台车道')

    def test_client_uses_the_configured_width(self):
        captured = {}

        class _Client(object):
            def __init__(self, *args, **kwargs):
                captured.update(kwargs)

        from config import Config
        with patch.object(Config, 'LLM_BACKEND', 'vllm'), \
                patch.object(Config, 'ENABLE_VLLM', True), \
                patch('services.vllm.VLLMClient', _Client):
            naming.client()
        self.assertEqual(captured.get('max_concurrency'), naming.naming_concurrency())


class GeometryBackfillTests(unittest.TestCase):
    """L1.4 的历史回填（repair R10）：空质心组要能被**真的**修好。

    生产库里这类组有 193 个（dry-run 实测）。这里在临时库上跑一遍 apply，
    验证：只读模式不改任何东西 → apply 填上质心 → 代表文件是最接近质心的那个。
    """

    def test_r10_fills_empty_anchor_and_repairs_the_representative(self):
        import tempfile
        from pathlib import Path
        from contextlib import closing as _closing
        from services.large_package_isolated import LargePackageStore
        from services import large_package_repair as repair
        from config import Config
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(Config, 'SCAN_ALLOWED_ROOTS', ()):
            root = Path(tmp)
            store = LargePackageStore(root / 'state.db')
            pid = store.create(root)['id']
            with _closing(store._connect()) as c:
                for i in range(3):
                    store.upsert_file(pid, {'path': 'm%d.txt' % i, 'name': 'm%d.txt' % i})
                c.execute('INSERT INTO large_catalog_groups(package_id,id,model,anchor,'
                          'representative,result,status,updated_at,member_count,content_revision) '
                          'VALUES(?,?,?,?,?,?,?,?,?,?)',
                          (pid, 'km-x-s0000', 'fixture', b'', 'm0.txt', '{}', 'named', 0.0, 3, 1))
                for i, vector in enumerate([np.array([1, 0, 0, 0], dtype=np.float32),
                                            np.array([.98, .02, 0, 0], dtype=np.float32),
                                            np.array([0, 1, 0, 0], dtype=np.float32)]):
                    c.execute('INSERT INTO large_catalog_members VALUES(?,?,?,?,?)',
                              (pid, 'm%d.txt' % i, 'km-x-s0000', 'h%d' % i, vector.tobytes()))

            self.assertEqual(repair.r10_group_geometry(store, apply=False)['groups'], 1)
            with _closing(store._connect()) as c:
                before = c.execute('SELECT anchor,representative FROM large_catalog_groups '
                                   'WHERE package_id=? AND id=?', (pid, 'km-x-s0000')).fetchone()
            self.assertEqual(before['anchor'], b'', '只读模式不得修改任何东西')

            done = repair.r10_group_geometry(store, apply=True)
            self.assertEqual(done['fixed'], 1)
            self.assertEqual(done['remaining'], 0)
            with _closing(store._connect()) as c:
                after = c.execute('SELECT anchor,representative FROM large_catalog_groups '
                                  'WHERE package_id=? AND id=?', (pid, 'km-x-s0000')).fetchone()
            self.assertTrue(after['anchor'])
            vector = np.frombuffer(after['anchor'], dtype=np.float32)
            self.assertAlmostEqual(float(np.linalg.norm(vector)), 1.0, places=5)
            self.assertIn(after['representative'], {'m0.txt', 'm1.txt'},
                          '代表文件必须是最接近质心的成员（不是原来的任意一个）')


class ServingHeadroomTests(unittest.TestCase):
    """10 小时目标的两条"按文件数"车道：至少不能把参数卡死在代码里。"""

    def test_preview_concurrency_cap_allows_eight_but_defaults_to_two(self):
        from config import Config
        import importlib, os
        # 默认保持 2（不改现有行为）
        self.assertLessEqual(Config.LP_PREVIEW_CONCURRENCY, 8)
        source = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   'config.py'), encoding='utf-8').read()
        self.assertIn('min(8, _positive_int(os.getenv("SJFX_LP_PREVIEW_CONCURRENCY"), 2))', source,
                      '上限应从 4 放宽到 8，否则 100GB 的预览耗时无法压下来')

    def test_naming_concurrency_cap_is_four(self):
        from config import Config
        self.assertLessEqual(Config.LP_NAMING_CONCURRENCY, 4)
        self.assertGreaterEqual(Config.LP_NAMING_CONCURRENCY, 1)


class FailureHandlingTests(unittest.TestCase):
    """§10：失败分类与释放语义在两条路径上共用一套。"""

    @staticmethod
    def _store():
        store = _FakeStore()
        store.real.execute('CREATE TABLE lp_catalog_jobs('
                           'package_id TEXT, group_id TEXT, revision INTEGER, status TEXT, '
                           'attempts INTEGER, leased REAL, next_at REAL, error TEXT)')
        return store

    def _job(self, store, group='g1', status='running', attempts=0):
        store.real.execute('INSERT INTO lp_catalog_jobs VALUES(?,?,?,?,?,?,?,?)',
                           ('p', group, 1, status, attempts, 0.0, 0.0, ''))

    def _row(self, store, group='g1'):
        return dict(store.real.execute('SELECT * FROM lp_catalog_jobs WHERE group_id=?', (group,)).fetchone())

    def test_classification_covers_the_five_documented_categories(self):
        self.assertEqual(naming.classify_failure(RuntimeError('Connection refused')),
                         naming.FAILURE_UNAVAILABLE)
        self.assertEqual(naming.classify_failure(ValueError('命名结果不是对象')),
                         naming.FAILURE_INVALID_OUTPUT)
        self.assertEqual(naming.classify_failure(ValueError('本轮命名预算已用尽')),
                         naming.FAILURE_BUDGET)
        self.assertEqual(naming.classify_failure(RuntimeError('结果陈旧')), naming.FAILURE_STALE)
        self.assertEqual(naming.classify_failure(RuntimeError('目录命名已暂停')), naming.FAILURE_CANCELLED)
        self.assertEqual(naming.classify_failure(RuntimeError('别的什么')), naming.FAILURE_UNKNOWN)

    def test_unavailable_does_not_burn_attempts_and_uses_long_backoff(self):
        store = self._store(); self._job(store, attempts=3)
        status = naming.release_job(store, 'p', 'g1', 1, attempts=3,
                                    error='Connection refused', category=naming.FAILURE_UNAVAILABLE)
        row = self._row(store)
        self.assertEqual(status, 'deferred')
        self.assertEqual(row['attempts'], 3, '依赖不可用不该递增 attempts')
        self.assertGreater(row['next_at'] - time.time(), 60, '应使用长退避（默认 120s）')
        self.assertIn('依赖不可用', row['error'])

    def test_invalid_output_burns_one_attempt_with_backoff(self):
        store = self._store(); self._job(store, attempts=0)
        naming.release_job(store, 'p', 'g1', 1, attempts=0, error='坏输出',
                           category=naming.FAILURE_INVALID_OUTPUT)
        self.assertEqual(self._row(store)['attempts'], 1)

    def test_plain_release_does_not_change_attempts_or_wait(self):
        store = self._store(); self._job(store, attempts=2)
        naming.release_job(store, 'p', 'g1', 1, attempts=2)
        row = self._row(store)
        self.assertEqual(row['status'], 'queued')
        self.assertEqual(row['attempts'], 2)
        self.assertLessEqual(row['next_at'] - time.time(), 1)

    def test_release_never_overwrites_a_finished_job(self):
        # 保护：只动"本次领取、还没落地"的任务。批量路径的 finally 会对所有领取过的
        # 任务调一次释放，没有这道保护就会把刚发布成功的 completed 打回 queued。
        store = self._store(); self._job(store, status='completed', attempts=0)
        naming.release_job(store, 'p', 'g1', 1, attempts=0)
        self.assertEqual(self._row(store)['status'], 'completed')


class HeldOutSupportTests(unittest.TestCase):
    """第 2 项：留出成员 + L2 画像 + 被引用证据的**软**支撑评分（§4.3–4.6）。

    判据本身在本语料上没有判别力（实测真名 p50 0.143 vs 假名 p90 0.105，50% 真名
    会被误判），所以这一组测试断言的是**行为边界**：只置复核、绝不改写名称、
    绝不硬拒，而且留出成员绝不能进模型 payload、也不能用来放宽编号核对。
    """

    NAME = '数据库事务隔离级别研究'
    # 被引用证据必须够长：校验器有一条"引用证据正文合计 <120 字 → 证据不足"的既有规则，
    # 那是状态判定，会盖过支撑评分（测试要测的是评分本身，所以材料要像真实证据一样长）。
    CITED = ('数据库事务隔离级别决定了并发事务之间的可见性与锁的行为。'
             '本文在相同硬件上对比读未提交、读已提交、可重复读与串行化四档隔离级别，'
             '给出吞吐、锁等待与死锁次数的实测数据，并讨论数据库事务隔离级别在'
             '分布式场景下的实现代价与取舍。实验覆盖三种存储引擎与两类工作负载，'
             '并给出隔离级别与长事务冲突概率之间的定量关系，供选型参考。')
    UNRELATED = ('机器学习模型的训练与推理优化涉及算子融合与量化。'
                 '本文给出端侧部署的延迟与精度实测，讨论剪枝、蒸馏与低比特量化的影响，'
                 '并比较不同推理引擎在移动设备上的内存占用与首帧时间。')

    def _material(self, cited=None, held=None, distinctive=None, common=None):
        return {
            'evidence': [{'id': 'E1', 'source_path': 'a.txt', 'text': cited or self.CITED,
                          'headings': []}],
            'held_out': [{'id': 'H1', 'source_path': 'b.txt', 'text': held or self.UNRELATED}],
            'profile': {'distinctive': [{'term': t, 'df': 3} for t in (distinctive or [])],
                        'common': [{'term': t, 'df': 3} for t in (common or [])],
                        'templates': []},
        }

    def _validate(self, name=None, **kwargs):
        payload = json.dumps({'name': name or self.NAME,
                              'description': '这是一段足够长的内容范围说明。',
                              'evidence_ids': ['E1'], 'purity': 'same'}, ensure_ascii=False)
        return naming.validate(payload, self._material(**kwargs))

    def test_name_only_supported_by_read_evidence_is_flagged_for_review(self):
        # 三条同时成立才会置复核：字面支撑主要来自模型读过的那几份、组里其余成员
        # 几乎不支持、且名称单元完全不在**非空**的 L2 画像里。
        got = self._validate(distinctive=['并发控制与锁机制'])
        self.assertTrue(got['needs_review'], '名称只在模型读过的材料里有支撑 → 必须让人看一眼')
        self.assertEqual(got['name'], self.NAME, '软评分**绝不改写**名称')
        self.assertGreaterEqual(got['support']['cited'], 0.5)
        self.assertEqual(got['support']['profile'], 0.0)
        self.assertLess(got['support']['held_out'], 0.5 * got['support']['cited'])

    def test_empty_profile_alone_does_not_trigger_review(self):
        # 画像为空（模板占满、旧版画像等）时**不判断**：没有画像不等于名称没有支撑，
        # 拿"缺席"当证据正是我们要避免的那种错误。
        got = self._validate()
        self.assertIsNone(got['support']['profile'])
        self.assertFalse(got['needs_review'])

    def test_name_supported_by_the_group_profile_is_not_flagged(self):
        got = self._validate(distinctive=['数据库事务隔离级别'])
        self.assertFalse(got['needs_review'],
                         '名称单元出现在覆盖全组的 L2 画像里 → 有整组支撑，不该打扰复核')

    def test_name_supported_by_held_out_members_is_not_flagged(self):
        got = self._validate(held=self.CITED)
        self.assertFalse(got['needs_review'], '组里其余成员也支持它 → 不该打扰复核')

    def test_cross_language_name_has_no_score_and_is_not_flagged(self):
        got = self._validate(name='Deep Learning Model Compression')
        self.assertIsNone(got['support'], '没有可核对的汉字单元时应当明确返回"不判断"')
        self.assertFalse(got['needs_review'])

    def test_held_out_text_cannot_rescue_an_invented_identifier(self):
        # 编号/术语核对**只认被引用的证据**：留出成员里有这个编号也不算数，
        # 否则加一份对照材料就等于把既有的防编造守卫拆掉了。
        #
        # 2026-09-20 改了**失败方式**：不再整组作废，而是把这个编号从名字/描述里删掉
        # （原因见 naming.validate 里的注释：整组作废 + 重试同一提示 = 永远没有名字，
        # 生产实测 7 组 / 276 个文件因此不进目录）。防编造的性质不变——删完之后
        # 这个名字里就没有无法核对的编号了，而且标了 needs_review。
        material = self._material(held='本文依据 GB-T 12345 标准编写。')
        payload = json.dumps({'name': 'GB-T 12345 标准下的数据库隔离研究',
                              'description': '这是一段足够长的内容范围说明。',
                              'evidence_ids': ['E1']}, ensure_ascii=False)
        got = naming.validate(payload, material)
        # 被删掉的是**编号本身**（`GB-T` 命中 [A-Z]{2,}… 规则）。裸数字 `12345` 不在
        # 编号规则覆盖范围内（规则只认年份/缩写/字母-数字组合），这一点与改动前一致。
        self.assertNotIn('GB-T', got['name'], '留出成员里的编号不能成为名字的一部分')
        self.assertTrue(got['needs_review'])

    def test_support_units_tolerate_rewriting_but_are_absent_in_unrelated_text(self):
        self.assertGreater(naming.name_support('数据库事务隔离级别研究', self.CITED), 0.5)
        self.assertEqual(naming.name_support('数据库事务隔离级别研究', self.UNRELATED), 0.0)
        self.assertIsNone(naming.name_support('数据库事务隔离级别研究', ''))
        self.assertIsNone(naming.name_support('Machine Learning', self.CITED))

    def test_bundle_provides_held_out_members_that_never_enter_the_payload(self):
        import tempfile
        from contextlib import closing as _closing
        from pathlib import Path
        from services.large_package_isolated import LargePackageStore
        from config import Config
        # The twenty-member evidence fixture assumes a service capable of the
        # complete naming contract, independently of small-context rejection.
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(Config, 'SCAN_ALLOWED_ROOTS', ()), \
                patch.object(Config, 'LLM_CONTEXT_TOKENS', 32768):
            root = Path(tmp)
            store = LargePackageStore(root / 'state.db')
            pid = store.create(root)['id']
            with _closing(store._connect()) as c:
                naming.init(c)
                # 每份文件的内容必须**各不相同**：模板检测会把 ≥30% 成员共享的 12 字
                # 重复串当页眉剔掉，一模一样的正文会被整段剥光，最后"没有内容证据"
                # （第一版测试正是这样失败的——也顺带证明模板过滤在工作）。
                # 成员数要**大于证据样本上限（14）**，否则全部成员都参与起草、
                # 根本没有"留出成员"可对照（生产上小组就是这种情况，评分会跳过）。
                count = 20
                for i in range(count):
                    path = 'r%02d.txt' % i
                    store.upsert_file(pid, {'path': path, 'name': path})
                    text = ('本文第%02d节讨论数据库事务隔离级别的实现细节与实测数据。'
                            '本份材料的独有内容是编号 %d 的场景记录。' % (i, 1000 + i))
                    summary = {'portrait': {'evidence': [
                        {'kind': 'text', 'location': '正文', 'text': text}],
                        'headings': ['标题'], 'fields': ['字段']}}
                    c.execute('UPDATE files SET local_summary=? WHERE package_id=? AND path=?',
                              (json.dumps(summary, ensure_ascii=False), pid, path))
                c.execute('INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,'
                          'result,status,updated_at,member_count,content_revision) '
                          'VALUES(?,?,?,?,?,?,?,?,?,?)',
                          (pid, 'km-aaa-00001', 'fixture', b'', 'r00.txt', '{}', 'named', 0.0, 0, 1))
                for i in range(count):
                    vector = np.array([1, .02 * i, 0, 0], dtype=np.float32)
                    vector = vector / np.linalg.norm(vector)
                    c.execute('INSERT INTO large_catalog_members VALUES(?,?,?,?,?)',
                              (pid, 'r%02d.txt' % i, 'km-aaa-00001', 'h%02d' % i,
                               vector.tobytes()))
                c.commit()
            material = naming.bundle(store, pid, 'km-aaa-00001')
            held = material.get('held_out') or []
            cited_paths = {e['source_path'] for e in material['evidence']}
            self.assertTrue(held, '必须提供留出成员做对照（成员数大于证据样本上限时）')
            self.assertLessEqual(len(held), 4)
            self.assertFalse({h['source_path'] for h in held} & cited_paths,
                             '留出成员不能同时是被引用的证据')
            self.assertNotIn('held_out', json.loads(material['user']),
                             '留出成员绝不能进模型 payload')
            for snippet in held:
                self.assertNotIn(snippet['text'][:20], material['user'],
                                 '留出成员的正文不得出现在提示词里')
            self.assertIn('profile', material)


class EmbedLaneTests(unittest.TestCase):
    """嵌入车道提速：并发发块 + 批大小/超时可配。

    实测依据（本次基准，生产库真实文本 256 条）：服务端配了 OMP 4 线程 × 准入 2（最多
    8 路），但客户端原来把 32 条切成 4 个 8 条的请求**串行**发，有效并行度只有 2.38；
    并发发块才能用上服务端已有的并行能力。这组测试钉住三件事：切块大小跟着客户端走、
    结果顺序与入参严格一致、**并发是真的**（用栅栏，串行实现会直接失败）。
    """

    _seq = itertools.count()

    def test_chunks_follow_the_client_batch_size(self):
        # 切块只在**并发路径**（给了工厂函数）发生：串行路径整体交给客户端自己切，
        # 免得两边各切一次（见 embed_many 的注释）。
        calls = []

        class FakeClient(object):
            max_batch_size = 8

            def embed(self, texts):
                calls.append(list(texts))
                return [[0.0, 1.0]] * len(texts)

        with patch.object(semantic, 'embed_concurrency', lambda: 2):
            out = semantic.embed_many(FakeClient(), ['t%d' % i for i in range(32)],
                                      factory=FakeClient)
        self.assertEqual(len(out), 32)
        self.assertEqual(sorted(len(c) for c in calls), [8, 8, 8, 8])

    def test_result_order_matches_input_order(self):
        class FakeClient(object):
            max_batch_size = 4

            def embed(self, texts):
                return [[float(t[1:])] for t in texts]

        out = semantic.embed_many(FakeClient(), ['t0', 't1', 't2', 't3', 't4', 't5'])
        self.assertEqual([v[0] for v in out], [0.0, 1.0, 2.0, 3.0, 4.0, 5.0])

    def test_chunks_really_run_concurrently(self):
        import threading
        barrier = threading.Barrier(2, timeout=10)

        class FakeClient(object):
            max_batch_size = 2

            def embed(self, texts):
                barrier.wait()          # 串行实现会在这里超时失败
                return [[0.0]] * len(texts)

        with patch.object(semantic, 'embed_concurrency', lambda: 2):
            out = semantic.embed_many(FakeClient(), ['a', 'b', 'c', 'd'],
                                      factory=FakeClient)
        self.assertEqual(len(out), 4)

    def test_without_a_factory_it_stays_serial_instead_of_faking_concurrency(self):
        # 同一个客户端实例内部是 BoundedSemaphore(1)：多线程发块只会撞它自己的互斥并抛
        # "忙碌"（第一版实测就是这么失败的）。没有工厂就**如实串行**，不起线程池。
        calls = []

        class FakeClient(object):
            max_batch_size = 2

            def embed(self, texts):
                calls.append(list(texts))
                return [[0.0]] * len(texts)

        with patch.object(semantic, 'embed_concurrency', lambda: 4):
            out = semantic.embed_many(FakeClient(), ['a', 'b', 'c', 'd'])
        self.assertEqual(len(out), 4)
        self.assertEqual(calls, [['a', 'b', 'c', 'd']], '应整体交给客户端自己切批')

    def test_each_concurrent_chunk_gets_its_own_client_instance(self):
        import threading
        seen, lock = [], threading.Lock()

        class FakeClient(object):
            max_batch_size = 2

            def __init__(self):
                self.tag = id(self)

            def embed(self, texts):
                with lock:
                    seen.append(self.tag)
                time.sleep(0.02)
                return [[0.0]] * len(texts)

        with patch.object(semantic, 'embed_concurrency', lambda: 2):
            semantic.embed_many(FakeClient(), ['a', 'b', 'c', 'd'], factory=FakeClient)
        self.assertGreaterEqual(len(set(seen)), 2,
                                '两个块必须用不同实例，否则会被客户端互斥串行化')

    def test_single_chunk_keeps_the_serial_path(self):
        seen = []

        class FakeClient(object):
            max_batch_size = 8

            def embed(self, texts):
                seen.append(len(texts))
                return [[0.0]] * len(texts)

        with patch.object(semantic, 'embed_concurrency', lambda: 4):
            semantic.embed_many(FakeClient(), ['a', 'b', 'c'])
        self.assertEqual(seen, [3], '只有一块时不该起线程池')

    def test_empty_input_is_a_no_op(self):
        class FakeClient(object):
            max_batch_size = 8

            def embed(self, texts):
                raise AssertionError('不该被调用')

        self.assertEqual(semantic.embed_many(FakeClient(), []), [])

    def test_chunk_failure_propagates_to_the_shared_failure_path(self):
        class FakeClient(object):
            max_batch_size = 2

            def embed(self, texts):
                raise RuntimeError('Connection refused')

        with self.assertRaises(RuntimeError) as caught:
            semantic.embed_many(FakeClient(), ['a', 'b', 'c', 'd'])
        # 交给上层统一分类：连接类错误必须仍被判成"依赖不可用"（长退避、不烧 attempts）
        self.assertEqual(naming.classify_failure(caught.exception), naming.FAILURE_UNAVAILABLE)

    def test_batch_size_and_timeout_come_from_config_and_are_wired_in(self):
        import inspect
        from config import Config
        self.assertEqual(semantic.embed_batch_size(), Config.VLLM_EMBED_MAX_BATCH_SIZE)
        self.assertGreaterEqual(semantic.embed_timeout(), 30,
                                '嵌入服务冷启动实测 21s，本车道超时必须比共享的 8s 宽')
        source = inspect.getsource(semantic.embedding_client)
        self.assertIn('embed_batch_size()', source, '客户端必须真的用上可配的批大小')
        self.assertIn('embed_timeout()', source, '客户端必须真的用上本车道的超时')
        self.assertEqual(semantic.embed_concurrency(), Config.LP_EMBED_CONCURRENCY)


class EmbeddingBackendSelectionTests(unittest.TestCase):
    """The selected vector space must never switch services after an outage."""

    def test_large_lane_uses_the_shared_factory_with_its_limits(self):
        from config import Config
        selected = object()
        with patch('services.embedding_backend.create_embedding_client', return_value=selected) as factory:
            self.assertIs(semantic.embedding_client(), selected)
        factory.assert_called_once_with(Config, lane='large', timeout=semantic.embed_timeout(),
                                        max_batch_size=semantic.embed_batch_size())

    def test_disabled_backend_ignores_legacy_fallback_configuration(self):
        from config import Config
        with patch.object(Config, 'EMBED_BACKEND', 'disabled'), \
                patch.dict('os.environ', {'SJFX_LP_EMBED_FALLBACK_BASE_URL': 'http://127.0.0.1:9/v1'}), \
                patch('services.embedding_backend.OpenAIEmbeddingClient') as transport:
            self.assertIsNone(semantic.embedding_client())
        transport.assert_not_called()

    def test_primary_outage_propagates_without_backup_requests(self):
        from unittest.mock import Mock
        selected = Mock()
        selected.embed.side_effect = RuntimeError('Connection refused')
        with patch('services.embedding_backend.create_embedding_client', return_value=selected) as factory:
            with self.assertRaises(RuntimeError) as caught:
                semantic.embedding_client().embed(['source passage'])
        factory.assert_called_once()
        self.assertEqual(naming.classify_failure(caught.exception), naming.FAILURE_UNAVAILABLE)
        selected.embed.assert_called_once_with(['source passage'])

    def test_legacy_fallback_never_changes_configured_endpoint(self):
        from config import Config
        with patch.object(Config, 'EMBED_BACKEND', 'openai_compatible'), \
                patch.object(Config, 'EMBED_BASE_URL', 'http://127.0.0.1:18004/v1'), \
                patch.object(Config, 'EMBED_MODEL_PATH', ''), \
                patch.dict('os.environ', {'SJFX_LP_EMBED_FALLBACK_BASE_URL': 'http://127.0.0.1:18005/v1'}), \
                patch('services.embedding_backend.OpenAIEmbeddingClient') as transport:
            self.assertIs(semantic.embedding_client(), transport.return_value)
        transport.assert_called_once()
        self.assertEqual(transport.call_args.args[0], 'http://127.0.0.1:18004/v1')

    def test_factory_client_retains_attributes_used_by_callers(self):
        from config import Config
        with patch.object(Config, 'EMBED_BACKEND', 'openai_compatible'), \
                patch.object(Config, 'EMBED_BASE_URL', 'http://127.0.0.1:18004/v1'), \
                patch.object(Config, 'EMBED_MODEL_PATH', ''):
            client = semantic.embedding_client()
        self.assertEqual(client.model, Config.EMBED_MODEL)
        self.assertEqual(client.max_batch_size, semantic.embed_batch_size())
        self.assertEqual(client.base_url, 'http://127.0.0.1:18004/v1')
        self.assertTrue(callable(client.health_check))


class TopicPurityTests(unittest.TestCase):
    """2a：主题纯度（purity/subtopics）的解析契约。

    为什么要有这个字段：只问"谁不属于本组"时，模型会先顺着"概括共同主题"的要求把名字
    凑出来，再判断"谁不属于这个名字"，于是几乎总答"没有"（实测一个包 138 次 no-outliers），
    而它在 description 里却写着"毫无关联/缺乏共同主题"。所以改成先判纯度、再给名字。
    """

    NAME = '数据库事务隔离级别研究'
    CITED = ('数据库事务隔离级别决定了并发事务之间的可见性与锁的行为。本文对比四档隔离级别，'
             '给出吞吐、锁等待与死锁次数的实测数据，并讨论分布式场景下的实现代价与取舍。')

    def _validate(self, payload, evidence=None):
        material = {'evidence': evidence or [{'id': 'E1', 'source_path': 'a.txt',
                                              'text': self.CITED, 'headings': []}]}
        return naming.validate(json.dumps(payload, ensure_ascii=False), material)

    def _payload(self, **extra):
        base = {'name': self.NAME, 'description': '这是一段足够长的内容范围说明。',
                'evidence_ids': ['E1']}
        base.update(extra)
        return base

    def test_mixed_with_two_subtopics_is_parsed(self):
        second = {'id': 'E2', 'source_path': 'b.txt',
                  'text': '机器学习模型压缩涉及蒸馏与低比特量化部署。' * 4,
                  'headings': []}
        out = self._validate(self._payload(
            purity='mixed', evidence_ids=['E1', 'E2'],
            subtopics=[{'name': '数据库并发控制', 'members': ['E1']},
                       {'name': '机器学习模型压缩', 'members': ['E2']}]),
            evidence=[{'id': 'E1', 'source_path': 'a.txt', 'text': self.CITED,
                       'headings': []}, second])
        self.assertEqual(out['purity'], 'mixed')
        self.assertEqual([s['name'] for s in out['subtopics']],
                         ['数据库并发控制', '机器学习模型压缩'])

    def test_mixed_without_usable_subtopics_degrades_to_unsure(self):
        # 自称 mixed 却给不出两个可用子主题 → 降级为 unsure，**不拆**（宁可少拆，不拆坏）
        out = self._validate(self._payload(purity='mixed', subtopics=[]))
        self.assertEqual(out['purity'], 'unsure')
        self.assertEqual(out['subtopics'], [])

    def test_missing_purity_is_unknown_and_never_splits(self):
        # 缺失字段按未知处理；保守地不拆，并提示复核。
        out = self._validate(self._payload())
        self.assertEqual(out['purity'], 'unsure')
        self.assertTrue(out['needs_review'])
        self.assertEqual(out['subtopics'], [])

    def test_subtopic_members_outside_evidence_are_dropped(self):
        out = self._validate(self._payload(
            purity='mixed',
            subtopics=[{'name': '数据库并发控制', 'members': ['E1', 'E99']},
                       {'name': '不存在的编号', 'members': ['E98']}]))
        # 第二个子主题只剩非法编号 → 无有效分组 → 降级
        self.assertEqual(out['purity'], 'unsure')

    def test_bogus_subtopic_names_fall_back_to_no_split(self):
        out = self._validate(self._payload(
            purity='mixed',
            subtopics=[{'name': '待细分', 'members': ['E1']},
                       {'name': 'x', 'members': ['E1']}]))
        self.assertEqual(out['purity'], 'unsure', '状态名/过短的名字不能当子主题名')


class SubtopicGuardTests(unittest.TestCase):
    """2b：尺寸感知守卫。A/B 实测：不加守卫误拆 23.3%/条目增 45%；
    一刀切要求"每子组≥3 个成员"则召回掉到 0（用户标的该拆组 5/9 是 ≤3 个成员的小组）。"""

    def test_small_group_has_no_floor(self):
        self.assertTrue(naming._subtopic_split_allowed(2, [1, 1]))
        self.assertTrue(naming._subtopic_split_allowed(4, [3, 1]))

    def test_large_group_rejects_slivers(self):
        self.assertFalse(naming._subtopic_split_allowed(20, [18, 2]), '2 个成员的碎片不该单独成条')
        self.assertFalse(naming._subtopic_split_allowed(30, [28, 2]))

    def test_large_group_accepts_balanced_split(self):
        self.assertTrue(naming._subtopic_split_allowed(20, [10, 10]))
        self.assertTrue(naming._subtopic_split_allowed(40, [30, 10]))

    def test_single_subtopic_is_never_a_split(self):
        self.assertFalse(naming._subtopic_split_allowed(10, [10]))


class IdenticalNameMergeTests(unittest.TestCase):
    """2c：同名叶子确定性合并（零模型调用）。

    实测：一个包里 9 组完全同名（「联合国安理会决议文本及国际安全事务记录」×4 …）始终没被
    合并，因为模型驱动的合并车道要求候选 needs_review=0，而该包 57% 的标签带复核标记。
    """

    @staticmethod
    def _fixture(tmp):
        from pathlib import Path
        from contextlib import closing as _closing
        from services.large_package_isolated import LargePackageStore
        from services import large_package_catalog_merge as merge
        from config import Config
        with patch.object(Config, 'SCAN_ALLOWED_ROOTS', ()):
            root = Path(tmp)
            store = LargePackageStore(root / 'state.db')
            pid = store.create(root)['id']

            def add_group(gid, paths, name):
                with _closing(store._connect()) as c:
                    naming.init(c)
                    c.execute('INSERT INTO large_catalog_groups(package_id,id,model,anchor,'
                              'representative,result,status,updated_at,member_count,'
                              'content_revision) VALUES(?,?,?,?,?,?,?,?,0,1)',
                              (pid, gid, 'fixture', b'', paths[0], '{}', 'named', 0.0))
                    for index, path in enumerate(paths):
                        store.upsert_file(pid, {'path': path, 'name': path})
                        vector = np.array([1, .1 * index, 0, 0], dtype=np.float32)
                        c.execute('INSERT INTO large_catalog_members VALUES(?,?,?,?,?)',
                                  (pid, path, gid, 'h%s' % index,
                                   (vector / np.linalg.norm(vector)).tobytes()))
                    # 插入成员会让 `lp_member_added` 触发器把 content_revision 自增，
                    # 于是刚写的标签会被判成"过期"。归一到 1，标签也写 1。
                    c.execute('UPDATE large_catalog_groups SET content_revision=1 '
                              'WHERE package_id=? AND id=?', (pid, gid))
                    # 每个组都抢占同一个 bucket 槽位：真实流水线里同名同主题的组必然落进
                    # 相邻甚至相同的桶，所以"把被并组的 bucket 行直接 UPDATE 成宿主"会撞
                    # 唯一约束 (package_id,band,bucket) —— 这里刻意制造这个冲突。
                    c.execute('INSERT INTO large_catalog_buckets VALUES(?,?,?,?)',
                              (pid, 0, 'shared-slot', gid))
                    c.execute('INSERT INTO lp_catalog_labels VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                              (pid, gid, 1, 1, name, '描述', '[]', 'qwen', 'd-' + gid, 0, 0, 0.0))
                    c.commit()
            return store, pid, add_group, merge

    def test_identical_names_merge_into_the_largest(self):
        import tempfile
        from contextlib import closing as _closing
        with tempfile.TemporaryDirectory() as tmp:
            store, pid, add_group, merge = self._fixture(tmp)
            add_group('km-a-00001', ['m0.txt', 'm1.txt', 'm2.txt'], '联合国安理会决议文本记录')
            add_group('km-a-00002', ['n0.txt', 'n1.txt'], '联合国安理会决议文本记录')
            merged = merge.merge_identical_names(store, pid)
            self.assertEqual(merged, 1, '应并掉 1 个同名组')
            with _closing(store._connect()) as c:
                groups = [r[0] for r in c.execute('SELECT id FROM large_catalog_groups '
                                                  'WHERE package_id=?', (pid,))]
                label = c.execute('SELECT needs_review,model FROM lp_catalog_labels '
                                  'WHERE package_id=? AND group_id=?',
                                  (pid, 'km-a-00001')).fetchone()
                members = c.execute('SELECT COUNT(*) FROM large_catalog_members '
                                    'WHERE package_id=? AND group_id=?',
                                    (pid, 'km-a-00001')).fetchone()[0]
            self.assertEqual(groups, ['km-a-00001'], '被并组应被删除')
            self.assertEqual(members, 5, '成员要全部搬进宿主组')
            self.assertEqual(label['needs_review'], 1, '自动合并必须留下复核标记')
            self.assertEqual(label['model'], 'merge-identical')

    def test_bucket_rows_survive_the_merge_without_a_unique_conflict(self):
        """回归：bucket 表主键是 (package_id,band,bucket)，宿主已占同槽位。

        真实数据上 `merge_identical_names` 曾在这里抛
        `sqlite3.IntegrityError: UNIQUE constraint failed: large_catalog_buckets...`
        导致整个校验脚本中止；正确的做法是删掉被并组与宿主的 bucket 行，再用
        重算后的质心重建宿主的桶位。
        """
        import tempfile
        from contextlib import closing as _closing
        with tempfile.TemporaryDirectory() as tmp:
            store, pid, add_group, merge = self._fixture(tmp)
            add_group('km-c-00001', ['p0.txt', 'p1.txt', 'p2.txt'], '同一主题的名称')
            add_group('km-c-00002', ['q0.txt', 'q1.txt'], '同一主题的名称')
            merged = merge.merge_identical_names(store, pid)      # 不得抛异常
            self.assertEqual(merged, 1)
            with _closing(store._connect()) as c:
                rows = [tuple(r) for r in c.execute(
                    'SELECT band,bucket,group_id FROM large_catalog_buckets '
                    'WHERE package_id=? ORDER BY band,bucket', (pid,))]
                anchor = c.execute('SELECT anchor FROM large_catalog_groups '
                                   'WHERE package_id=? AND id=?',
                                   (pid, 'km-c-00001')).fetchone()[0]
            self.assertTrue(rows, '宿主组的桶位必须被重建，否则相似度检索看不到它')
            self.assertEqual({r[2] for r in rows}, {'km-c-00001'},
                             '被并组的桶位必须清掉，不能留下悬空引用')
            self.assertTrue(anchor, '宿主组的质心应被重算')
            import numpy as _np
            from services.large_package_semantic import _buckets
            self.assertEqual(rows,
                             [(band, bucket, 'km-c-00001') for band, bucket in
                              enumerate(_buckets(_np.frombuffer(anchor, dtype=_np.float32)))])

    def test_identical_names_merge_past_the_leaf_size_cap(self):
        """同名合并的上限必须比叶子上限宽，否则它最该修的问题被自己的守卫挡住。

        真实数据（包 48db486cf0a0）：叶子上限 cap=102，而 6 组同名主题的成员合计
        121~170（`联合国安理会决议文本及国际安全事务记录` 93+59+15=167 …），全部按
        "超上限"跳过 —— 只有两组小到能塞进 102 的并掉了。这里按同样的形状造：两个
        同名组共 120 个成员，语料 6,020 个向量（叶子上限 76、同名合并上限 201）。
        """
        import tempfile
        from contextlib import closing as _closing
        from config import Config
        with tempfile.TemporaryDirectory() as tmp:
            store, pid, add_group, merge = self._fixture(tmp)
            add_group('km-d-00001', ['r%03d.txt' % i for i in range(60)], '同一主题名称')
            add_group('km-d-00002', ['s%03d.txt' % i for i in range(60)], '同一主题名称')
            filler = np.array([1, 0, 0, 0], dtype=np.float32).tobytes()
            with _closing(store._connect()) as c:
                c.executemany('INSERT INTO large_catalog_members VALUES(?,?,?,?,?)',
                              [(pid, 'filler%05d.txt' % i, 'km-filler', 'h', filler)
                               for i in range(5900)])
                c.commit()
            union = 120
            with patch.object(Config, 'LP_CATALOG_GROUP_MERGE_RATIO', 1.5):
                strict = merge.merge_size_ceiling(store, pid)
            widened = merge.merge_size_ceiling(store, pid)
            self.assertLess(strict, union, '对照：只按叶子上限口径时，同名组确实会被挡住')
            self.assertGreaterEqual(widened, union, '放宽后的上限必须容得下同名主题')
            self.assertEqual(merge.merge_identical_names(store, pid), 1, '同名必须合并')

    def test_status_names_are_never_merged(self):
        import tempfile
        from contextlib import closing as _closing
        with tempfile.TemporaryDirectory() as tmp:
            store, pid, add_group, merge = self._fixture(tmp)
            add_group('km-b-00001', ['x0.txt'], '证据不足（待补充内容）')
            add_group('km-b-00002', ['y0.txt'], '证据不足（待补充内容）')
            merged = merge.merge_identical_names(store, pid)
            self.assertEqual(merged, 0, '诊断状态名不是主题名，绝不能按名字合并')
            with _closing(store._connect()) as c:
                groups = sorted(r[0] for r in c.execute('SELECT id FROM large_catalog_groups '
                                                        'WHERE package_id=?', (pid,)))
            self.assertEqual(groups, ['km-b-00001', 'km-b-00002'])


class SubtopicSplitTests(unittest.TestCase):
    """2b：按子主题建真实分组、复用模型给的名字（零额外调用）。"""

    @staticmethod
    def _fixture(tmp, members=4):
        from pathlib import Path
        from contextlib import closing as _closing
        from services.large_package_isolated import LargePackageStore
        from config import Config
        with patch.object(Config, 'SCAN_ALLOWED_ROOTS', ()):
            root = Path(tmp)
            store = LargePackageStore(root / 'state.db')
            pid = store.create(root)['id']
            paths = ['m%02d.txt' % i for i in range(members)]
            with _closing(store._connect()) as c:
                naming.init(c)
                # 先建组、再插成员：成员插入触发器 `lp_member_count_add` 才会把
                # member_count 加上去（反过来的话组是 0，拆分后的成员数就不对了）。
                c.execute('INSERT INTO large_catalog_groups(package_id,id,model,anchor,'
                          'representative,result,status,updated_at,member_count,content_revision) '
                          'VALUES(?,?,?,?,?,?,?,?,0,1)',
                          (pid, 'km-z-00001', 'fixture', b'', paths[0], '{}', 'named', 0.0))
                for i, path in enumerate(paths):
                    store.upsert_file(pid, {'path': path, 'name': path})
                    vector = np.array([1, .05 * i, 0, 0], dtype=np.float32)
                    c.execute('INSERT INTO large_catalog_members VALUES(?,?,?,?,?)',
                              (pid, path, 'km-z-00001', 'h%d' % i,
                               (vector / np.linalg.norm(vector)).tobytes()))
                # 插入成员会触发 content_revision 自增；归一到 1，好让素材里的 revision=1 对得上
                c.execute('UPDATE large_catalog_groups SET content_revision=1 '
                          'WHERE package_id=? AND id=?', (pid, 'km-z-00001'))
                c.commit()
            return store, pid, paths

    def _material(self, paths, revision=1):
        return {'revision': revision,
                'evidence': [{'id': 'E%d' % i, 'source_path': p, 'text': 'x' * 200,
                              'headings': []} for i, p in enumerate(paths)]}

    def test_split_creates_named_groups_from_model_subtopics(self):
        import tempfile
        from contextlib import closing as _closing
        from config import Config
        # 关键：**异类不成组**（LP_LEAF_SPLIT_NAMED=False，第③项的默认）下，
        # 子主题拆分仍然要生效——两者是独立开关，2026-09-20 之前共用一个，
        # 导致开子主题拆分时把"异类直接成组"的旧行为一起放了回来。
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(Config, 'LP_LEAF_SPLIT_NAMED', False), \
                patch.object(Config, 'LP_LEAF_SPLIT_SUBTOPICS', True):
            store, pid, paths = self._fixture(tmp, 4)
            # The proposed two topics must also have two distinct content populations.
            with _closing(store._connect()) as c:
                for path in paths[2:]:
                    c.execute('UPDATE large_catalog_members SET vector=? WHERE package_id=? AND path=?',
                              (np.array([0,1,0,0],dtype=np.float32).tobytes(),pid,path))
                c.execute('UPDATE large_catalog_groups SET content_revision=1 WHERE package_id=?',(pid,))
            result = {'purity': 'mixed' , 'name': '云计算与锅炉压力容器',
                      'subtopics': [{'name': '云计算架构', 'evidence_ids': ['E0', 'E1']},
                                    {'name': '锅炉压力容器规范', 'evidence_ids': ['E2', 'E3']}],
                      'revision': 1}
            self.assertTrue(naming.split_into_subtopics(store, pid, 'km-z-00001',
                                                        self._material(paths), result))
            with _closing(store._connect()) as c:
                rows = {r['id']: r for r in c.execute(
                    'SELECT g.id,g.member_count,a.name,a.needs_review,a.model '
                    'FROM large_catalog_groups g LEFT JOIN lp_catalog_labels a '
                    '  ON a.package_id=g.package_id AND a.group_id=g.id '
                    'WHERE g.package_id=?', (pid,))}
                splits = c.execute('SELECT splits FROM lp_leaf_splits WHERE package_id=?',
                                   (pid,)).fetchone()
            self.assertEqual(len(rows), 2, '应新建 1 个组')
            self.assertEqual(sorted(r['name'] for r in rows.values()),
                             ['云计算架构', '锅炉压力容器规范'],
                             '名字必须复用模型给的子主题名，不重新调用')
            self.assertTrue(all(r['needs_review'] == 1 for r in rows.values()),
                            '二次判断来的名字要标复核')
            self.assertTrue(all(r['model'] == 'model-subtopic' for r in rows.values()))
            self.assertEqual(sum(r['member_count'] for r in rows.values()), 4, '成员不能丢')
            self.assertEqual(splits[0], 1, '拆分预算照记')

    def test_subtopic_split_has_its_own_switch(self):
        """子主题拆分有自己的回滚开关，且**不受**"异类成组"开关影响。"""
        import tempfile
        from contextlib import closing as _closing
        from config import Config
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(Config, 'LP_LEAF_SPLIT_SUBTOPICS', False), \
                patch.object(Config, 'LP_LEAF_SPLIT_NAMED', True):
            store, pid, paths = self._fixture(tmp, 4)
            result = {'purity': 'mixed', 'subtopics': [
                {'name': '云计算架构', 'evidence_ids': ['E0', 'E1']},
                {'name': '锅炉压力容器规范', 'evidence_ids': ['E2', 'E3']}], 'revision': 1}
            self.assertFalse(naming.split_into_subtopics(store, pid, 'km-z-00001',
                                                         self._material(paths), result))
            with _closing(store._connect()) as c:
                count = c.execute('SELECT COUNT(*) FROM large_catalog_groups '
                                  'WHERE package_id=?', (pid,)).fetchone()[0]
            self.assertEqual(count, 1, '开关关闭时不应新建组')

    def test_large_group_with_a_sliver_is_not_split(self):
        import tempfile
        from contextlib import closing as _closing
        from config import Config
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(Config, 'LP_LEAF_SPLIT_SUBTOPICS', True):
            store, pid, paths = self._fixture(tmp, 20)
            result = {'purity': 'mixed', 'subtopics': [
                {'name': '计算机安全机制', 'evidence_ids': ['E%d' % i for i in range(18)]},
                {'name': '非计算机相关条目', 'evidence_ids': ['E18', 'E19']}], 'revision': 1}
            self.assertFalse(naming.split_into_subtopics(store, pid, 'km-z-00001',
                                                         self._material(paths), result),
                             '20 个成员的组里拆出 2 个文件的碎片应被守卫拦下')
            with _closing(store._connect()) as c:
                count = c.execute('SELECT COUNT(*) FROM large_catalog_groups '
                                  'WHERE package_id=?', (pid,)).fetchone()[0]
            self.assertEqual(count, 1)


class LeafSplitPolicyTests(unittest.TestCase):
    """第 3 项：模型指出的"异类"默认**不给名字**，移进系统桶 km-other-*。

    为什么：本语料上"这撮人自成一类"这件事无法验证——试过四种搬动前核对的
    判据（子组内聚度、与父组质心的间隔、置换检验、名称/证据一致性），判别力
    全部为零。没有验证手段时编一个名字比不给名字更糟：它把"不知道"包装成一个
    看起来确定的主题。旧行为（新建命名叶组）留在 SJFX_LP_LEAF_SPLIT_NAMED 后面。
    """

    N = 6

    def _fixture(self, tmp, n=None):
        from pathlib import Path
        from contextlib import closing as _closing
        from services.large_package_isolated import LargePackageStore
        from config import Config
        n = n or self.N
        with patch.object(Config, 'SCAN_ALLOWED_ROOTS', ()):
            root = Path(tmp)
            store = LargePackageStore(root / 'state.db')
            pid = store.create(root)['id']
            with _closing(store._connect()) as c:
                naming.init(c)
                for i in range(n):
                    store.upsert_file(pid, {'path': 'm%02d.txt' % i, 'name': 'm%02d.txt' % i})
                # 父组要有一个**真实质心**：搬动前核对要拿子组质心与它比
                # （空质心是系统桶的设计，不是被核对组的常态）。
                parent_vector = np.array([0, 1, .05, 0], dtype=np.float32)
                parent_anchor = (parent_vector / np.linalg.norm(parent_vector)).tobytes()
                c.execute('INSERT INTO large_catalog_groups(package_id,id,model,anchor,'
                          'representative,result,status,updated_at,member_count,content_revision) '
                          'VALUES(?,?,?,?,?,?,?,?,?,?)',
                          (pid, 'km-aaa-00001', 'fixture', parent_anchor, 'm00.txt', '{}',
                           'named', 0.0, 0, 1))
                # 前两篇一个方向、其余另一个方向：这是"模型会指认的异类"的常见形状
                # （余弦仍然很高——本语料的判别区间只有 0.1，正是本项的依据）。
                for i in range(n):
                    vector = np.array([0, 1, .05, 0] if i else [1, .05, 0, 0],
                                      dtype=np.float32)
                    vector = vector / np.linalg.norm(vector)
                    c.execute('INSERT INTO large_catalog_members VALUES(?,?,?,?,?)',
                              (pid, 'm%02d.txt' % i, 'km-aaa-00001', 'h%02d' % i,
                               vector.tobytes()))
                c.commit()
            return store, pid

    @staticmethod
    def _material(paths, revision=1):
        return {'revision': revision,
                'evidence': [{'id': 'E%d' % i, 'source_path': p}
                             for i, p in enumerate(paths)]}

    @staticmethod
    def _revision(store, pid, group='km-aaa-00001'):
        """组的当前修订号（插入成员会被触发器逐条递增，不能写死 1）。"""
        from contextlib import closing as _closing
        with _closing(store._connect()) as c:
            return int(c.execute('SELECT content_revision FROM large_catalog_groups '
                                 'WHERE package_id=? AND id=?', (pid, group)).fetchone()[0])

    def _call(self, store, pid, outliers, n=None):
        paths = ['m%02d.txt' % i for i in range(n or self.N)]
        material = self._material(paths, self._revision(store, pid))
        return naming.split_off_outliers(store, pid, 'km-aaa-00001', material,
                                         {'outliers': outliers})

    def _bucket(self, pid):
        import hashlib
        return 'km-other-' + hashlib.sha256(pid.encode()).hexdigest()[:8]

    def _named_leaf(self, pid, seq=0):
        import hashlib
        return 'km-%s-s%04d' % (hashlib.sha256(pid.encode()).hexdigest()[:8], seq)

    def test_outliers_land_in_the_holding_bucket_without_a_new_named_group(self):
        import tempfile
        from contextlib import closing as _closing
        from config import Config
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(Config, 'LP_LEAF_SPLIT_NAMED', False):
            store, pid = self._fixture(tmp)
            self.assertTrue(self._call(store, pid, ['E0', 'E1']))
            with _closing(store._connect()) as c:
                groups = {r[0]: r for r in c.execute(
                    'SELECT id,status,anchor,representative,result,member_count '
                    'FROM large_catalog_groups WHERE package_id=?', (pid,))}
                jobs = [r[0] for r in c.execute('SELECT group_id FROM lp_catalog_jobs '
                                                'WHERE package_id=?', (pid,))]
                members = {r[0]: r[1] for r in c.execute(
                    'SELECT path,group_id FROM large_catalog_members WHERE package_id=?', (pid,))}
                splits = c.execute('SELECT splits FROM lp_leaf_splits WHERE package_id=?',
                                   (pid,)).fetchone()
            bucket = self._bucket(pid)
            self.assertNotIn(self._named_leaf(pid), groups, '默认不得新建命名叶组')
            self.assertEqual(sorted(groups), sorted(['km-aaa-00001', bucket]))
            self.assertEqual(groups[bucket]['member_count'], 2)
            self.assertEqual(groups['km-aaa-00001']['member_count'], self.N - 2)
            self.assertFalse(groups[bucket]['anchor'], '待归并桶必须保持空质心')
            self.assertEqual(groups[bucket]['status'], 'named',
                             '系统桶不参与语义命名（与 semantic.py 建桶约定一致）')
            self.assertEqual(sorted(p for p, g in members.items() if g == bucket),
                             ['m00.txt', 'm01.txt'])
            self.assertEqual(jobs, [], '待归并桶不得排命名任务')
            self.assertEqual(splits[0], 1, '摘除次数照常计入预算（预算没有被绕过）')
            payload = json.loads(groups[bucket]['result'])
            self.assertEqual(payload['hold_reason'], 'outliers')
            self.assertEqual(payload['holds'][-1]['parent'], 'km-aaa-00001')
            self.assertEqual(payload['holds'][-1]['moved'], 2)
            # §3.2 的"搬动前核对"：几何必须真的算出来并落库（判别力为零，所以不据它
            # 决策，但"为什么不成组"必须是可复查的数据而不是我们的一面之词）。
            check = payload['holds'][-1].get('check')
            self.assertIsNotNone(check, '每次摘除都要记录成员向量的核对结果')
            self.assertEqual(check['members'], 2)
            self.assertIsNotNone(check['cohesion'])
            self.assertIsNotNone(check['parent_cos'])
            # 父组几何必须按**搬走之后**的成员重算，否则代表性文件可能已被搬走。
            vector = np.frombuffer(groups['km-aaa-00001']['anchor'], dtype=np.float32)
            self.assertAlmostEqual(float(np.linalg.norm(vector)), 1.0, places=5)
            self.assertIn(groups['km-aaa-00001']['representative'],
                          ['m%02d.txt' % i for i in range(2, self.N)],
                          '父组代表文件必须还是它现在的成员')

    def test_flag_restores_the_old_named_leaf_group(self):
        import tempfile
        from contextlib import closing as _closing
        from config import Config
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(Config, 'LP_LEAF_SPLIT_NAMED', True):
            store, pid = self._fixture(tmp)
            self.assertTrue(self._call(store, pid, ['E0', 'E1']))
            with _closing(store._connect()) as c:
                groups = {r[0]: r for r in c.execute(
                    'SELECT id,status,anchor FROM large_catalog_groups WHERE package_id=?',
                    (pid,))}
                jobs = [r[0] for r in c.execute('SELECT group_id FROM lp_catalog_jobs '
                                                'WHERE package_id=?', (pid,))]
            leaf = self._named_leaf(pid)
            self.assertIn(leaf, groups, '开关打开时恢复旧行为')
            self.assertNotIn(self._bucket(pid), groups)
            self.assertEqual(groups[leaf]['status'], 'queued')
            self.assertTrue(groups[leaf]['anchor'], '命名子组必须带质心')
            self.assertEqual(jobs, [leaf])

    def test_repeated_splits_keep_the_reason_log_bounded(self):
        import tempfile
        from contextlib import closing as _closing
        from config import Config
        total = 26
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(Config, 'LP_LEAF_SPLIT_NAMED', False):
            store, pid = self._fixture(tmp, total)
            for i in range(total - 1):
                # 每次都摘当前组里的第一篇（证据 id 随本次素材重新编号）
                paths = ['m%02d.txt' % j for j in range(i, total)]
                material = self._material(paths, self._revision(store, pid))
                self.assertTrue(naming.split_off_outliers(
                    store, pid, 'km-aaa-00001', material, {'outliers': ['E0']}),
                    '第 %d 次摘除应当成功' % (i + 1))
            with _closing(store._connect()) as c:
                raw = c.execute('SELECT result FROM large_catalog_groups WHERE package_id=? AND id=?',
                                (pid, self._bucket(pid))).fetchone()[0]
                splits = c.execute('SELECT splits FROM lp_leaf_splits WHERE package_id=?',
                                   (pid,)).fetchone()[0]
            self.assertEqual(splits, total - 1)
            self.assertLessEqual(len(json.loads(raw)['holds']), 20,
                                 'reason 记录必须有界，不能随拆分次数无限增长')

    def test_every_rejection_is_logged_with_a_reason(self):
        import tempfile
        from config import Config
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(Config, 'LP_LEAF_SPLIT_NAMED', False):
            store, pid = self._fixture(tmp)
            paths = ['m%02d.txt' % i for i in range(self.N)]
            with self.assertLogs('services.large_package_catalog_naming', level='INFO') as logs:
                # 模型没报异类：这是常态答案，必须留下可查的痕迹
                self.assertFalse(naming.split_off_outliers(
                    store, pid, 'km-aaa-00001', self._material(paths), {'outliers': []}))
                # 报的 id 不在本次证据里（模型幻觉）：同样要记原因
                self.assertFalse(naming.split_off_outliers(
                    store, pid, 'km-aaa-00001', self._material(paths), {'outliers': ['E99']}))
            text = '\n'.join(logs.output)
            self.assertIn('reason=no-outliers', text)
            self.assertIn('reason=outliers-not-in-evidence', text)


class KAdaptiveTests(unittest.TestCase):
    """第【5】项：k 自适应（闭环）。反馈信号 = 模型自认"跨领域拼凑(mixed)"的叶组占比。

    为什么走闭环而不是几何估计：本语料相似度基线 0.80–0.85、真正相关 0.94，判别区间
    只有 0.1；前面四轮实测（内聚度/置换检验/名称一致性）判别力全为零。而"叶子太粗"
    这件事**模型自己就会说**：同一份 8,158 文件的语料，k=120（中位 55 文件）时 40% 的
    叶组被判 mixed，k=300（中位 19 文件）时降到 27%。所以反馈用它的判定。
    """

    @staticmethod
    def _fixture(tmp, purities):
        from pathlib import Path
        from contextlib import closing as _closing
        from services.large_package_isolated import LargePackageStore
        from config import Config
        with patch.object(Config, 'SCAN_ALLOWED_ROOTS', ()):
            root = Path(tmp)
            store = LargePackageStore(root / 'state.db')
            pid = store.create(root)['id']
            with _closing(store._connect()) as c:
                naming.init(c)
                semantic.init(c)
                for i, purity in enumerate(purities):
                    gid = 'km-k-%05d' % i
                    c.execute('INSERT INTO large_catalog_groups(package_id,id,model,anchor,'
                              'representative,result,status,updated_at,member_count,'
                              'content_revision) VALUES(?,?,?,?,?,?,?,?,0,1)',
                              (pid, gid, 'fixture', b'', 'p%03d.txt' % i,
                               json.dumps({'purity': purity}), 'named', 0.0))
                    c.execute('UPDATE large_catalog_groups SET member_count=5 '
                              'WHERE package_id=? AND id=?', (pid, gid))
                    # 判据只统计"标签仍然有效"的组（修订号对得上），所以夹具要有标签
                    c.execute('INSERT INTO lp_catalog_labels VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                              (pid, gid, 1, 1, '主题名 %d' % i, '描述', '[]', 'qwen',
                               'd-%s' % gid, 0, 0, 0.0))
                c.execute('INSERT INTO large_catalog_km_state(package_id,signature,vectors,'
                          'updated_at) VALUES(?,?,?,?)', (pid, 'sig', len(purities), 0.0))
                c.commit()
            return store, pid

    def _patches(self, **kw):
        from config import Config
        base = {'LP_KM_ADAPTIVE': True, 'LP_KM_ESCALATE_SHARE': 0.25,
                'LP_KM_ESCALATE_FACTOR': 2.0, 'LP_KM_ESCALATE_MAX': 1}
        base.update(kw)
        return [patch.object(Config, key, value) for key, value in base.items()]

    def test_mixed_share_uses_only_current_leaves(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            # 3 mixed / 10 已知；子主题组(subtopic)与 unsure 进分母，但不算 mixed
            store, pid = self._fixture(tmp, ['mixed'] * 3 + ['same'] * 5
                                       + ['subtopic', 'unsure'])
            mixed, known, share = semantic.catalog_mixed_share(store, pid)
            self.assertEqual((mixed, known), (3, 10))
            self.assertAlmostEqual(share, 0.3, places=6)

    def test_mixed_labels_without_member_evidence_cannot_trigger_global_rebuild(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            store,pid=self._fixture(tmp,['mixed']*8+['same']*2)
            self.assertFalse(semantic.maybe_escalate_k(store,pid))
            self.assertEqual(semantic._km_boost_get(store,pid),0)
            self.assertEqual(semantic._km_state_get(store,pid),'sig')

    def test_low_mixed_share_and_small_samples_do_not_escalate(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            store, pid = self._fixture(tmp, ['mixed'] * 2 + ['same'] * 8)
            patches = self._patches()
            for p in patches:
                p.start()
            try:
                self.assertFalse(semantic.maybe_escalate_k(store, pid), '20% < 25% 不动')
            finally:
                for p in patches:
                    p.stop()
        with tempfile.TemporaryDirectory() as tmp:
            store, pid = self._fixture(tmp, ['mixed'] * 3)     # 只有 3 个已知 → 样本太小
            patches = self._patches()
            for p in patches:
                p.start()
            try:
                self.assertFalse(semantic.maybe_escalate_k(store, pid))
            finally:
                for p in patches:
                    p.stop()

    def test_adaptive_off_keeps_the_configured_k(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            store, pid = self._fixture(tmp, ['mixed'] * 9 + ['same'] * 1)
            patches = self._patches(LP_KM_ADAPTIVE=False)
            for p in patches:
                p.start()
            try:
                self.assertFalse(semantic.maybe_escalate_k(store, pid))
                self.assertEqual(semantic.adaptive_k(store, pid, 120, 8000,
                                                     semantic._km_settings()), 120)
            finally:
                for p in patches:
                    p.stop()


class NamingUnverifiableTokenTests(unittest.TestCase):
    """编号核对的两类误杀（2026-09-20 生产实测：7 组 / 276 个文件因此永无名字）。

    ① 公文里年份写成中文数字（一九八〇），模型规范成「1980」→ 按字面比判"未见于证据"；
    ② 模型把「人工智能」写成 AI、「亚太经合组织」写成 APEC → 缩写不在证据文本里。
    两者都不是幻觉，而是**规范化改写**。现在的口径：
      · 中文数字归一后再比；
      · 仍查不到的编号/缩写从名字/描述里**删掉**（并置 needs_review），不整组作废；
      · 删完不满足长度/格式要求才拒绝——宁可不给名字，也不给残缺的名字。
    """

    @staticmethod
    def _material(text, headings=None):
        return {'evidence': [{'id': 'E1', 'source_path': 'a.docx', 'text': text,
                              'headings': headings or []}],
                'held_out': [], 'profile': {}, 'revision': 1, 'digest': 'd',
                'owner_id': 'o', 'tokens': 10}

    def test_chinese_numeral_year_is_not_an_invented_identifier(self):
        material = self._material('一九八〇年国务院批转外贸体制改革方案，一九八五年继续执行。')
        content = json.dumps({'name': '1980年代外贸体制改革文件',
                              'description': '收录1980年代国务院批转的外贸体制改革文件。',
                              'evidence_ids': ['E1'], 'outliers': [], 'needs_review': False},
                             ensure_ascii=False)
        result = naming.validate(content, material)      # 不得抛异常
        self.assertIn('1980', result['name'])

    def test_acronym_absent_from_evidence_is_dropped_not_fatal(self):
        material = self._material('新一代人工智能发展规划，推动人工智能与实体经济融合。')
        content = json.dumps({'name': '人工智能发展规划与行业应用政策',
                              'description': '涵盖 AI 发展规划、监管制度与产业应用政策。',
                              'evidence_ids': ['E1'], 'outliers': [], 'needs_review': False},
                             ensure_ascii=False)
        result = naming.validate(content, material)
        self.assertNotIn('AI', result['description'], '查不到的缩写要从描述里删掉')
        self.assertEqual(result['name'], '人工智能发展规划与行业应用政策')
        self.assertTrue(result['needs_review'], '降级过的名字必须让人复核')

    def test_name_that_becomes_too_short_is_still_rejected(self):
        material = self._material('本组只谈人工智能的应用与治理。')
        content = json.dumps({'name': 'AI 治理',
                              'description': '关于 AI 治理的若干材料。',
                              'evidence_ids': ['E1'], 'outliers': [], 'needs_review': False},
                             ensure_ascii=False)
        # 删掉 AI 后名字只剩「治理」→ 不足 4 字，仍然拒绝（残缺的名字不如不给）
        with self.assertRaises(ValueError):
            naming.validate(content, material)

    def test_single_group_naming_gets_its_own_output_budget(self):
        from config import Config
        cfg = naming.settings()
        self.assertEqual(cfg['namer_output'], Config.LP_CATALOG_NAMING_OUTPUT_TOKENS)
        self.assertGreaterEqual(cfg['namer_output'], 300,
                                '单组命名的输出上限要够放下 purity+subtopics')
        self.assertEqual(Config.LP_CATALOG_OUTPUT_TOKENS, 240,
                         '批量车道的口径不动（它是按组数放大的）')


class FallbackNamingTests(unittest.TestCase):
    """重试耗尽的组必须有兜底名，否则目录覆盖率永远缺一块。

    生产实测（lp-6a7119c7）：模型命名失败 → 重试同一提示得到同一句话 → attempts 到上限
    后任务不再被拾取；而模型命名开启时 name_batch 是被禁用的（它会写组状态、卡死命名循环）
    → 那一组永远没有名字，目录页停在 7369/7754。
    兜底的边界：只写标签（model='fallback-naming'、needs_review=1）+ 任务收尾，
    **不碰 large_catalog_groups.status**。
    """

    @staticmethod
    def _fixture(tmp, attempts=5, with_label=False):
        from pathlib import Path
        from contextlib import closing as _closing
        from services.large_package_isolated import LargePackageStore
        from config import Config
        with patch.object(Config, 'SCAN_ALLOWED_ROOTS', ()):
            root = Path(tmp)
            store = LargePackageStore(root / 'state.db')
            pid = store.create(root)['id']
            gid = 'km-f-00001'
            rep = 'doc.pdf'
            with _closing(store._connect()) as c:
                naming.init(c)
                semantic.init(c)
                store.upsert_file(pid, {'path': rep, 'name': '2024.07.11_关于做好会计法贯彻实施工作的通知.pdf'})
                c.execute('INSERT INTO large_catalog_groups(package_id,id,model,anchor,'
                          'representative,result,status,updated_at,member_count,'
                          'content_revision) VALUES(?,?,?,?,?,?,?,?,0,1)',
                          (pid, gid, 'fixture', b'', rep, '{}', 'queued', 0.0))
                c.execute('UPDATE large_catalog_groups SET member_count=3 '
                          'WHERE package_id=? AND id=?', (pid, gid))
                c.execute('INSERT INTO lp_catalog_jobs(package_id,group_id,revision,status,'
                          'attempts,next_at,error) VALUES(?,?,?,?,?,?,?)',
                          (pid, gid, 1, 'deferred', attempts, 0.0, '模型重试耗尽'))
                if with_label:
                    c.execute('INSERT INTO lp_catalog_labels VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                              (pid, gid, 1, 1, '已有的名字', '描述描述描述描述', '[]', 'qwen',
                               'd', 0, 0, 0.0))
                c.commit()
            return store, pid, gid

    def test_exhausted_job_gets_a_fallback_name_without_touching_group_status(self):
        import tempfile
        from contextlib import closing as _closing
        with tempfile.TemporaryDirectory() as tmp:
            store, pid, gid = self._fixture(tmp)
            fixed = naming.fallback_name_exhausted(store, pid)
            self.assertEqual(fixed, 1)
            with _closing(store._connect()) as c:
                label = c.execute('SELECT name,description,model,needs_review,revision '
                                  'FROM lp_catalog_labels WHERE package_id=? AND group_id=?',
                                  (pid, gid)).fetchone()
                job = c.execute('SELECT status,error FROM lp_catalog_jobs WHERE package_id=? '
                                'AND group_id=?', (pid, gid)).fetchone()
                status = c.execute('SELECT status FROM large_catalog_groups WHERE package_id=? '
                                   'AND id=?', (pid, gid)).fetchone()[0]
                title = c.execute('SELECT name FROM files WHERE package_id=? AND path=?',
                                  (pid, 'doc.pdf')).fetchone()
            # 夹具里 doc.pdf 是代表文件，兜底名就取自它的标题（抽取式，零模型调用）
            self.assertEqual(label['name'], '关于做好会计法贯彻实施工作的通知',
                             '兜底名取自代表文件标题，并去掉日期前缀/扩展名')
            self.assertTrue(title, '代表文件要有 files 行，兜底名才拿得到标题')
            self.assertEqual(label['model'], 'fallback-naming')
            self.assertIn('兜底命名', label['description'])
            self.assertEqual(label['needs_review'], 1, '兜底名必须让人看见')
            self.assertEqual(label['revision'], 1, '标签要挂在当前修订号上才算有效')
            self.assertEqual(job['status'], 'completed')
            self.assertIn('兜底命名', job['error'])
            self.assertEqual(status, 'queued',
                             '绝不能改组状态：模型命名循环按它找任务（既有教训）')

    def test_jobs_below_the_attempt_limit_are_left_to_the_model(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            store, pid, gid = self._fixture(tmp, attempts=2)
            self.assertEqual(naming.fallback_name_exhausted(store, pid), 0,
                             '还有重试额度时不许抢在模型前面起名')

    def test_group_that_already_has_a_current_label_is_skipped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            store, pid, gid = self._fixture(tmp, with_label=True)
            self.assertEqual(naming.fallback_name_exhausted(store, pid), 0)


class MergeVerdictParsingTests(unittest.TestCase):
    """合并车道：'模型说这组不用并' 是合法答案，不该报 WARNING。

    生产实测 8 次 `Invalid equivalence verdict`，全是"这一批没有可合并的"，
    却把真正需要看的失败淹掉了。
    """

    def test_missing_or_null_verdict_means_no_merge(self):
        import services.large_package_catalog_merge as merge
        rows = [{'group_id': 'g1', 'files': 5, 'name': 'a', 'description': 'a'},
                {'group_id': 'g2', 'files': 3, 'name': 'b', 'description': 'b'}]
        source = open(merge.__file__, encoding='utf-8').read()
        # normalize 是 consolidate 的内部函数，这里用它的判据做源码级校验：
        # None / {'merges': None} 必须走"不合并"分支，而不是抛 'Invalid equivalence verdict'
        self.assertIn("if value is None:\n            return []", source)
        self.assertIn("if value is None:\n                return []", source)

    def test_merge_ceiling_is_clamped_to_a_multiple_of_the_leaf_cap(self):
        import tempfile
        from contextlib import closing as _closing
        from config import Config
        import services.large_package_catalog_merge as merge
        with tempfile.TemporaryDirectory() as tmp:
            store, pid, gid = FallbackNamingTests._fixture(tmp)
            with patch.object(Config, 'LP_CATALOG_GROUP_MAX', 60), \
                    patch.object(Config, 'LP_CATALOG_GROUP_MAX_AUTO', True), \
                    patch.object(Config, 'LP_KM_TARGET_LEAVES', 120), \
                    patch.object(Config, 'LP_CATALOG_GROUP_MERGE_RATIO', 40.0), \
                    patch.object(Config, 'LP_CATALOG_GROUP_MERGE_MAX_FACTOR', 3.0):
                # 没有钳制时 auto = N/120×40 会远大于叶子上限；钳制后最多 3 倍
                ceiling = merge.merge_size_ceiling(store, pid)
            from services.large_package_semantic import effective_group_max
            leaf = effective_group_max(store, pid)
            self.assertLessEqual(ceiling, max(leaf, leaf * 3),
                                 '合并上限必须被钳在叶子上限的常数倍以内')
            # 下界同样要钉住：曾经因为 effective_group_max 没在模块级导入抛 NameError、
            # 又被静默吞掉，函数退化成"只返回 auto"，小语料上等于 2 → 合并被莫名拒绝。
            self.assertGreaterEqual(ceiling, leaf,
                                    '合并上限绝不能低于叶子上限（否则合并会被无故拒绝）')


if __name__ == '__main__':
    unittest.main()
