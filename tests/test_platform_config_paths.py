"""Path configuration is independent of cwd and never falls back from an explicit env.

Each child imports a copy of config with synthetic env files only. No local
deployment configuration, database or models are read or initialized.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


SOURCE_ROOT = Path(__file__).resolve().parents[1]
FIELDS = (
    "BASE_DIR", "DATA_DIR", "OUTPUT_DIR", "MODELS_DIR", "TOOLS_DIR",
    "DOCLING_ARTIFACTS_DIR", "RAPIDOCR_MODEL_DIR", "EMBED_MODEL_PATH",
    "VLLM_MODEL_PATH", "TRANSLATION_MODEL_PATH", "TRANSLATION_CT2_MODEL_PATH",
    "CONVERSATION_TOKENIZER_PATH", "PARSE_TEMP_DIR", "LARGE_QA_INDEX_DIR",
)
PROBE = """
import json, os
from config import Config
fields = %r
result = {name: str(getattr(Config, name)) for name in fields}
result.update(HF_HOME=os.environ['HF_HOME'],
              DOCLING_CACHE_DIR=os.environ['DOCLING_CACHE_DIR'],
              SJFX_EMBED_MODEL_PATH=os.environ['SJFX_EMBED_MODEL_PATH'],
              SCAN_ALLOWED_ROOTS=[str(p) for p in Config.SCAN_ALLOWED_ROOTS])
print(json.dumps(result, ensure_ascii=False))
""" % (FIELDS,)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "应用 目录"
    root.mkdir()
    shutil.copyfile(SOURCE_ROOT / "config.py", root / "config.py")
    services = root / "services"
    services.mkdir()
    for module in ("model_settings.py", "model_contract.py", "model_endpoint_policy.py", "model_output.py", "embedding_backend.py"):
        shutil.copyfile(SOURCE_ROOT / "services" / module, services / module)
    (root / "empty.env").write_text("", encoding="utf-8")
    return root


def run_probe(project, cwd, *, code=PROBE, unset=(), **overrides):
    # Preserve only operating-system essentials. Do not inherit deployment
    # secrets, feature switches or env locations from the developer machine.
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP",
                              "USERPROFILE", "HOME", "HOMEDRIVE", "HOMEPATH"}}
    env.update(PYTHONPATH=str(project), PYTHONIOENCODING="utf-8",
               SJFX_ENV_FILE=str(project / "empty.env"))
    env.update({key: str(value) for key, value in overrides.items()})
    for key in unset:
        env.pop(key, None)
    return subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=30, check=False)


def values(result):
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_default_model_tree_is_preserved_without_import_writes(project, tmp_path):
    data = values(run_probe(project, tmp_path))
    models = project / "models"
    assert data["DATA_DIR"] == str(project / "data")
    assert data["OUTPUT_DIR"] == str(project / "outputs")
    assert data["TOOLS_DIR"] == str(project / ".tools")
    assert data["DOCLING_ARTIFACTS_DIR"] == data["DOCLING_CACHE_DIR"] == str(models / "docling")
    assert data["HF_HOME"] == str(models / "huggingface")
    assert data["EMBED_MODEL_PATH"] == str(models / "multilingual-e5-small")
    assert not (project / "data").exists()
    assert not (project / "outputs").exists()
    assert not models.exists()


def test_external_unicode_model_output_tools_directories(project, tmp_path):
    models = tmp_path / "离线 模型"
    output = tmp_path / "导出 文件"
    tools = tmp_path / "解析 工具"
    state = tmp_path / "运行 数据"
    data = values(run_probe(project, tmp_path, SJFX_MODELS_DIR=models,
                            SJFX_OUTPUT_DIR=output, SJFX_TOOLS_DIR=tools,
                            SJFX_STATE_DIR=state))
    for key, expected in {
        "MODELS_DIR": models, "OUTPUT_DIR": output, "TOOLS_DIR": tools,
        "DATA_DIR": state, "DOCLING_ARTIFACTS_DIR": models / "docling",
        "RAPIDOCR_MODEL_DIR": models / "rapidocr", "HF_HOME": models / "huggingface",
        "EMBED_MODEL_PATH": models / "multilingual-e5-small",
        "VLLM_MODEL_PATH": models / "Qwen3.6-27B",
        "TRANSLATION_MODEL_PATH": models / "nllb-200-distilled-600m",
        "TRANSLATION_CT2_MODEL_PATH": models / "nllb-200-distilled-600m-ct2-int8",
        "PARSE_TEMP_DIR": state / "parse_temp", "LARGE_QA_INDEX_DIR": state / "large_qa",
    }.items():
        assert data[key] == str(expected), key
    # The deployed model tree stays usable, but a generation tokenizer must be
    # selected explicitly rather than inheriting a previous vLLM model's path.
    assert data["CONVERSATION_TOKENIZER_PATH"] == ""
    assert data["SJFX_EMBED_MODEL_PATH"] == data["EMBED_MODEL_PATH"]
    assert not any(path.exists() for path in (models, output, tools, state))


def test_relative_env_file_and_paths_are_anchored_to_application(project, tmp_path):
    settings = project / "配置 文件.env"
    settings.write_text(
        'SJFX_MODELS_DIR="离线 模型"\nSJFX_OUTPUT_DIR=结果 目录\n'
        'SJFX_TOOLS_DIR=工具 目录\nSJFX_STATE_DIR=数据 目录\n'
        'SJFX_PARSE_TEMP_DIR=临时 目录\nLARGE_QA_INDEX_DIR=问答 索引\n'
        'SCAN_ALLOWED_ROOTS=输入 一' + os.pathsep + '输入 二\n',
        encoding="utf-8-sig",
    )
    other_cwd = tmp_path / "另一个 工作目录"
    other_cwd.mkdir()
    a = values(run_probe(project, project, SJFX_ENV_FILE=settings.name))
    b = values(run_probe(project, other_cwd, SJFX_ENV_FILE=settings.name))
    assert a == b
    for key, tail in {
        "MODELS_DIR": "离线 模型", "OUTPUT_DIR": "结果 目录", "TOOLS_DIR": "工具 目录",
        "DATA_DIR": "数据 目录", "PARSE_TEMP_DIR": "临时 目录", "LARGE_QA_INDEX_DIR": "问答 索引",
    }.items():
        assert a[key] == str(project / tail)
    assert a["SCAN_ALLOWED_ROOTS"] == [str(project / "输入 一"), str(project / "输入 二")]


@pytest.mark.parametrize('key', ['LLM_TOKENIZER_PATH', 'CONVERSATION_TOKENIZER_PATH'])
def test_explicit_selected_tokenizer_is_anchored_to_application(project, tmp_path, key):
    data = values(run_probe(project, tmp_path, LLM_BACKEND='ollama',
                            **{key:'所选模型/分词器'}))
    assert data['CONVERSATION_TOKENIZER_PATH'] == str(project / '所选模型' / '分词器')


def test_external_env_relative_values_and_process_overrides(project, tmp_path):
    settings = tmp_path / "external.env"
    settings.write_text(
        'SJFX_MODELS_DIR=models from env\nHF_HOME=cache hf\n'
        'DOCLING_CACHE_DIR=cache docling\nRAPIDOCR_MODEL_DIR=ocr custom\n'
        'VLLM_MODEL_PATH=llm custom\nTRANSLATION_MODEL_PATH=translation custom\n'
        'TRANSLATION_CT2_MODEL_PATH=ct2 custom\nSJFX_EMBED_MODEL_PATH=embed custom\n',
        encoding="utf-8",
    )
    data = values(run_probe(project, tmp_path, SJFX_ENV_FILE=settings,
                            SJFX_MODELS_DIR="models from process"))
    assert data["MODELS_DIR"] == str(project / "models from process")
    for key, tail in {
        "HF_HOME": "cache hf", "DOCLING_ARTIFACTS_DIR": "cache docling",
        "RAPIDOCR_MODEL_DIR": "ocr custom", "VLLM_MODEL_PATH": "llm custom",
        "TRANSLATION_MODEL_PATH": "translation custom",
        "TRANSLATION_CT2_MODEL_PATH": "ct2 custom", "EMBED_MODEL_PATH": "embed custom",
    }.items():
        assert data[key] == str(project / tail)


def test_missing_explicit_env_never_falls_back(project, tmp_path):
    (project / ".env").write_text("SJFX_OUTPUT_DIR=fallback should not load\n", encoding="utf-8")
    result = run_probe(project, tmp_path, SJFX_ENV_FILE="missing.env")
    assert result.returncode != 0
    assert "SJFX_ENV_FILE" in result.stderr
    assert "fallback should not load" not in result.stdout


def test_explicit_state_env_precedes_project_env(project, tmp_path):
    state = tmp_path / "private state"
    state.mkdir()
    (state / ".env").write_text("SJFX_OUTPUT_DIR=state results\n", encoding="utf-8")
    (project / ".env").write_text("SJFX_OUTPUT_DIR=project results\n", encoding="utf-8")
    data = values(run_probe(project, tmp_path, unset=("SJFX_ENV_FILE",), SJFX_STATE_DIR=state))
    assert data["OUTPUT_DIR"] == str(project / "state results")


def test_directory_aliases_and_specific_model_priority(project, tmp_path):
    data = values(run_probe(project, tmp_path, MODELS_DIR="alias models",
                            OUTPUT_DIR="alias output", TOOLS_DIR="alias tools",
                            DOCLING_CACHE_DIR="old docling", SJFX_DOCLING_ARTIFACTS_DIR="new docling",
                            VLLM_EMBED_MODEL_PATH="old embedding", SJFX_EMBED_MODEL_PATH="new embedding"))
    assert data["MODELS_DIR"] == str(project / "alias models")
    assert data["OUTPUT_DIR"] == str(project / "alias output")
    assert data["TOOLS_DIR"] == str(project / "alias tools")
    assert data["DOCLING_CACHE_DIR"] == data["DOCLING_ARTIFACTS_DIR"] == str(project / "new docling")
    assert data["EMBED_MODEL_PATH"] == str(project / "new embedding")


@pytest.mark.skipif(os.name != "nt", reason="Native Windows drive and UNC semantics")
def test_windows_absolute_drives_and_unc_are_not_prefixed(project, tmp_path):
    # Exercise UNC interpretation without contacting a real/unavailable share.
    code = """
from pathlib import Path
original_resolve = Path.resolve
def safe_resolve(self, *args, **kwargs):
    return self if self.drive.startswith('\\\\\\\\') else original_resolve(self, *args, **kwargs)
Path.resolve = safe_resolve
""" + PROBE
    data = values(run_probe(project, tmp_path, code=code, SJFX_OUTPUT_DIR=r'D:\分析 结果',
                            SJFX_MODELS_DIR=r'E:\离线 模型',
                            SJFX_TOOLS_DIR=r'\\server\共享 目录\tools',
                            SCAN_ALLOWED_ROOTS=r'C:\输入 文件;D:\归档 文件'))
    assert data["OUTPUT_DIR"] == str(Path(r'D:\分析 结果').resolve())
    assert data["MODELS_DIR"] == str(Path(r'E:\离线 模型').resolve())
    assert data["TOOLS_DIR"] == str(Path(r'\\server\共享 目录\tools'))
    assert data["SCAN_ALLOWED_ROOTS"] == [str(Path(r'C:\输入 文件').resolve()), str(Path(r'D:\归档 文件').resolve())]


def test_drive_relative_path_fails_instead_of_using_process_drive_cwd(project, tmp_path):
    result = run_probe(project, tmp_path, SJFX_OUTPUT_DIR="C:ambiguous")
    assert result.returncode != 0
    assert "Windows" in result.stderr


SERVER_PROBE = """
import json, runpy, sys, types
from pathlib import Path
calls = []
class LoadedModel:
    def eval(self): pass
class LocalLoader:
    @staticmethod
    def from_pretrained(path, **kwargs):
        calls.append([path, kwargs])
        return LoadedModel()
class FakeApp:
    def __init__(self, **kwargs): pass
    def get(self, route): return lambda fn: fn
    def post(self, route): return lambda fn: fn
sys.modules['torch'] = types.SimpleNamespace(Tensor=object, set_num_threads=lambda n: None,
                                             set_num_interop_threads=lambda n: None)
sys.modules['transformers'] = types.SimpleNamespace(AutoModel=LocalLoader, AutoTokenizer=LocalLoader)
sys.modules['uvicorn'] = types.SimpleNamespace()
sys.modules['fastapi'] = types.SimpleNamespace(FastAPI=FakeApp, HTTPException=Exception)
sys.modules['pydantic'] = types.SimpleNamespace(BaseModel=object)
server = runpy.run_path(str(Path(%r) / 'services' / 'mail' / 'embedding_server.py'))
print(json.dumps({'path': server['MODEL_PATH'], 'port': server['SERVER_PORT'], 'calls': calls}))
"""


@pytest.mark.parametrize("port_override, expected", [({}, 18083), ({"PORT": "18183"}, 18183),
                                                     ({"PORT": "18183", "SJFX_EMBED_PORT": "18184"}, 18184)])
def test_embedding_server_uses_shared_local_model_from_any_cwd(project, tmp_path, port_override, expected):
    server_dir = project / "services" / "mail"
    server_dir.mkdir(parents=True)
    shutil.copyfile(SOURCE_ROOT / "services" / "mail" / "embedding_server.py", server_dir / "embedding_server.py")
    settings = project / "server.env"
    settings.write_text("SJFX_MODELS_DIR=离线 模型\nPORT=8000\n", encoding="utf-8")
    data = values(run_probe(project, tmp_path, code=SERVER_PROBE % str(project),
                            SJFX_ENV_FILE=settings, **port_override))
    expected_model = str(project / "离线 模型" / "multilingual-e5-small")
    assert data["path"] == expected_model
    assert data["port"] == expected
    # Both loaders stay offline and reject remote code; model weights must use
    # the existing safetensors-only loading contract.
    assert data["calls"] == [
        [expected_model, {"local_files_only": True, "trust_remote_code": False}],
        [expected_model, {"local_files_only": True, "trust_remote_code": False, "use_safetensors": True}],
    ]
