"""Substantive isolated PDF evidence survives line numbers and publication notices."""
from services import large_package_evidence as evidence
from services.large_package_catalog import material
from services.large_package_catalog_profile import filter_meta
from services import large_package_catalog_naming as naming
from unittest.mock import MagicMock, patch
from types import SimpleNamespace


def test_long_pdf_page_retains_body_instead_of_pure_line_numbers():
    sentences = [
        'Conditional variational autoencoders identify muscle mechanisms and therapeutic targets through Bayesian parameter inference.',
        'The model estimates muscle force and validates the predictions against experimental measurements.',
        'Patient measurements support a treatment response analysis with uncertainty estimates.',
        'Intervention experiments reveal parameter combinations that alter the predicted force generation.',
        'Independent validation measures treatment outcomes across different muscle conditions.',
    ]
    # Mirrors retained scientific PDF pages with independent line-number runs.
    page = '\n'.join(str(i) for i in range(249, 499)) + '\nControl\nSupplement\n' + '\n'.join(sentences * 5)
    parts = []
    evidence.record(parts, 'PDF 第 3 页', page)
    assert parts
    assert any('Bayesian parameter inference' in p['text'] for p in parts)
    assert not any(p['text'].strip().isdigit() for p in parts)
    assert parts[0]['text'] not in {'Control', 'Supplement'}
    assert sum(len(p['text']) + 1 for p in parts) <= 1804
    info = {'evidence': evidence.select_blocks(parts, limit=16, chars=9600), 'fields': [], 'headings': []}
    source = material({'local_summary': {'sample': evidence.compact_text(parts, chars=4000), 'portrait': info}})
    assert source is not None
    assert 'Bayesian' in source[0]['text']


def test_numbers_with_substantive_units_amounts_dates_and_structured_records_survive():
    records = [
        {'text': '2026-10-11', 'kind': 'date', 'location': '记录日期'},
        {'text': '剂量 20 mg，每日两次', 'kind': 'text', 'location': '治疗条件'},
        {'text': '收入人民币 1000 元，成本人民币 600 元', 'kind': 'text', 'location': '财务事实'},
        {'text': '249', 'kind': 'record', 'location': '表格销售额'},
        {'text': '2026', 'kind': 'fields', 'location': '表头年度'},
    ]
    selected = evidence.select_blocks(records, limit=8, chars=4000)
    assert {p['text'] for p in selected} == {p['text'] for p in records}


def test_pdf_page_and_line_numbers_do_not_become_naming_evidence():
    parts = [{'text': '\n'.join(['1', '65', '249', '250', '1117', 'Page 9 of 36',
                                'Muscle responses reveal therapeutic targets and model uncertainty.']),
              'kind': 'text', 'location': 'PDF 第 1 页'}]
    selected = evidence.select_blocks(parts)
    assert len(selected) == 1
    assert selected[0]['text'].startswith('Muscle responses')


def test_exact_preprint_notices_are_not_used_as_substantive_signals():
    notices = [
        '.CC-BY 4.0 International licensemade available under a',
        '.CC-BY-NC-ND 4.0 International licenseavailable under a',
        'The copyright holder for this preprintthis version posted April 14, 2024.',
        '(which was not certified by peer review) is the author/funder, who has granted bioRxiv a license to display the preprint in perpetuity.',
        'https://doi.org/10.1101/2024.03.28.587296doi: bioRxiv preprint',
    ]
    for text in notices:
        assert evidence.is_boilerplate(text), text
    substantive = 'Cardiac stem cell differentiation is regulated by the Desmin-SPARC signaling axis.'
    parts = [{'text': '\n'.join(notices + [substantive]), 'kind': 'text', 'location': 'PDF 第 1 页'}]
    chosen = evidence.select_blocks(parts)
    assert chosen
    assert all(not evidence.is_boilerplate(p['text']) for p in chosen)
    assert any(substantive == p['text'] for p in chosen)


def test_copyright_and_license_research_body_is_preserved():
    body = [
        'We compare CC-BY licenses and copyright policy to evaluate how open access changes scientific reuse.',
        'Copyright holders in biomedical publishing respond to different economic incentives.',
        'The study examines peer review and publisher licensing contracts using cross-country policy data.',
    ]
    for text in body:
        assert not evidence.is_boilerplate(text)
    selected = evidence.select_blocks([{'text': '\n'.join(body), 'kind': 'text', 'location': '正文'}])
    assert {p['text'] for p in selected} == set(body)


def test_existing_short_note_is_not_reduced_or_misrepresented_as_full_document():
    parts = []
    evidence.record(parts, '段落 1', '销售增长 12%，目标 15%。仍需核验下一季度。', 'text')
    assert parts[0]['text'] == '销售增长 12%，目标 15%。仍需核验下一季度。'
    assert parts[0]['preview_only'] is True
    assert parts[0]['reader_version'] == evidence.VERSION
    assert evidence.compact_text(parts) == parts[0]['text']


def test_metadata_cannot_reintroduce_removed_preprint_notices():
    body = 'Turner syndrome development involves inactive chromosome regulation in human stem cells.'
    headings = ['.CC-BY-NC-ND 4.0 International licenseavailable under a',
                'Page 1 of 30', 'bioRxiv 8 of 34', 'Turner syndrome development']
    assert filter_meta(headings, [], 3, source_text=body, kind='heading') == ['Turner syndrome development']
    assert filter_meta(['preprint', 'university', 'biorxiv', 'doi', 'Turner'], [], 4,
                       source_text=body) == ['university', 'Turner']
    row = {'local_summary': {'portrait': {'evidence': [{'text': body, 'kind': 'text'}],
                                         'headings': headings, 'fields': []}}}
    card = evidence.make_card(row)
    assert card['readable']
    assert card['headings'] == ['Turner syndrome development']
    assert not any('license' in term or 'biorxiv' in term for term in card['terms'])


def test_publication_words_remain_when_the_research_body_supports_them():
    body = 'We compare copyright license policies and preprint server reuse to evaluate scientific access.'
    keywords = ['copyright', 'license', 'preprint', 'reuse']
    assert filter_meta(keywords, [], 4, source_text=body) == keywords
    assert filter_meta(['2026', '249'], [], 4, source_text=body) == ['2026', '249']
    assert filter_meta(['Copyright policy and license economics'], [], 3,
                       source_text=body, kind='heading') == ['Copyright policy and license economics']


def test_real_extracted_cc_prefix_and_interleaved_table_notice_are_removed():
    notices = [
        'CC-BY-ND 4.0The copyright holder for this preprint (which was not certified by peer review) is the author/funder, who has granted bioRxiv a license to display the preprint in perpetuity.',
        'Table I was not certified by peer review) is the author/funder.',
    ]
    body = 'Metabolic perturbations to the cell-free system reveal a trade-off in protein production.'
    parts = [{'text': '\n'.join(notices + [body]), 'kind': 'text'}]
    assert evidence.select_blocks(parts)[0]['text'] == body
    assert len(evidence.select_blocks(parts)) == 1


def test_naming_bundle_filters_metadata_using_the_actual_excerpt():
    body = 'Turner syndrome development involves inactive chromosome regulation in human stem cells.'
    row = {'path': 'turner.pdf', 'local_summary': {'portrait': {
        'evidence': [{'text': body, 'kind': 'text', 'location': 'PDF 第 1 页'}]}}}
    value = {'text': body, 'headings': ['.CC-BY-NC-ND 4.0 International licenseavailable under a'],
             'fields': [], 'keywords': ['preprint', 'biorxiv', 'doi', 'Turner']}
    group = {'member_count': 20, 'content_revision': 1, 'model': 'fixture', 'anchor': b'', 'coherence': .5}
    profile = {'common': [], 'distinctive': [], 'minority': [], 'templates': [], 'meta_templates': []}
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = ['owner']
    store = SimpleNamespace(_connect=lambda: conn)
    with patch.object(naming, 'representatives', return_value=([(row, value)], group)), \
         patch('services.large_package_catalog_profile.group_profile', return_value=profile), \
         patch.object(naming, 'held_out_members', return_value=[]), \
         patch.object(naming, 'bundle_key', return_value='fixture'), \
         patch.object(naming, 'settings', return_value={'input': 20000}), \
         patch.object(naming, 'token_count', side_effect=lambda t: len(str(t))):
        core = naming._bundle_core_impl(store, 'package', 'group')
    actual = core['payload']['evidence'][0]
    assert actual['text'] == body
    assert actual['headings'] == []
    assert actual['keywords'] == ['Turner']
    assert 'licenseavailable' not in core['user']
    assert 'biorxiv' not in core['user']
