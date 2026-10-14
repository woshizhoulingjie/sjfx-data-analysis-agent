"""OCR API compatibility and truthful failure states found with MCS scans."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from config import Config

from services.mail.ocr import _rapid_engine, _rapid_models, _rapid_result, _rapid_text, extract_result, rapid_available
from services.mail.extraction import extract_ocr


class _FakePage:
    def render(self, **kwargs):
        return self
    def to_pil(self):
        return self
    def save(self, path):
        Path(path).write_bytes(b'image fixture')
    def close(self):
        pass


class _FakePDF:
    def __len__(self):
        return 2
    def __getitem__(self, index):
        return _FakePage()
    def close(self):
        pass


class MailOCRCompatibilityTests(unittest.TestCase):
    def test_auto_selects_local_server_recognition_and_keeps_mobile_detection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('ch_PP-OCRv5_det_mobile.onnx', 'ch_PP-OCRv5_rec_mobile.onnx',
                         'ch_ppocr_mobile_v2.0_cls_mobile.onnx', 'ch_PP-OCRv5_rec_server.onnx'):
                (root / name).write_bytes(b'local model fixture')
            modules = {'config': SimpleNamespace(Config=SimpleNamespace(RAPIDOCR_MODEL_DIR=root, PARSE_TEMP_DIR=root / 'parse-temp'))}
            with patch.dict(sys.modules, modules), patch.dict(os.environ, {'MAIL_OCR_REC_MODEL': 'auto'}):
                selected = _rapid_models()
                self.assertEqual(selected[1], 'ch_PP-OCRv5_rec_server.onnx')
                self.assertEqual(selected[0], 'ch_PP-OCRv5_det_mobile.onnx')
                self.assertEqual(selected[2], 'ch_ppocr_mobile_v2.0_cls_mobile.onnx')
            with patch.dict(sys.modules, modules), patch.dict(os.environ, {'MAIL_OCR_REC_MODEL': 'mobile'}):
                self.assertEqual(_rapid_models()[1], 'ch_PP-OCRv5_rec_mobile.onnx')

    def test_missing_explicit_server_model_is_unavailable_without_mobile_downgrade(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('ch_PP-OCRv5_det_mobile.onnx', 'ch_PP-OCRv5_rec_mobile.onnx',
                         'ch_ppocr_mobile_v2.0_cls_mobile.onnx'):
                (root / name).write_bytes(b'local model fixture')
            modules = {'config': SimpleNamespace(Config=SimpleNamespace(RAPIDOCR_MODEL_DIR=root, PARSE_TEMP_DIR=root / 'parse-temp'))}
            with patch.dict(sys.modules, modules), patch('services.mail.ocr.importlib.util.find_spec', return_value=object()):
                with patch.dict(os.environ, {'MAIL_OCR_REC_MODEL': 'auto'}):
                    self.assertTrue(rapid_available())
                    self.assertEqual(_rapid_models()[1], 'ch_PP-OCRv5_rec_mobile.onnx')
                with patch.dict(os.environ, {'MAIL_OCR_REC_MODEL': 'server'}):
                    with self.assertRaises(RuntimeError):
                        rapid_available()
                    fallback = MagicMock()
                    isolated = {'services.parse_isolation': SimpleNamespace(runner_for=fallback)}
                    part = SimpleNamespace(filename_raw='scan.pdf',content_type='application/pdf',raw_payload=b'PDF fixture')
                    with patch.dict(sys.modules,isolated):
                        result=extract_ocr(part,object())
                    self.assertEqual(result['status'],'failed')
                    fallback.assert_not_called()
                with patch.dict(os.environ, {'MAIL_OCR_REC_MODEL': 'unknown-remote-model'}):
                    with self.assertRaises(ValueError):
                        rapid_available()

    def test_quality_reports_actual_local_server_model_without_altering_recognized_text(self):
        engine = MagicMock(return_value=SimpleNamespace(txts=['2026年10月17日前', '之日为准。'], scores=[.93,.92]))
        engine._mail_ocr_profile = {'engine': 'RapidOCR PP-OCRv5 server recognition / mobile detection',
                                   'recognition_model': 'ch_PP-OCRv5_rec_server.onnx', 'model_resolution': 'local files only'}
        text, quality = _rapid_result(engine, 'scan.png')
        self.assertEqual(text, '2026年10月17日前\n之日为准。')
        self.assertEqual(quality['recognition_model'], 'ch_PP-OCRv5_rec_server.onnx')
        self.assertEqual(quality['recognition_accuracy'], 'unverified')
        self.assertEqual(quality['bbox_coordinate_space'], 'input image pixels')
        self.assertEqual(quality['lines'][1]['char_start'], len('2026年10月17日前') + 1)

    def test_server_engine_bounds_padding_and_native_threads(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'ch_PP-OCRv5_rec_server.onnx').write_bytes(b'local model fixture')
            constructor = MagicMock(return_value=SimpleNamespace())
            cv2 = SimpleNamespace(setNumThreads=MagicMock(), ocl=SimpleNamespace(setUseOpenCL=MagicMock()))
            modules = {'config': SimpleNamespace(Config=SimpleNamespace(RAPIDOCR_MODEL_DIR=root)),
                       'rapidocr': SimpleNamespace(RapidOCR=constructor), 'cv2': cv2}
            with patch.dict(sys.modules, modules), patch.dict(os.environ, {'MAIL_OCR_REC_MODEL': 'auto'}), \
                    patch('services.mail.ocr.rapid_available', return_value=True):
                engine = _rapid_engine()
            params = constructor.call_args[1]['params']
            self.assertEqual(params['Rec.rec_batch_num'], 1)
            self.assertEqual(params['EngineConfig.onnxruntime.intra_op_num_threads'], 2)
            self.assertEqual(params['EngineConfig.onnxruntime.inter_op_num_threads'], 1)
            self.assertEqual(engine._mail_ocr_profile['recognition_model'], 'ch_PP-OCRv5_rec_server.onnx')
            self.assertEqual(engine._mail_ocr_profile['inference_threads'], 2)

    def test_ocr_subprocess_uses_separate_finite_timeout(self):
        import json
        def complete(command, **kwargs):
            Path(command[3]).with_name('ocr-result.json').write_text(json.dumps({'status': 'ready', 'text': 'exact OCR'}), encoding='utf-8')
            return SimpleNamespace(returncode=0)
        config = SimpleNamespace(MAIL_PARSE_TIMEOUT_SECONDS=60, MAIL_OCR_TIMEOUT_SECONDS=120, PARSE_TEMP_DIR=Config.PARSE_TEMP_DIR)
        modules = {'config': SimpleNamespace(Config=config),
                   'services.parse_isolation': SimpleNamespace(runner_for=MagicMock())}
        part = SimpleNamespace(filename_raw='scan.pdf', content_type='application/pdf', raw_payload=b'PDF fixture')
        with patch.dict(sys.modules, modules), patch('services.mail.ocr.available', return_value=True), \
                patch('subprocess.run', side_effect=complete) as run:
            result = extract_ocr(part, object())
        self.assertEqual(result['text'], 'exact OCR')
        self.assertEqual(run.call_args[1]['timeout'], 120)

    def test_local_cpu_engine_caps_native_thread_pools_within_memory_limit(self):
        constructor = MagicMock(return_value=object())
        cv2 = SimpleNamespace(setNumThreads=MagicMock(), ocl=SimpleNamespace(setUseOpenCL=MagicMock()))
        modules = {'config': SimpleNamespace(Config=SimpleNamespace(RAPIDOCR_MODEL_DIR='/local/models')),
                   'rapidocr': SimpleNamespace(RapidOCR=constructor), 'cv2': cv2}
        with patch.dict(sys.modules, modules), patch.dict(os.environ, {'MAIL_OCR_REC_MODEL': 'mobile'}), \
                patch('services.mail.ocr.rapid_available', return_value=True):
            _rapid_engine()
        cv2.setNumThreads.assert_called_once_with(1)
        cv2.ocl.setUseOpenCL.assert_called_once_with(False)
        params = constructor.call_args[1]['params']
        self.assertEqual(params['EngineConfig.onnxruntime.intra_op_num_threads'], 1)
        self.assertEqual(params['EngineConfig.onnxruntime.inter_op_num_threads'], 1)
        self.assertTrue(all(params['EngineConfig.onnxruntime.use_' + name] is False for name in ['cuda','dml','cann','coreml']))
        self.assertTrue(all(Path(params[k]).parent == Path('/local/models') for k in ['Det.model_path','Rec.model_path','Cls.model_path']))

    def test_current_rapid_output_dto_and_old_rows_api_recognize_same_text(self):
        old_rows = [[[0, 0, 2, 2], '审批单', .98], [[0, 3, 2, 5], 'HX-PR-2025', .97]]
        outputs = [SimpleNamespace(txts=['审批单', 'HX-PR-2025']), (old_rows, [.1, .2, .3]), old_rows]
        for value in outputs:
            with self.subTest(output_type=type(value).__name__):
                self.assertEqual(_rapid_text(lambda image: value, 'image.png'), '审批单\nHX-PR-2025')

    def test_both_rapid_apis_can_report_genuinely_empty_recognition(self):
        for value in [SimpleNamespace(txts=None), SimpleNamespace(txts=[]), (None, [.1, .2]), []]:
            self.assertEqual(_rapid_text(lambda image: value, 'image.png'), '')

    def test_unsupported_output_shape_raises_instead_of_claiming_no_text(self):
        for value in [object(), {'error': 'recognition failed'}, ([['bad row']], [.1])]:
            with self.assertRaises(TypeError):
                _rapid_text(lambda image: value, 'image.png')

    def _pdf_result(self, results):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'scan.pdf'; path.write_bytes(b'pdf fixture')
            fake_pdfium = SimpleNamespace(PdfDocument=lambda filename: _FakePDF())
            with patch.dict(sys.modules, {'pypdfium2': fake_pdfium}), \
                    patch('services.mail.ocr._rapid_engine', return_value=object()), \
                    patch('services.mail.ocr._recognize', side_effect=results):
                return extract_result(path, 500000, time.monotonic() + 10)

    def test_all_page_technical_failures_are_failed_and_do_not_count_newlines(self):
        result = self._pdf_result([AttributeError('private source detail'), RuntimeError('private source detail')])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['text'], '')
        self.assertEqual(result['coverage_ratio'], 0)
        self.assertEqual(result['error']['type'], 'MailOCRPageError')
        self.assertEqual([r['type'] for r in result['meta']['page_errors']], ['AttributeError', 'RuntimeError'])
        self.assertEqual([r['page_number'] for r in result['meta']['page_errors']], [1, 2])
        self.assertNotIn('private source detail', str(result))
        self.assertFalse(any('可能为空白页' in warning for warning in result['warnings']))

    def test_successful_blank_pages_keep_no_text_distinct_from_technical_failure(self):
        result = self._pdf_result([('', 'zh+eng', False), ('', 'zh+eng', False)])
        self.assertEqual(result['status'], 'ocr_no_text')
        self.assertEqual(result['text'], '')
        self.assertEqual(result['meta']['page_errors'], [])
        self.assertNotIn('error', result)

    def test_partial_page_failure_preserves_successful_text_and_page_numbers(self):
        result = self._pdf_result([('完整识别出的第一页', 'zh+eng', False), RuntimeError('second page failed')])
        self.assertEqual(result['status'], 'partial')
        self.assertIn('完整识别出的第一页', result['text'])
        self.assertEqual(result['coverage_ratio'], .5)
        self.assertEqual([row['page_number'] for row in result['units']], [1, 2])
        self.assertEqual(result['meta']['page_errors'][0]['stage'], 'recognition')

    def test_deadline_before_any_page_is_not_successful_no_text(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'scan.png'; path.write_bytes(b'image fixture')
            with patch('services.mail.ocr._rapid_engine', return_value=object()):
                result = extract_result(path, 500000, time.monotonic() - 1)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error']['type'], 'MailOCRIncomplete')
        self.assertFalse(result['meta']['traversal_complete'])


if __name__ == '__main__':
    unittest.main()
