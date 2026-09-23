import hashlib
import json
import os
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from config import Config
from large_package_worker import LargePackageWorker, _normalize_unicode


class DeepUnicodeTests(unittest.TestCase):
    def test_valid_unicode_is_preserved_and_normalization_is_idempotent(self):
        valid = '\u4e2d\u6587 \U0001f600 \U00020000 \u03b1 \u0e44\u0e17\u0e22'
        self.assertEqual(_normalize_unicode(valid), valid)
        bad = {'text': valid + '\ud83d\ude00 X\ud800Y\udfffZ',
               'evidence': [{'text': '\ud800broken'}], 'number': 2, 'none': None}
        clean = _normalize_unicode(bad)
        self.assertEqual(clean['text'], valid + '\U0001f600 X\ufffdY\ufffdZ')
        self.assertEqual(clean['evidence'][0]['text'], '\ufffdbroken')
        self.assertEqual(_normalize_unicode(clean), clean)
        json.dumps(clean, ensure_ascii=False).encode('utf-8')
        self.assertIn('\ud800', bad['evidence'][0]['text'])

    def test_real_deep_parse_and_atomic_commit_accept_surrogate_evidence(self):
        from services.large_package_isolated import LargePackageStore
        with tempfile.TemporaryDirectory() as folder, patch.object(Config, 'SCAN_ALLOWED_ROOTS', ()), \
                patch.object(Config, 'LP_DEEP_CONCURRENCY', 1), \
                patch.dict(os.environ, {'LARGE_PACKAGE_MODEL_SUMMARY': '1'}):
            root = Path(folder)
            store = LargePackageStore(root / 'state.db')
            pid = store.create(root)['id']
            path = 'fixture.pdf'
            (root / path).write_bytes(b'parser fixture')
            store.upsert_file(pid, {'path': path, 'name': path})
            with closing(store._connect()) as conn:
                conn.execute("INSERT INTO queue(package_id,path,stage,status,updated_at) VALUES(?,?,'deep','queued',?)",
                             (pid, path, time.time()))
            token = store.claim('deep', 1, pid)[0][2]
            worker = LargePackageWorker(store=store)
            raw = {
                'text': 'Bridge matching evidence \ud800 and valid pair \ud83d\ude00. ' * 4,
                'source': {'sha256': 'original-source-hash'},
                'evidence': [{'text': 'Evidence \udfff', 'source_sha256': 'original-source-hash',
                              'content_sha256': 'old-content-hash'}],
                'coverage': {'complete': True}, 'parser': {'name': 'fixture'},
            }
            worker._parser = Mock()
            worker._parser.parse.return_value = raw
            def summary(_path, text):
                text.encode('utf-8')
                self.assertIn('\U0001f600', text)
                return {'summary_type': 'model', 'summary': 'Model result \ud800',
                        'quality': 'complete', 'coverage': 'sampled'}
            with patch.object(worker, '_model_summary', side_effect=summary), \
                    patch.object(worker, '_catalog_incremental'):
                result = worker._deep_worker(pid, str(root), path, token)
            self.assertTrue(result[1], result)
            saved = store.get_file(pid, path)
            self.assertEqual(saved['deep_status'], 'completed')
            self.assertEqual(store.stage_counts(pid, 'deep'), {'completed': 1})
            deep = saved['deep_summary']
            json.dumps(deep, ensure_ascii=False).encode('utf-8')
            self.assertEqual(saved['sha256'], 'original-source-hash')
            self.assertEqual(deep['evidence'][0]['source_sha256'], 'original-source-hash')
            self.assertEqual(deep['evidence'][0]['content_sha256'],
                             hashlib.sha256(deep['evidence'][0]['text'].encode('utf-8')).hexdigest())
            self.assertEqual(deep['summary'], 'Model result \ufffd')
            self.assertTrue(deep['text_normalization']['source_unchanged'])
            self.assertIn('\ud800', raw['text'])
            self.assertEqual(store.stage_counts(pid, 'catalog'), {'queued': 1})
            worker.preview_runner.close()


if __name__ == '__main__':
    unittest.main()
