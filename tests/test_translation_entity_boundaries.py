import unittest

from services.translation import protect_text


class TranslationEntityBoundaryTests(unittest.TestCase):
    def test_capitalized_words_across_rows_and_cells_remain_translatable(self):
        for separator in ('\n', '\r\n', '\r', '\t', '|'):
            with self.subTest(separator=repr(separator)):
                source = 'Status' + separator + 'Pump'
                protected = protect_text(source)
                self.assertEqual(source, protected.text)
                self.assertEqual({}, protected.replacements)

    def test_real_table_preserves_numbers_without_preserving_neighboring_labels(self):
        source = ('Item | Quantity | Status\nPump | 12 | Approved\n'
                  'Valve | 7 | Not approved\nTotal approved quantity: 12.')
        protected = protect_text(source)
        self.assertEqual(['12', '7', '12'], list(protected.replacements.values()))
        self.assertIn('Status\nPump', protected.text)
        self.assertIn('Approved\nValve', protected.text)
        self.assertEqual(source, protected.restore(protected.text))

    def test_inline_names_and_existing_facts_are_still_protected(self):
        source = ('Microsoft Corporation asked John Smith to send 12 copies on '
                  '2026-10-20 to procurement@example.org using ${reference}.')
        protected = protect_text(source)
        self.assertEqual({'Microsoft Corporation', 'John Smith', '12',
                          '2026-10-20', 'procurement@example.org', '${reference}'},
                         set(protected.replacements.values()))
        self.assertEqual(source, protected.restore(protected.text))


if __name__ == '__main__':
    unittest.main()
