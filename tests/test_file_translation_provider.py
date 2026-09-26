import unittest
from unittest.mock import Mock

from services.file_translation import VLLMFileTranslationProvider, build_file_translation_service
from services.translation import TranslationPolicy, TranslationProviderError


class FileTranslationProviderTests(unittest.TestCase):
    def client(self, content, finish_reason='stop'):
        client = Mock(configured=True, model='test-model')
        client.chat.return_value = {'content': content, 'finish_reason': finish_reason, 'model': 'test-model'}
        return client

    def test_valid_json_at_token_limit_is_not_complete(self):
        provider = VLLMFileTranslationProvider(self.client('{"translation":"译文只写了一半"}', 'length'))
        with self.assertRaises(TranslationProviderError) as caught:
            provider.translate('The complete document has more content.', 'en')
        self.assertEqual(caught.exception.code, 'output_truncated')

    def test_invalid_json_at_token_limit_remains_retryable(self):
        provider = VLLMFileTranslationProvider(self.client('{"translation":"未完成', 'length'))
        with self.assertRaises(TranslationProviderError) as caught:
            provider.translate('Full source.', 'en')
        self.assertEqual(caught.exception.code, 'output_truncated')
        self.assertTrue(caught.exception.retryable)

    def test_normal_translation_preserves_paragraphs(self):
        import json
        text = '第一段。' + chr(10) * 2 + '第二段。'
        provider = VLLMFileTranslationProvider(self.client(json.dumps({'translation': text})))
        response = provider.translate('First paragraph. Second paragraph.', 'en')
        self.assertEqual(response.text, text)

    def test_file_policy_does_not_settle_failed_prose_as_complete(self):
        service = build_file_translation_service(self.client(''), None)
        self.assertFalse(service.policy.preserve_untranslated_tail)
        self.assertTrue(service.policy.preserve_figure_blocks)
        self.assertIn(':file:strict-v2:', service.policy.contract_version)

    def test_shared_mail_and_import_policy_remains_unchanged(self):
        policy = TranslationPolicy()
        self.assertFalse(policy.preserve_untranslated_tail)
        self.assertFalse(policy.preserve_figure_blocks)
        self.assertNotIn(':file:strict-v2:', policy.contract_version)
