"""Capability contract must match execution routes and honest diagnostics."""
from services import file_capabilities as caps
from services.import_pipeline import parse_plan


def test_compressed_suffixes_and_plan_are_consistent():
    for name, expected in [('REPORT.CSV.GZ', '.gz'), ('report.TAR.BZ2', '.tar.bz2'),
                           ('report.tar.xz', '.tar.xz'), ('report.tbz', '.tbz'),
                           ('report.tbz2', '.tbz2'), ('report.7Z', '.7z')]:
        assert caps.file_extension(name) == expected
        assert parse_plan({'path': name, 'extension': '.bz2'})['parser'] == 'archive_members'
    assert parse_plan({'path': 'settings.ini'})['parser'] == 'text_full'
    assert parse_plan({'path': 'settings.cfg'})['parser'] == 'text_full'
    assert parse_plan({'path': 'unsupported.odt'})['parser'] == 'metadata_only'
    assert parse_plan({'path': 'unsupported.ods'})['parser'] == 'metadata_only'


def test_no_dependency_means_no_available_claim(monkeypatch):
    monkeypatch.setattr(caps, '_module_available', lambda name: False)
    monkeypatch.setattr(caps, '_legacy_tool', lambda name: False)
    monkeypatch.setattr(caps.shutil, 'which', lambda name: None)
    rows = {r['extension']: r for r in caps.capability_matrix()['formats']}
    for ext in ('.pdf', '.docx', '.pptx', '.xls', '.rar', '.7z', '.msg', '.pst', '.png'):
        assert rows[ext]['availability'] == 'dependency_missing'
        assert rows[ext]['missing_dependencies']
    assert rows['.txt']['availability'] == 'available'
    assert rows['.mbox']['mail_source'] and not rows['.mbox']['deep_parse']
    assert rows['.mbx']['mail_source'] and not rows['.mbx']['preview']
    assert not rows['.zip']['mail_attachment']
    assert not rows['.eml']['mail_attachment']


def test_modules_and_models_do_not_claim_execution_verification():
    matrix = caps.capability_matrix()
    assert matrix['ocr']['verification'] == 'presence_only'
    assert matrix['preview_is_bounded'] is True
    assert matrix['unknown_formats'] == 'inventory_only'
    assert 'timeout' in matrix['result_statuses']
