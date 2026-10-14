"""Runtime directories must never mutate immutable model/tool inputs."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_device_signing_dependency_blocks_partial_startup(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import importlib.util
    import pytest
    import sjfxctl
    find_spec = importlib.util.find_spec
    monkeypatch.setattr(sjfxctl.importlib.util, 'find_spec',
                        lambda name: None if name == 'cryptography' else find_spec(name))
    configuration = SimpleNamespace(DATA_DIR=tmp_path / 'never-created')
    with pytest.raises(RuntimeError, match='Missing application dependencies: cryptography'):
        sjfxctl.start(SimpleNamespace(), configuration)
    assert not configuration.DATA_DIR.exists()


def clean_env(tmp_path):
    allowed = {'SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP', 'USERPROFILE', 'HOME', 'HOMEDRIVE', 'HOMEPATH'}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    envfile = tmp_path / 'settings.env'
    envfile.write_text('', encoding='utf-8')
    env.update(SJFX_ENV_FILE=str(envfile), PYTHONPATH=str(ROOT), PYTHONUTF8='1',
               PYTHONDONTWRITEBYTECODE='1')
    return env


def test_runtime_creates_only_user_directories(tmp_path):
    env = clean_env(tmp_path)
    state = tmp_path / '状态 数据'
    output = tmp_path / '输出 目录'
    resources = tmp_path / '缺失只读模型输入'
    env.update(SJFX_STATE_DIR=str(state), SJFX_OUTPUT_DIR=str(output),
               SJFX_MODELS_DIR=str(resources))
    result = subprocess.run([sys.executable, '-B', '-c',
                             'from config import ensure_runtime_directories; ensure_runtime_directories()'],
                            cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert state.is_dir() and output.is_dir() and (state / 'parse_temp').is_dir()
    assert not resources.exists()


def test_setup_uses_readonly_input_roots_and_private_cache(tmp_path):
    env = clean_env(tmp_path)
    envfile = tmp_path / '私有 配置' / 'instance.env'
    models = tmp_path / '资源 输入' / 'models-not-mounted'
    tools = tmp_path / '资源 输入' / 'tools-not-mounted'
    temp = tmp_path / '独立 临时'
    result = subprocess.run([sys.executable, '-B', str(ROOT / 'sjfxctl.py'), '--env-file', str(envfile),
                             'setup', '--models-dir', str(models), '--tools-dir', str(tools),
                             '--parse-temp-dir', str(temp), '--backend', 'openai_compatible'],
                            cwd=tmp_path, env=env, capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr + result.stdout
    content = envfile.read_text(encoding='utf-8')
    assert 'SJFX_TOOLS_DIR="' + str(tools) + '"' in content
    assert 'SJFX_PARSE_TEMP_DIR="' + str(temp) + '"' in content
    assert 'HF_HOME="' + str(envfile.parent / 'state' / 'cache' / 'huggingface') + '"' in content
    assert temp.is_dir()
    assert not models.exists() and not tools.exists() and not models.parent.exists()
    assert 'SJFX_API_ACCESS_TOKEN' not in result.stdout
