"""Thai OCR resolves local platform bundles without invoking native OCR."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from config import Config
from services.mail import thai_ocr


@pytest.fixture
def resources(tmp_path, monkeypatch):
    tools = tmp_path / '工具 目录'
    legacy = tmp_path / '历史 项目' / 'services' / 'mail' / '_ocr'
    monkeypatch.setattr(Config, 'TOOLS_DIR', tools)
    monkeypatch.setattr(thai_ocr, '__file__', str(legacy.parent / 'thai_ocr.py'))
    return tools, legacy


def bundle(root, windows, *, bin_dir=False):
    binary = ((root / 'bin' if bin_dir else root) / 'tesseract.exe' if windows
              else root / 'usr' / 'bin' / 'tesseract')
    tessdata = (root / 'tessdata' if windows
                else root / 'usr' / 'share' / 'tesseract-ocr' / '5' / 'tessdata')
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b'synthetic tool, never executed')
    tessdata.mkdir(parents=True, exist_ok=True)
    for name in ('tha.traineddata', 'eng.traineddata'):
        (tessdata / name).write_bytes(b'synthetic local language model')
    libraries = root / 'usr' / 'lib' / 'x86_64-linux-gnu'
    if not windows:
        libraries.mkdir(parents=True, exist_ok=True)
    return binary, tessdata, libraries


@pytest.mark.parametrize('windows, folder, bin_dir', [
    (True, 'tesseract', False), (True, 'mail-ocr', True),
    (False, 'tesseract', False), (False, 'mail-ocr', False),
])
def test_external_tool_layout_uses_same_resources_for_availability_and_execution(resources, monkeypatch, windows, folder, bin_dir):
    tools, _ = resources
    binary, tessdata, libraries = bundle(tools / folder, windows, bin_dir=bin_dir)
    monkeypatch.setattr(thai_ocr, '_is_windows', lambda: windows)
    monkeypatch.setattr(thai_ocr.time, 'monotonic', lambda: 100.0)
    monkeypatch.setenv('OMP_NUM_THREADS', '99')
    monkeypatch.setenv('OMP_THREAD_LIMIT', '99')
    monkeypatch.setenv('TESSDATA_PREFIX', 'unrelated models')
    monkeypatch.setenv('LD_LIBRARY_PATH', 'previous library path')
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout='ข้อความภาษาไทย English'.encode('utf-8')))
    monkeypatch.setattr(thai_ocr.subprocess, 'run', run)
    image = tools.parent / '泰文 图像.png'
    assert thai_ocr.available()
    assert thai_ocr.ocr_image(image, 105.0) == 'ข้อความภาษาไทย English'
    args, kwargs = run.call_args
    assert args[0] == [str(binary), str(image), 'stdout', '-l', 'tha+eng', '--psm', '3']
    assert kwargs['timeout'] == 5.0
    assert kwargs['capture_output'] is True
    assert kwargs['env']['TESSDATA_PREFIX'] == str(tessdata)
    assert kwargs['env']['OMP_NUM_THREADS'] == kwargs['env']['OMP_THREAD_LIMIT'] == '1'
    if windows:
        assert 'LD_LIBRARY_PATH' not in kwargs['env']
    else:
        assert kwargs['env']['LD_LIBRARY_PATH'].startswith(str(libraries) + ':')
        assert kwargs['env']['LD_LIBRARY_PATH'].endswith(':previous library path')
    # Child configuration must not overwrite other model processes' variables.
    assert thai_ocr.os.environ['OMP_NUM_THREADS'] == '99'
    assert thai_ocr.os.environ['TESSDATA_PREFIX'] == 'unrelated models'
    assert thai_ocr.os.environ['LD_LIBRARY_PATH'] == 'previous library path'


def test_historical_linux_bundle_remains_available(resources, monkeypatch):
    _, legacy = resources
    binary, tessdata, libraries = bundle(legacy, False)
    monkeypatch.setattr(thai_ocr, '_is_windows', lambda: False)
    monkeypatch.delenv('LD_LIBRARY_PATH', raising=False)
    monkeypatch.setattr(thai_ocr.time, 'monotonic', lambda: 0.0)
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=b'legacy'))
    monkeypatch.setattr(thai_ocr.subprocess, 'run', run)
    assert thai_ocr.available()
    assert thai_ocr.ocr_image('input.png', 2.0) == 'legacy'
    assert run.call_args.args[0][0] == str(binary)
    assert run.call_args.kwargs['env']['TESSDATA_PREFIX'] == str(tessdata)
    assert run.call_args.kwargs['env']['LD_LIBRARY_PATH'] == ':'.join(
        (str(libraries), str(legacy / 'usr' / 'lib')))


def test_external_complete_bundle_precedes_historical_resources(resources, monkeypatch):
    tools, legacy = resources
    binary, _, _ = bundle(tools / 'tesseract', False)
    bundle(legacy, False)
    monkeypatch.setattr(thai_ocr, '_is_windows', lambda: False)
    assert thai_ocr._resolve_resources()[0] == binary


@pytest.mark.parametrize('windows', [True, False])
@pytest.mark.parametrize('missing', ['binary', 'tha.traineddata', 'eng.traineddata'])
def test_missing_binary_or_language_is_unavailable_and_never_launches(resources, monkeypatch, windows, missing):
    tools, _ = resources
    binary, tessdata, _ = bundle(tools / 'mail-ocr', windows)
    (binary if missing == 'binary' else tessdata / missing).unlink()
    monkeypatch.setattr(thai_ocr, '_is_windows', lambda: windows)
    run = Mock()
    monkeypatch.setattr(thai_ocr.subprocess, 'run', run)
    assert not thai_ocr.available()
    with pytest.raises(RuntimeError, match='Tesseract.*tha/eng'):
        thai_ocr.ocr_image('input.png', 10000.0)
    run.assert_not_called()


def test_empty_resources_and_different_roots_cannot_form_a_complete_bundle(resources, monkeypatch):
    tools, _ = resources
    monkeypatch.setattr(thai_ocr, '_is_windows', lambda: True)
    assert not thai_ocr.available()
    binary, tessdata, _ = bundle(tools / 'tesseract', True)
    _, alternate, _ = bundle(tools / 'mail-ocr', True)
    (tools / 'mail-ocr' / 'tesseract.exe').unlink()
    for name in ('tha.traineddata', 'eng.traineddata'):
        (tessdata / name).unlink()
    assert binary.is_file() and (alternate / 'tha.traineddata').is_file()
    assert not thai_ocr.available()


def test_windows_does_not_treat_historical_linux_binary_as_a_windows_tool(resources, monkeypatch):
    _, legacy = resources
    bundle(legacy, False)
    monkeypatch.setattr(thai_ocr, '_is_windows', lambda: True)
    assert not thai_ocr.available()


@pytest.mark.parametrize('remaining', [0.0, -1.0, 0.025])
def test_deadline_is_respected_without_rounding_past_remaining_budget(resources, monkeypatch, remaining):
    tools, _ = resources
    binary, _, _ = bundle(tools / 'tesseract', True)
    monkeypatch.setattr(thai_ocr, '_is_windows', lambda: True)
    monkeypatch.setattr(thai_ocr.time, 'monotonic', lambda: 10.0)
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=b''))
    monkeypatch.setattr(thai_ocr.subprocess, 'run', run)
    if remaining <= 0:
        with pytest.raises(thai_ocr.subprocess.TimeoutExpired) as raised:
            thai_ocr.ocr_image('input.png', 10.0 + remaining)
        assert raised.value.cmd == str(binary)
        run.assert_not_called()
    else:
        assert thai_ocr.ocr_image('input.png', 10.0 + remaining) == ''
        assert run.call_args.kwargs['timeout'] == pytest.approx(remaining)


def test_subprocess_timeout_and_nonzero_exit_remain_explicit_failures(resources, monkeypatch):
    tools, _ = resources
    bundle(tools / 'mail-ocr', True)
    monkeypatch.setattr(thai_ocr, '_is_windows', lambda: True)
    monkeypatch.setattr(thai_ocr.time, 'monotonic', lambda: 0.0)
    run = Mock(side_effect=thai_ocr.subprocess.TimeoutExpired('tesseract', 1))
    monkeypatch.setattr(thai_ocr.subprocess, 'run', run)
    with pytest.raises(thai_ocr.subprocess.TimeoutExpired):
        thai_ocr.ocr_image('input.png', 1.0)
    run.side_effect = None
    run.return_value = SimpleNamespace(returncode=7, stdout=b'')
    with pytest.raises(RuntimeError, match='7'):
        thai_ocr.ocr_image('input.png', 1.0)
