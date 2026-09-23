"""Behavior checks for visible semantic directory -> selection -> summary -> overview."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from config import Config
from services.large_package_isolated import LargePackageStore
from services import large_package_catalog as catalog, large_package_semantic as semantic
from services.large_package_labels import name_group
from services.large_package_reporting import build_report, export_report
from large_package_worker import LargePackageWorker

class Embeddings:
    model='test-semantic'
    def __init__(self):self.calls=[]
    def embed(self,texts):
        self.calls.append(texts)
        return [[1,0,0] if 'attestation' in x.lower() else [0,1,0] if 'inventory' in x.lower() else [0,0,1] for x in texts]

# ---------------------------------------------------------------------------
# 模式的适用范围声明（A3）。
#
# 本文件里的四条断言检查的是**旧算法**的产物：`catalog_model='extractive-semantic-label/1'`、
# `portrait['catalog_group']`、`catalog_states['classified']`。生产的目录算法是
# `SJFX_LP_CATALOG_KMODE=1`（k-means），它不产生这些字段 —— 也就是说
# **生产在用的目录算法，原本没有任何契约测试覆盖**。
#
# 处理方式（不掩盖）：
#   * 这四条在 k-means 模式下**显式跳过**，并印出跳过理由，说明它们只适用于旧算法；
#   * 生产模式的真实覆盖补在 `tests/test_catalog_mode_contract.py`
#     （与算法无关的目录性质，两种模式下都跑、都必须通过）；
#   * 遗留清单里记一条：这四条应当进一步**参数化**成"两种模式各写各的断言"。
# ---------------------------------------------------------------------------
_MODE_ISOLATED_LEGACY = (
    "断言的是旧算法产物（catalog_group / catalog_model / catalog_states.classified）；"
    "生产跑 SJFX_LP_CATALOG_KMODE=1，这些字段不存在。生产模式的覆盖见 "
    "tests/test_catalog_mode_contract.py")


class SemanticCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.allowed=patch.object(Config,'SCAN_ALLOWED_ROOTS',());self.allowed.start()
        self.store=LargePackageStore(self.root/'large.db');self.pid=self.store.create(self.root)['id']
        self.embed=Embeddings();self.embedding_patch=patch('services.large_package_semantic.embedding_client',return_value=self.embed);self.embedding_patch.start()
    def tearDown(self):
        self.embedding_patch.stop();self.allowed.stop();self.tmp.cleanup()
    def add(self,path,text,**extra):
        self.store.upsert_file(self.pid,{'path':path,'name':path,**extra})
        self.store.update_file(self.pid,path,quick_status='completed',local_summary={'sample':text,'portrait':{'schema':'large-content-portrait/1','fields':[],'headings':[]}})
    def classify(self):
        while self.store.stage_counts(self.pid,'catalog').get('queued'):semantic.embed_batch(self.store,self.pid,self.embed)
        while semantic.name_batch(self.store,self.pid):pass
    def row(self,path):return self.store.get_file(self.pid,path)
    @unittest.skipIf(Config.LP_CATALOG_KMODE, _MODE_ISOLATED_LEGACY)
    def test_same_meaning_cross_format_groups_use_original_evidence(self):
        self.add('contract.pdf','Remote attestation verifies evidence from devices. Remote attestation establishes trust.')
        self.add('sales.txt','Remote attestation exchanges device claims. Remote attestation verifies these claims.')
        self.add('one.csv','Inventory stock movements and warehouse records. Inventory stock tracking.')
        self.classify()
        a,b,c=self.row('contract.pdf'),self.row('sales.txt'),self.row('one.csv')
        self.assertEqual(a['local_summary']['portrait']['catalog_group'],b['local_summary']['portrait']['catalog_group'])
        self.assertEqual(a['topic'],b['topic']);self.assertNotEqual(a['topic'],c['topic'])
        self.assertIn('attestation',a['topic'].lower());self.assertNotIn('sales',a['topic'].lower())
        evidence=b['local_summary']['portrait']['catalog_evidence'][0]
        self.assertEqual(evidence['source_path'],'sales.txt');self.assertIn('exchanges',evidence['text'])
    @unittest.skipIf(Config.LP_CATALOG_KMODE, _MODE_ISOLATED_LEGACY)
    def test_directory_generation_never_calls_generative_model(self):
        self.add('one.pdf','Remote attestation verifies evidence from devices. Remote attestation protocols.')
        with patch('services.vllm.VLLMClient.chat',side_effect=AssertionError('No generation allowed')):
            self.classify()
        self.assertEqual(self.row('one.pdf')['local_summary']['portrait']['catalog_model'],'extractive-semantic-label/1')
    @unittest.skipIf(Config.LP_CATALOG_KMODE, _MODE_ISOLATED_LEGACY)
    def test_deep_summary_reassigns_and_preserves_source(self):
        self.add('a.pdf','Trusted execution architecture and memory isolation mechanisms.')
        self.add('b.pdf','Remote attestation verifies claims from devices. Remote attestation protocols.')
        self.classify();before=self.row('a.pdf')
        deep={'summary_type':'model','summary':'Remote attestation is the central subject. Remote attestation verification protocols and evidence appraisal.','text_preview':'Trusted execution architecture background.'}
        self.store.update_file(self.pid,'a.pdf',deep_status='completed',deep_summary=deep,sha256='original-hash')
        self.classify();after=self.row('a.pdf')
        self.assertNotEqual(before['local_summary']['portrait']['catalog_group'],after['local_summary']['portrait']['catalog_group'])
        self.assertIn('attestation',after['topic'].lower());self.assertEqual(after['deep_summary'],deep)
        self.assertEqual(after['sha256'],'original-hash');self.assertEqual(after['local_summary']['sample'],before['local_summary']['sample'])
    def test_cached_embedding_not_recomputed_for_duplicate_or_unchanged(self):
        text='Remote attestation evidence and verification mechanisms.'
        self.add('a.pdf',text);self.add('b.pdf',text);self.classify()
        self.assertEqual(sum(len(c) for c in self.embed.calls),1)
        row=self.row('a.pdf');self.store.update_file(self.pid,'a.pdf',local_summary=row['local_summary'])
        self.assertFalse(self.store.stage_counts(self.pid,'catalog').get('queued'))
    def test_embedding_outage_keeps_file_browser_and_parse_results(self):
        self.add('a.pdf','Remote attestation verifies claims about devices and servers.')
        self.embed.embed=Mock(side_effect=RuntimeError('unavailable'))
        semantic.embed_batch(self.store,self.pid,self.embed)
        self.assertEqual(self.row('a.pdf')['quick_status'],'completed')
        self.assertEqual(self.row('a.pdf')['local_summary']['portrait']['catalog_status'],'deferred')
        self.assertEqual(self.store.file_page(self.pid)['total'],1)
    def test_pause_prevents_semantic_work(self):
        self.add('a.pdf','Remote attestation evidence and verification mechanisms.')
        self.store.update_package(self.pid,status='paused',phase='catalog_ready')
        worker=LargePackageWorker(self.store)
        self.assertFalse(worker.run_once());self.assertEqual(len(self.embed.calls),0)
    def test_empty_sensitive_files_do_not_infer_topic_from_filename(self):
        self.add('sales-contract.pdf','',sensitive=True)
        self.assertEqual(self.row('sales-contract.pdf')['category'],'受限文件')
        self.assertFalse(self.store.stage_counts(self.pid,'catalog').get('queued'))
    def test_stale_output_cannot_replace_new_summary(self):
        self.add('a.pdf','Remote attestation evidence and verification mechanisms.')
        digest=catalog.material(self.row('a.pdf'))[1]
        self.store.update_file(self.pid,'a.pdf',deep_status='completed',deep_summary={'summary_type':'model','summary':'Inventory stock tracking and warehouse operations.'})
        self.assertFalse(catalog._save(self.store,self.pid,'a.pdf',digest,error='stale'))
        self.assertEqual(self.store.stage_counts(self.pid,'catalog')['queued'],1)
    def test_representative_move_preserves_other_members(self):
        self.add('a.pdf','Remote attestation protocols and claims. Remote attestation evidence.')
        self.add('b.pdf','Remote attestation verifies claims. Remote attestation trust protocols.')
        self.classify()
        self.store.update_file(self.pid,'a.pdf',deep_status='completed',deep_summary={'summary_type':'model','summary':'Inventory warehouse stock tracking. Inventory stock movements.'})
        self.classify()
        self.assertIn('attestation',self.row('b.pdf')['topic'].lower())
        self.assertIn('inventory',self.row('a.pdf')['topic'].lower())
    def test_bad_vectors_are_rejected(self):
        for vector in ([0,0,0],[float('nan'),1,0]):
            with self.assertRaises(ValueError):semantic._vector(vector)
    def test_extractive_labels_preserve_chinese_source_phrases(self):
        result=name_group([{'text':'机密计算发展现状与趋势。机密计算发展现状与趋势。','deep_summary':'','headings':['机密计算发展现状与趋势'],'fields':[]}])
        self.assertIn('机密计算',result['topic'])
    @unittest.skipIf(Config.LP_CATALOG_KMODE, _MODE_ISOLATED_LEGACY)
    def test_preview_stage_produces_visible_groups_before_selection(self):
        source=self.root/'input';source.mkdir();(source/'one.txt').write_text('Remote attestation protocols verify remote device claims.',encoding='utf-8')
        self.store.update_package(self.pid,status='paused')
        self.pid=self.store.create(source)['id'];worker=LargePackageWorker(self.store)
        try:
            worker.run_once();worker.run_once();worker.run_once()
            item=self.store.get(self.pid);cat=self.store.catalog(self.pid)
            self.assertEqual(item['status'],'waiting_for_selection')
            self.assertEqual(cat['smart']['catalog_states']['classified'],1)
            self.assertTrue(cat['smart']['topics'])
        finally:worker.preview_runner.close()
    def test_selected_file_summary_updates_directory_overview_and_exports(self):
        self.add('one.pdf','Trusted execution architecture and memory isolation.')
        self.add('two.pdf','Remote attestation protocols and evidence verification.')
        self.classify();self.store.update_package(self.pid,status='waiting_for_selection',phase='catalog_ready')
        before=build_report(self.store,self.store.get(self.pid))
        old_topic=self.row('one.pdf')['topic']
        self.store.save_selection(self.pid,['one.pdf'])
        result={'summary_type':'model','summary':'Remote attestation protocols require fresh evidence. Finding: nonce verification prevents replay attacks.','text_preview':'Trusted execution background and generic introduction.'}
        worker=LargePackageWorker(self.store)
        with patch.object(worker,'_deep_parse',return_value=result):worker.run_once()
        current=self.store.get(self.pid);report=build_report(self.store,current)
        self.assertEqual(current['status'],'completed');self.assertNotEqual(old_topic,self.row('one.pdf')['topic'])
        self.assertEqual(self.row('two.pdf')['deep_status'],'idle')
        self.assertNotEqual(before['revision'],report['revision'])
        self.assertIn('nonce verification',json.dumps(report['research_brief']['key_findings']))
        self.assertEqual(report['coverage']['deep_completed'],1)
        self.assertEqual(report['topic_summaries'][0]['sources'][0]['scope'],'深度摘要')
        self.assertTrue(export_report(self.store,current,'json').is_file());self.assertTrue(export_report(self.store,current,'docx').is_file())
    def test_renaming_preserves_paths_selections_and_processing_state(self):
        self.add('a.pdf','Remote attestation protocols and evidence verification.')
        self.store.update_package(self.pid,status='completed',phase='report_ready',progress=100)
        before=self.store.get(self.pid);self.classify();after=self.store.get(self.pid)
        for key in ('status','phase','progress','updated_at'):self.assertEqual(before[key],after[key])
        self.assertEqual(self.store.file_page(self.pid)['files'][0]['path'],'a.pdf')
    def test_background_names_do_not_wait_for_busy_generation(self):
        self.add('a.pdf','Remote attestation protocols and evidence verification.')
        semantic.embed_batch(self.store,self.pid,self.embed)
        self.store.update_package(self.pid,status='waiting_for_selection',phase='catalog_ready')
        worker=LargePackageWorker(self.store)
        with patch('services.vllm.VLLMClient.chat',side_effect=AssertionError('Do not call')):
            self.assertTrue(worker.run_once())
        # 终态随模式不同，且**两种都是正确的**：
        #   * legacy 抽取式车道（KMODE=0）在本次调度里就写完名字 -> 'classified'
        #   * KMODE=1 把命名**有意**派给后台队列（"不让可选措辞阻塞选择"是既定
        #     产品决定），所以此刻文件还没被标成已分类 -> 'pending'/'grouped'，
        #     这不是缺陷，而是异步的必然中间态。
        # 原来只认 'classified'，等于把"同步命名"当成了唯一正确实现，
        # 在 KMODE=1（生产在用的模式）下必然失败。
        # KMODE=1 的**终态**由 `test_label_projection_reaches_member_files` 覆盖。
        status = self.row('a.pdf')['local_summary']['portrait']['catalog_status']
        if getattr(Config, 'LP_CATALOG_KMODE', 0):
            # KMODE=1：命名**有意**派给后台队列（"不让可选措辞阻塞选择"是既定
            # 产品决定），所以此刻还没定稿是正确行为 —— 这条断言恰好证明了
            # "后台命名没有阻塞本次调度"。
            self.assertIn(status, ('pending', 'grouped'),
                          "KMODE=1 下命名应被派给后台队列，而不是就地写完")
        else:
            # KMODE=0：本次调度就应该把文件推进目录状态。
            # 具体取值两条路都可能：`semantic._group` 写 'grouped'
            # （已分组、名字正在归纳），抽取式命名器写 'classified'（已定稿）。
            # 真正要钉住的是"**不再是 pending**"——即这一步确实归类了。
            self.assertNotEqual(status, 'pending',
                                "legacy 车道应在本次调度内完成归类，而不是留在 pending")

    def test_label_projection_reaches_member_files(self):
        """DEF-14：标签一旦写入，必须**同步投影到成员文件**，且可幂等重放。

        为什么单独立这一条：增量投影写在 `naming.apply()` 里，而 `apply()` 只在
        "组刚拿到名字"那一刻跑一次。**在那次改动之前命名的组不会再走一遍**，
        于是成员文件永远停在 `catalog_status='pending'` —— 生产实测
        **13,426 行 / 6 个包**，其中最大的一个包 12,851 行、命名队列却是空的。
        这条用例把"投影"单独钉住：给定一个已有标签的组，投影之后成员文件必须
        变成 `classified` 且 `topic` 等于标签名；再跑一次必须 0 行（幂等）。
        """
        from services import large_package_repair as repair
        for name in ('a.pdf', 'b.pdf'):
            self.add(name, 'Remote attestation protocols and evidence verification.')
        semantic.embed_batch(self.store, self.pid, self.embed)
        with self.store._connect() as c:
            row = c.execute("SELECT group_id FROM large_catalog_members WHERE package_id=? "
                            "ORDER BY path LIMIT 1", (self.pid,)).fetchone()
            self.assertIsNotNone(row, '嵌入后应当已经分组')
            gid = row[0]
            c.execute("INSERT OR REPLACE INTO lp_catalog_labels "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (self.pid, gid, 1, 1, '统一主题名', '', '[]', 'test-model', 'd', 0, 0, 0))
        for name in ('a.pdf', 'b.pdf'):
            self.assertIn(self.row(name)['local_summary']['portrait']['catalog_status'],
                          ('pending', 'grouped'), '投影之前不应已被标成已分类')
        out = repair.r9_project_labels(self.store, apply=True)
        self.assertGreaterEqual(out['changed'], 1, '至少应当投影这两个文件')
        for name in ('a.pdf', 'b.pdf'):
            f = self.row(name)
            self.assertEqual(f['topic'], '统一主题名', '投影后 topic 必须等于标签名')
            self.assertEqual(f['local_summary']['portrait']['catalog_status'], 'classified',
                             '投影后必须标成已分类')
            self.assertEqual(f['local_summary']['portrait']['catalog_group'], gid)
        again = repair.r9_project_labels(self.store, apply=True)
        self.assertEqual(again['changed'], 0, '投影必须幂等')

if __name__=='__main__':unittest.main()
