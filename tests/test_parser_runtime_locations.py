"""Actual helper imports and scratch files must survive external working dirs."""
from pathlib import Path
from types import SimpleNamespace
import tempfile

from config import Config
from services.parse_temp import temporary_parse_directory
from services.mail.extraction import extract_attachment, extract_ocr


def test_parser_scratch_ignores_system_temp_and_cleans_instance_files(tmp_path, monkeypatch):
    root=tmp_path/'实例 中文 临时'
    monkeypatch.setattr(Config,'PARSE_TEMP_DIR',root)
    def forbidden():
        raise AssertionError('system TEMP must not be consulted')
    monkeypatch.setattr(tempfile,'gettempdir',forbidden)
    with temporary_parse_directory(prefix='actual-parser-') as scratch:
        path=Path(scratch)
        assert path.parent == root
        (path/'结果.txt').write_text('实际写入',encoding='utf8')
    assert not path.exists()
    assert list(root.iterdir()) == []


def test_isolated_attachment_imports_from_external_cwd_without_pythonpath(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('PYTHONPATH',raising=False)
    monkeypatch.setattr(Config,'MAIL_ENABLE_ISOLATED_PARSING',True)
    monkeypatch.setattr(Config,'PARSE_TEMP_DIR',tmp_path/'实例 解析')
    part=SimpleNamespace(raw_payload='外部目录 附件 125000'.encode('utf8'),
                         content_type='text/plain',filename_raw='中文 附件.txt',charset='utf8')
    result=extract_attachment(part)
    assert result['status']=='ready'
    assert result['text']=='外部目录 附件 125000'
    assert list(Config.PARSE_TEMP_DIR.iterdir()) == []


def test_ocr_helper_uses_application_cwd_and_instance_scratch(tmp_path, monkeypatch):
    from services.mail import extraction, ocr
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Config,'PARSE_TEMP_DIR',tmp_path/'实例 解析')
    monkeypatch.setattr(ocr,'available',lambda _:True)
    def child(command,**kwargs):
        import json
        assert kwargs['cwd']==str(Path(extraction.__file__).resolve().parents[2])
        scratch=Path(command[3]).parent
        assert scratch.parent==Config.PARSE_TEMP_DIR
        (scratch/'ocr-result.json').write_text(json.dumps({'status':'ready','text':'fixture'}),encoding='utf8')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr('subprocess.run',child)
    part=SimpleNamespace(raw_payload=b'PDF fixture',content_type='application/pdf',filename_raw='中文 扫描.pdf')
    assert extract_ocr(part,object())['status']=='ready'
    assert list(Config.PARSE_TEMP_DIR.iterdir()) == []
