"""Protection denials are never converted into parser failure or model fallback."""
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sjfx_protection.store import ProtectionError
from services.mail import jobs
from services import large_qa_synthesis as synthesis
from services import package_analysis
from services.unified_parser import UnifiedDocumentParser


class DenialPropagationTests(unittest.TestCase):
    def setUp(self):
        self.denial=ProtectionError('source rejected',423,'context_stale')
        self.package={'package_id':'fixture','owner_id':'alice'}
        self.repo=MagicMock()
        self.item={'message_key':'message','mime_path':'part'}
        self.repo.pending_attachments.return_value=[self.item]
        self.services=SimpleNamespace(repository=self.repo,policy=MagicMock(),parser=MagicMock())
        self.job={'id':'job','options':{},'owner_id':'alice'}

    def test_attachment_denial_does_not_store_parse_failure(self):
        with patch.object(jobs,'_package',return_value=self.package),patch.object(jobs,'_active'),patch.object(jobs,'read_part',side_effect=self.denial):
            with self.assertRaises(ProtectionError):
                jobs._extract(self.job,self.services)
        self.repo.store_extraction.assert_not_called()
        self.repo.save_progress.assert_not_called()

    def test_ocr_denial_does_not_store_retry_failure(self):
        self.repo._connection.return_value.__enter__.return_value.execute.return_value.__iter__.return_value=[self.item]
        with patch.object(jobs,'_package',return_value=self.package),patch.object(jobs,'_active'),patch.object(jobs,'read_part',side_effect=self.denial):
            with self.assertRaises(ProtectionError):
                jobs._ocr(self.job,self.services)
        self.repo.store_extraction.assert_not_called()

    def test_embedding_denial_does_not_enqueue_model_retry(self):
        with patch.object(jobs.Config,'MAIL_ENABLE_VECTOR_INDEX',True),patch.object(jobs,'_package',return_value=self.package),patch('services.mail.semantic.embed_segments_batch',side_effect=self.denial):
            with self.assertRaises(ProtectionError):
                jobs._embed(self.job,self.services)
        self.repo.save_progress.assert_not_called()

    def test_qa_synthesis_denial_does_not_return_extractive_fallback(self):
        with patch.object(synthesis,'_cache_read',return_value=None),patch.object(synthesis,'qa_model_text',side_effect=self.denial),patch.object(synthesis,'_fallback') as fallback:
            with self.assertRaises(ProtectionError):
                synthesis._map(MagicMock(),'key',[],{'S1':{'text':'approved evidence'}},'query',MagicMock(),time.monotonic()+60)
        fallback.assert_not_called()

    def test_serial_and_parallel_parse_denials_never_commit_file_failure(self):
        @contextmanager
        def source(*args,**kwargs):
            yield Path('unused.txt')
        parser=UnifiedDocumentParser()
        for workers in (1,2):
            with self.subTest(workers=workers),patch.object(package_analysis,'_logical_source_snapshot',source),patch.object(package_analysis,'_parse_with_timeout_fallback',side_effect=self.denial):
                complete=MagicMock()
                with self.assertRaises(ProtectionError):
                    package_analysis._parallel_parse_files(parser,[{'path':'a.txt'},{'path':'b.txt'}],Path('.'),'fast',max_workers=workers,on_complete=complete)
                complete.assert_not_called()


if __name__=='__main__':
    unittest.main()
