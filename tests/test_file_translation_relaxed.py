import json
import unittest
from unittest.mock import Mock

from services.file_translation import build_file_translation_service
from services.translation import (
    InMemoryTranslationMemory, ProviderResponse, TranslationPolicy,
    TranslationProvider, TranslationService,
)


class FixedProvider(TranslationProvider):
    provider_id = 'offline-test:1'

    def __init__(self, output):
        self.output = output
        self.calls = 0

    def translate(self, text, source_language, target_language='zh-CN', **kwargs):
        self.calls += 1
        output = self.output(text) if callable(self.output) else self.output
        return ProviderResponse(output, model=self.provider_id)


def document(text):
    return {'text': text, 'structure': {'title': '测试文件'}}


class FileTranslationRelaxedTests(unittest.TestCase):
    def file_service(self, provider, memory=None, **kwargs):
        return build_file_translation_service(None, memory, provider=provider, **kwargs)

    def test_technical_terms_do_not_discard_readable_chinese_or_retry(self):
        source = 'Use the compute kernel with cuda stream scheduler and memory allocator in this operation.'
        target = '本操作使用 compute kernel、cuda stream scheduler 和 memory allocator。'
        provider = FixedProvider(target)
        relaxed = self.file_service(provider).translate_document(document(source))
        self.assertEqual(relaxed['status'], 'completed')
        self.assertEqual(relaxed['translated_text'], target)
        self.assertIn('target_contains_untranslated_text', relaxed['warnings'])
        self.assertEqual(provider.calls, 1)
        strict = TranslationService(FixedProvider(target)).translate_document(document(source))
        self.assertEqual(strict['status'], 'failed')

    def test_protection_differences_keep_existing_chinese_with_warning(self):
        source = 'Revenue increased by 12 percent during the year.'
        target = '本年度收入增长百分之十二。'
        result = self.file_service(FixedProvider(target)).translate_document(document(source))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['translated_text'], target)
        self.assertIn('protected_token_mismatch', result['warnings'])
        unit = next(u for u in result['units'] if u['translation_required'])
        self.assertEqual(unit['qa']['missing_protected_items'], 1)
        self.assertTrue(unit['qa']['passed'])
        strict = TranslationService(FixedProvider(target)).translate_document(document(source))
        self.assertEqual(strict['errors'][0]['code'], 'protected_token_mismatch')

    def test_existing_tokens_restore_even_when_one_is_missing(self):
        result = self.file_service(FixedProvider('本年度收入为 __SJFX_KEEP_0001__。')).translate_document(
            document('Revenue was 12 dollars in 2025.'))
        self.assertEqual(result['status'], 'completed')
        self.assertIn('12', result['translated_text'])
        self.assertNotIn('__SJFX_', result['translated_text'])
        self.assertIn('protected_token_mismatch', result['warnings'])

    def test_table_and_extreme_length_heuristics_are_advisory(self):
        cases = [
            ('first\tsecond\tthird', '第一、第二、第三', 'table_structure_changed'),
            ('ordinary text', '这是完整的中文译文。' * 20, 'implausible_length_ratio'),
        ]
        for source, target, warning in cases:
            with self.subTest(warning=warning):
                # Unit planning normally preserves separators itself. Exercise
                # a model-facing table unit to test the changed QA contract.
                unit = {'source_text': source, 'source_language': 'en',
                        'block_kind': 'table' if '\t' in source else 'paragraph',
                        'memory_key': 'test-unit'}
                relaxed = self.file_service(FixedProvider(target))
                self.assertTrue(relaxed._translate_unit(unit, {}))
                self.assertEqual(unit['target_text'], target)
                self.assertIn(warning, unit['qa']['warnings'])
                strict = TranslationService(FixedProvider(target))
                self.assertFalse(strict._translate_unit({
                    'source_text': source, 'source_language': 'en',
                    'block_kind': unit['block_kind'], 'memory_key': 'strict-unit',
                }, {}))

    def test_empty_foreign_echo_and_unknown_marker_still_fail(self):
        source = 'The ordinary document describes the delivery process.'
        for target in ['', source, 'A different foreign sentence with no Chinese.',
                       '这是中文译文 __SJFX_KEEP_9999__']:
            with self.subTest(target=target):
                result = self.file_service(FixedProvider(target), max_attempts=1).translate_document(document(source))
                self.assertEqual(result['status'], 'failed')
                self.assertIsNone(result['translated_text'])

    def test_glossary_expansion_does_not_make_echoed_text_chinese(self):
        result = self.file_service(FixedProvider(lambda text: text), max_attempts=1).translate_document(
            document('The TEE executes the ordinary document processing operation.'))
        self.assertEqual(result['status'], 'failed')

    def test_generating_and_offline_services_share_only_file_policy(self):
        client = Mock(configured=True, model='test-model')
        client.chat.return_value = {'content': json.dumps({'translation': '这份文件说明交付流程。'}), 'finish_reason': 'stop'}
        generated = build_file_translation_service(client, None, max_unit_chars=4800)
        provider = FixedProvider('这份文件说明交付流程。')
        offline = self.file_service(provider, max_unit_chars=4800, coalesce_paragraphs=False)
        for service in [generated, offline]:
            self.assertTrue(service.policy.relaxed_quality_checks)
            self.assertIn(':file:relaxed-v1:', service.policy.contract_version)
            self.assertEqual(service.translate_document(document('The ordinary document describes the delivery process.'))['status'], 'completed')
        self.assertIs(offline.provider, provider)
        self.assertEqual(offline.policy.max_unit_chars, 4800)
        self.assertFalse(offline.policy.coalesce_paragraphs)
        self.assertEqual(generated.policy.max_unit_chars, 720)
        self.assertFalse(TranslationPolicy().relaxed_quality_checks)

    def test_relaxed_memory_is_reused_only_by_relaxed_service(self):
        memory = InMemoryTranslationMemory()
        provider = FixedProvider('本操作使用 compute kernel、cuda stream scheduler 和 memory allocator。')
        service = self.file_service(provider, memory)
        source = document('Use the compute kernel with cuda stream scheduler and memory allocator in this operation.')
        first = service.translate_document(source)
        second = service.translate_document(source, resume_state=first)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(second['status'], 'completed')
        strict = TranslationService(provider, memory, policy=TranslationPolicy(max_attempts=1))
        self.assertEqual(strict.translate_document(source, resume_state=first)['status'], 'failed')
        self.assertEqual(provider.calls, 2)

    def test_stricter_successful_checkpoint_survives_file_policy_upgrade(self):
        provider = FixedProvider('这份文件说明交付流程。')
        source = document('The ordinary document describes the delivery process.')
        strict = TranslationService(provider)
        first = strict.translate_document(source)
        self.assertEqual(first['status'], 'completed')
        relaxed = self.file_service(provider).translate_document(source, resume_state=first)
        self.assertEqual(relaxed['status'], 'completed')
        self.assertEqual(provider.calls, 1)
        self.assertTrue(any(u.get('reused_from') == 'checkpoint' for u in relaxed['units']))

    def test_old_glossary_only_echo_is_rechecked_during_upgrade(self):
        for text in ['The TEE TEE TEE executes.', 'Enable TEE 1']:
            with self.subTest(source=text):
                provider = FixedProvider(lambda source: source)
                source = document(text)
                first = TranslationService(provider).translate_document(source)
                self.assertEqual(first['status'], 'completed')
                result = self.file_service(provider, max_attempts=1).translate_document(source, resume_state=first)
                self.assertEqual(result['status'], 'failed')
                self.assertIn('source_copied_without_translation', result['errors'][0]['message'])


if __name__ == '__main__':
    unittest.main()
