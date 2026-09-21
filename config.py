import os
import multiprocessing
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


BASE_DIR = Path(__file__).resolve().parent

# The product contract uses one binary 10 GiB ceiling for every individual
# content object (source file, archive container/member, expanded archive and
# export). Component-specific environment variables may lower their budgets,
# but can never accidentally raise them above this audited hard limit.
TEN_GIB_BYTES = 10 * 1024 * 1024 * 1024


def _positive_int(raw, default):
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return max(1, int(default))


def content_byte_limit(component_env=None):
    """Return a component budget bounded by the central 10 GiB contract.

    ``MAX_CONTENT_BYTES`` lowers every content budget at once. A legacy
    component setting can lower its own budget further, which keeps existing
    deployments compatible without allowing conflicting values to exceed the
    product ceiling.
    """
    central = min(
        TEN_GIB_BYTES,
        _positive_int(os.getenv("MAX_CONTENT_BYTES", TEN_GIB_BYTES), TEN_GIB_BYTES),
    )
    if not component_env:
        return central
    return min(
        central,
        _positive_int(os.getenv(component_env, central), central),
    )


def _mount_filesystem(path):
    """Return the Linux filesystem type for *path*, if it can be determined."""
    mountinfo = Path("/proc/self/mountinfo")
    if not mountinfo.is_file():
        return None
    candidate = Path(path).resolve()
    best = (0, None)
    try:
        lines = mountinfo.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        left, separator, right = line.partition(" - ")
        if not separator:
            continue
        fields = left.split()
        right_fields = right.split()
        if len(fields) < 5 or not right_fields:
            continue
        mount_point = Path(fields[4].replace("\\040", " ")).resolve()
        try:
            candidate.relative_to(mount_point)
        except ValueError:
            continue
        if len(mount_point.parts) > best[0]:
            best = (len(mount_point.parts), right_fields[0].lower())
    return best[1]


def _default_state_dir():
    """Keep SQLite/WAL off NFS while leaving source packages on the NAS."""
    project_state = BASE_DIR / "data"
    filesystem = _mount_filesystem(project_state)
    if filesystem and (filesystem.startswith("nfs") or filesystem in {"cifs", "smbfs"}):
        durable_home_state = Path.home() / ".local" / "state" / "sjfx-data-analysis-agent"
        home_filesystem = _mount_filesystem(durable_home_state)
        if not home_filesystem or not (
            home_filesystem.startswith("nfs") or home_filesystem in {"cifs", "smbfs"}
        ):
            return durable_home_state
        user = re.sub(r"[^A-Za-z0-9_.-]+", "-", os.getenv("USER", "sjfx")) or "sjfx"
        uid = str(os.getuid()) if hasattr(os, "getuid") else user
        return Path("/var/tmp") / "sjfx-data-analysis-agent-{}".format(uid)
    return project_state


DEFAULT_STATE_DIR = _default_state_dir()


def load_local_env():
    explicit = str(os.getenv("SJFX_ENV_FILE") or "").strip()
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.extend((DEFAULT_STATE_DIR / ".env", BASE_DIR / ".env"))
    env_file = next((item for item in candidates if item.is_file()), None)
    if env_file is None:
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_local_env()

# Defaults must be applied after .env loading, otherwise setdefault() would
# make the hard-coded defaults win over project-local configuration.
os.environ.setdefault("HF_HOME", str(BASE_DIR / "models" / "huggingface"))
os.environ.setdefault("DOCLING_CACHE_DIR", str(BASE_DIR / "models" / "docling"))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
# The laboratory server has no outbound Hugging Face route.  Never let a
# document parse attempt a model download into a shared system cache or wait
# for a network timeout; Docling may use only artifacts stored under this app.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


def _split_path_list(value):
    """Split an allow-list using the host separator without breaking drives."""
    raw = str(value or "")
    separator = os.pathsep
    # ``os.pathsep`` is ``;`` on Windows, so ``C:\data;D:\archive`` remains
    # intact.  POSIX deployments use ``:`` as documented by the sample env.
    return [item.strip() for item in raw.split(separator) if item.strip()]


def _token_expiry(raw):
    """Normalize an optional token expiry to a UTC epoch timestamp."""
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            # A malformed expiry is represented by a sentinel in the past;
            # the API will fail closed instead of silently issuing a permanent
            # token.  The raw value is never logged or returned to clients.
            return 0.0


def _local_model_url(raw):
    """Reject accidental remote model endpoints in the offline product."""
    value = str(raw or "").strip().rstrip("/")
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(
            "物理断网部署只允许本机模型地址（127.0.0.1/localhost/::1），拒绝远程模型地址"
        )
    return value


class Config:
    BASE_DIR = BASE_DIR
    DATA_DIR = Path(os.getenv("SJFX_STATE_DIR", str(DEFAULT_STATE_DIR))).expanduser().resolve()
    OUTPUT_DIR = BASE_DIR / "outputs"
    LOG_DIR = DATA_DIR / "logs"
    MODELS_DIR = BASE_DIR / "models"
    DOCLING_ARTIFACTS_DIR = MODELS_DIR / "docling"
    RAPIDOCR_MODEL_DIR = MODELS_DIR / "rapidocr"
    DB_PATH = DATA_DIR / "agent.db"
    DOCUMENT_CACHE_DIR = DATA_DIR / "document_payloads"
    # Parsing can temporarily expand an archive close to the product ceiling.
    # Keep that traffic off the small system /tmp filesystem by default. The
    # directory may be moved to a dedicated local volume in production.
    PARSE_TEMP_DIR = Path(
        os.getenv("SJFX_PARSE_TEMP_DIR", str(DATA_DIR / "parse_temp"))
    ).expanduser().resolve()
    PARSE_TEMP_DISK_RESERVE_BYTES = max(
        0, int(os.getenv("PARSE_TEMP_DISK_RESERVE_BYTES", str(1024 * 1024 * 1024)))
    )
    PARSE_TEMP_STALE_SECONDS = max(
        60, int(os.getenv("PARSE_TEMP_STALE_SECONDS", "21600"))
    )
    # Select the local serving backend. vLLM is the default; Ollama remains
    # available as an explicit compatibility option during migration.
    LLM_BACKEND = os.getenv("LLM_BACKEND", "vllm").strip().lower()
    if LLM_BACKEND not in {"vllm", "ollama"}:
        raise RuntimeError("LLM_BACKEND 只能是 vllm 或 ollama")
    ENABLE_VLLM = os.getenv("ENABLE_VLLM", "1").strip().lower() in {"1", "true", "yes", "on"}
    VLLM_BASE_URL = _local_model_url(os.getenv("VLLM_BASE_URL", "http://127.0.0.1:8001/v1"))
    VLLM_MODEL = os.getenv("VLLM_MODEL", "qwen3.6:27b").strip()
    VLLM_MODEL_PATH = os.getenv("VLLM_MODEL_PATH", str(BASE_DIR / "models" / "Qwen3.6-27B")).strip()
    VLLM_API_KEY = os.getenv("VLLM_API_KEY", "").strip()
    VLLM_REQUEST_TIMEOUT = max(60, int(os.getenv("VLLM_REQUEST_TIMEOUT", "600")))
    # Keep single-GPU vLLM replies within a responsive output budget while
    # preserving the configured 32K input context window.
    VLLM_INTERACTIVE_MAX_TOKENS = max(128, min(800, int(os.getenv("VLLM_INTERACTIVE_MAX_TOKENS", "480"))))
    VLLM_INTERACTIVE_TIMEOUT_SECONDS = max(60, min(300, int(os.getenv("VLLM_INTERACTIVE_TIMEOUT_SECONDS", "120"))))
    # The benchmark fixture uses 768 generated tokens for every SJFX stage.
    # Keep that as the capture default, but do not impose an artificial small
    # upper ceiling: normal product workflows may explicitly require longer
    # structured output.
    VLLM_STRUCTURED_MAX_TOKENS = max(
        256, min(32768, int(os.getenv("VLLM_STRUCTURED_MAX_TOKENS", "768")))
    )
    VLLM_STRUCTURED_TIMEOUT_SECONDS = max(
        90, min(600, int(os.getenv("VLLM_STRUCTURED_TIMEOUT_SECONDS", "180")))
    )

    # Embeddings run in a separate CPU vLLM process so the GPU generation
    # server remains isolated. Retrieval must degrade quickly if it is down.
    ENABLE_VLLM_EMBEDDINGS = os.getenv("ENABLE_VLLM_EMBEDDINGS", "1").strip().lower() in {"1", "true", "yes", "on"}
    VLLM_EMBED_BASE_URL = _local_model_url(os.getenv("VLLM_EMBED_BASE_URL", "http://127.0.0.1:8002/v1"))
    VLLM_EMBED_MODEL = os.getenv("VLLM_EMBED_MODEL", "multilingual-e5-small").strip()
    VLLM_EMBED_TIMEOUT_SECONDS = max(2, min(60, int(os.getenv("VLLM_EMBED_TIMEOUT_SECONDS", "12"))))
    VLLM_EMBED_MAX_BATCH_SIZE = max(1, min(64, int(os.getenv("VLLM_EMBED_MAX_BATCH_SIZE", "8"))))
    # 大包嵌入车道的**并发请求数**（默认 2 = 嵌入服务的准入上限，不超出它的承受能力）。
    # 实测依据：服务端配了 OMP 4 线程 × 准入 2（最多 8 路），但客户端原来是串行发块，
    # 有效并行度只有 2.38（批 8）/3.79（批 16）；并发发块才能用上服务端已有的并行能力。
    LP_EMBED_CONCURRENCY = max(1, min(4, _positive_int(os.getenv("SJFX_LP_EMBED_CONCURRENCY"), 2)))
    # 大包嵌入车道的单请求超时（默认 60s）。共享客户端是 8s，而**服务空闲后第一个请求
    # 实测要 21 秒**（冷启动），8s 会让服务重启后的第一批嵌入必然失败。
    LP_EMBED_TIMEOUT_SECONDS = max(5, min(600, _positive_int(os.getenv("SJFX_LP_EMBED_TIMEOUT"), 60)))
    # 嵌入的**备端点**（通常指向 CPU 嵌入服务）。GPU sidecar 有显存守卫，可能拒绝启动
    # 或返回 503；没有备端点时那等于整条嵌入车道停摆（文件拿不到向量 → 进"未能识别
    # 内容"桶），那是质量事故。配了备端点就只是慢，不会停。默认空 = 不启用回退。
    LP_EMBED_FALLBACK_URL = os.getenv("SJFX_LP_EMBED_FALLBACK_URL", "").strip()
    VLLM_EMBED_MAX_CHARS = max(128, min(4000, int(os.getenv("VLLM_EMBED_MAX_CHARS", "1200"))))
    # The client-side mutex must fail fast so an interactive lane can fall back
    # to lexical retrieval immediately, while the socket budget above is what
    # absorbs a busy shared CPU embedding server.  Keeping the two budgets
    # separate is deliberate: one value previously governed both, so raising it
    # to survive load also turned the fast fallback into a long block.
    VLLM_EMBED_SEMAPHORE_TIMEOUT_SECONDS = max(
        1, min(30, int(os.getenv("VLLM_EMBED_SEMAPHORE_TIMEOUT_SECONDS", "2")))
    )
    VLLM_EMBED_RETRIES = max(0, min(5, int(os.getenv("VLLM_EMBED_RETRIES", "2"))))

    # The server also provides a local Ollama instance for rollback. Keep its
    # settings intact so LLM_BACKEND=ollama remains a safe fallback.
    ENABLE_SHARED_OLLAMA = os.getenv("ENABLE_SHARED_OLLAMA", "0").strip().lower() in {"1", "true", "yes"}
    OLLAMA_BASE_URL = _local_model_url(os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1"))
    OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen-agent:latest")
    OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "qwen-embed:latest")
    # Keep the embedding model on CPU so the vLLM generation model retains the GPU.
    OLLAMA_EMBED_NUM_GPU = max(0, int(os.getenv("OLLAMA_EMBED_NUM_GPU", "0")))
    LLM_MAX_CONCURRENCY = max(1, int(os.getenv("LLM_MAX_CONCURRENCY", "1")))
    # Directory names are the product's primary output.  On the ordinary
    # "import -> select -> confirm" path the naming model used to be disabled,
    # which left every name to a deterministic fallback that could emit raw
    # English placeholders.  The naming is bounded (a few short calls), so it is
    # on by default; set SJFX_SEMANTIC_NAMING_ON_USER_IMPORT=0 to restore the
    # previous faster-but-unreadable behaviour without a code change.
    SEMANTIC_NAMING_ON_USER_IMPORT = os.getenv(
        "SJFX_SEMANTIC_NAMING_ON_USER_IMPORT", "1"
    ).strip().lower() in {"1", "true", "yes", "on"}
    # The available Ollama service is shared by the laboratory. Do not send
    # long document prompts to it unless a dedicated scheduling decision was made.
    ENABLE_SHARED_OLLAMA_EMBEDDINGS = os.getenv("ENABLE_SHARED_OLLAMA_EMBEDDINGS", "0").strip().lower() in {"1", "true", "yes"}
    SHARED_OLLAMA_REQUEST_TIMEOUT = max(60, int(os.getenv("SHARED_OLLAMA_REQUEST_TIMEOUT", "600")))
    SHARED_OLLAMA_MAX_CHARS = max(4000, int(os.getenv("SHARED_OLLAMA_MAX_CHARS", "48000")))
    # Keep the prompt budget aligned with the serving engine.  This project's
    # vLLM service is intentionally configured for a 32K context window on the
    # single RTX 3090; using the old 64K Ollama default can overfill long
    # document prompts before they reach the model.
    _default_context_tokens = "32768" if LLM_BACKEND == "vllm" else "65536"
    LLM_CONTEXT_TOKENS = max(
        8192, int(os.getenv("LLM_CONTEXT_TOKENS", _default_context_tokens))
    )
    LLM_MODEL = VLLM_MODEL if LLM_BACKEND == "vllm" else OLLAMA_MODEL
    LLM_BASE_URL = VLLM_BASE_URL if LLM_BACKEND == "vllm" else OLLAMA_BASE_URL
    LLM_ENABLED = ENABLE_VLLM if LLM_BACKEND == "vllm" else ENABLE_SHARED_OLLAMA
    ENABLE_TRANSLATION = os.getenv("ENABLE_TRANSLATION", "1").strip().lower() in {"1", "true", "yes", "on"}
    # Translation is local-only by default. Set TRANSLATION_PROVIDER=ollama
    # explicitly to retain the legacy Ollama adapter.
    TRANSLATION_PROVIDER = os.getenv("TRANSLATION_PROVIDER", "offline_nllb").strip().lower()
    # Explicit document/file translation backend. Mail translation is assembled separately.
    TRANSLATION_FILE_BACKEND = os.getenv("TRANSLATION_FILE_BACKEND", "vllm").strip().lower()
    if TRANSLATION_FILE_BACKEND not in {"offline_nllb", "vllm"}:
        TRANSLATION_FILE_BACKEND = "vllm"
    TRANSLATION_MODEL_PATH = os.getenv(
        "TRANSLATION_MODEL_PATH",
        str(BASE_DIR / "models" / "nllb-200-distilled-600m"),
    ).strip()
    TRANSLATION_CT2_MODEL_PATH = os.getenv(
        "TRANSLATION_CT2_MODEL_PATH",
        str(BASE_DIR / "models" / "nllb-200-distilled-600m-ct2-int8"),
    ).strip()
    TRANSLATION_PREFER_CT2 = os.getenv("TRANSLATION_PREFER_CT2", "1").strip().lower() in {
        "1", "true", "yes", "on",
    }
    TRANSLATION_DEVICE = os.getenv("TRANSLATION_DEVICE", "cpu").strip().lower() or "cpu"
    # Batch short units in one CPU model call for better throughput. Keep one
    # translation worker and a bounded thread pool so this cannot compete with
    # other users by spawning extra model processes.
    TRANSLATION_BATCH_SIZE = max(1, min(32, int(os.getenv("TRANSLATION_BATCH_SIZE", "8"))))
    TRANSLATION_CPU_THREADS = max(1, min(64, int(os.getenv("TRANSLATION_CPU_THREADS", "4"))))
    # NLLB-200 accepts up to roughly 1k source tokens. Using the full safe
    # window reduces needless CPU calls while the provider still performs
    # token-aware splitting for anything larger.
    TRANSLATION_MAX_INPUT_TOKENS = max(128, min(4096, int(os.getenv("TRANSLATION_MAX_INPUT_TOKENS", "1024"))))
    TRANSLATION_MAX_NEW_TOKENS = max(128, min(4096, int(os.getenv("TRANSLATION_MAX_NEW_TOKENS", "512"))))
    ENABLE_TRANSLATION_REVIEW = os.getenv("ENABLE_TRANSLATION_REVIEW", "1").strip().lower() in {
        "1", "true", "yes", "on",
    }
    TRANSLATION_OLLAMA_BASE_URL = _local_model_url(
        os.getenv("TRANSLATION_OLLAMA_BASE_URL", OLLAMA_BASE_URL)
    )
    TRANSLATION_OLLAMA_MODEL = os.getenv("TRANSLATION_OLLAMA_MODEL", OLLAMA_MODEL).strip()
    TRANSLATION_MAX_UNIT_CHARS = max(
        128, min(12000, int(os.getenv("TRANSLATION_MAX_UNIT_CHARS", "4800")))
    )
    TRANSLATION_MAX_ATTEMPTS = max(1, min(6, int(os.getenv("TRANSLATION_MAX_ATTEMPTS", "2"))))
    TRANSLATION_TIMEOUT_SECONDS = max(10, min(1800, int(os.getenv("TRANSLATION_TIMEOUT_SECONDS", "180"))))
    TRANSLATION_COALESCE_PARAGRAPHS = os.getenv(
        "TRANSLATION_COALESCE_PARAGRAPHS", "1"
    ).strip().lower() in {"1", "true", "yes", "on"}
    TRANSLATION_REVIEW_COMPLEX_UNITS = os.getenv(
        "TRANSLATION_REVIEW_COMPLEX_UNITS", "0"
    ).strip().lower() in {"1", "true", "yes", "on"}
    # The import pipeline starts translation as soon as a parsed document is
    # durable. It deliberately owns one local NLLB instance; parser parallelism
    # is reduced while it is active so shared-host CPU use stays predictable.
    TRANSLATION_PIPELINE_ENABLED = os.getenv(
        "TRANSLATION_PIPELINE_ENABLED", "1"
    ).strip().lower() in {"1", "true", "yes", "on"}
    TRANSLATION_PIPELINE_PARSE_MAX_CONCURRENCY = max(
        1, min(8, int(os.getenv("TRANSLATION_PIPELINE_PARSE_MAX_CONCURRENCY", "1")))
    )
    ENABLE_IMPORT_TRANSLATION = os.getenv(
        "ENABLE_IMPORT_TRANSLATION", "0"
    ).strip().lower() in {"1", "true", "yes", "on"}
    IMPORT_TRANSLATION_MAX_FILES = max(
        1, min(500, int(os.getenv("IMPORT_TRANSLATION_MAX_FILES", "30")))
    )
    IMPORT_TRANSLATION_LARGE_MAX_FILES = max(
        1, min(200, int(os.getenv("IMPORT_TRANSLATION_LARGE_MAX_FILES", "8")))
    )
    IMPORT_TRANSLATION_MAX_CHARS_PER_FILE = max(
        1000, min(100000, int(os.getenv("IMPORT_TRANSLATION_MAX_CHARS_PER_FILE", "12000")))
    )
    IMPORT_TRANSLATION_MAX_TOTAL_CHARS = max(
        10000, min(5000000, int(os.getenv("IMPORT_TRANSLATION_MAX_TOTAL_CHARS", "80000")))
    )
    IMPORT_TRANSLATION_MAX_UNITS_PER_FILE = max(
        1, min(100, int(os.getenv("IMPORT_TRANSLATION_MAX_UNITS_PER_FILE", "6")))
    )
    TRANSLATION_PACKAGE_BATCH_FILES = max(
        1, min(500, int(os.getenv("TRANSLATION_PACKAGE_BATCH_FILES", "20")))
    )
    TRANSLATION_FOREGROUND_MAX_UNITS = max(
        1, min(200, int(os.getenv("TRANSLATION_FOREGROUND_MAX_UNITS", "24")))
    )
    AUTO_TRANSLATE_PACKAGES = os.getenv("AUTO_TRANSLATE_PACKAGES", "1").strip().lower() in {
        "1", "true", "yes", "on",
    }
    HOST = os.getenv("HOST", "127.0.0.1")
    PORT = int(os.getenv("PORT", "8000"))
    # Additional explicit interfaces let VPN and existing SSH tunnels coexist.
    EXTRA_BIND_HOSTS = tuple(dict.fromkeys(
        host.strip() for host in os.getenv("SJFX_EXTRA_BIND_HOSTS", "").split(",")
        if host.strip() and host.strip() != os.getenv("HOST", "127.0.0.1")
    ))
    API_ACCESS_TOKEN = os.getenv("SJFX_API_ACCESS_TOKEN", "").strip()
    OWNER_ID = re.sub(
        r"[^A-Za-z0-9_.-]+", "-", os.getenv("SJFX_OWNER_ID", "primary").strip()
    ).strip("-")[:64] or "primary"
    API_TOKEN_EXPIRES_AT = _token_expiry(os.getenv("SJFX_API_TOKEN_EXPIRES_AT", ""))
    DOWNLOAD_TICKET_TTL_SECONDS = max(
        30, min(600, int(os.getenv("DOWNLOAD_TICKET_TTL_SECONDS", "120")))
    )
    _LOOPBACK_HOST = all(
        host in {"127.0.0.1", "localhost", "::1"}
        for host in (HOST,) + EXTRA_BIND_HOSTS
    )
    AUTH_REQUIRED = os.getenv(
        "AUTH_REQUIRED", "0" if _LOOPBACK_HOST else "1"
    ).strip().lower() in {"1", "true", "yes", "on"}
    if not _LOOPBACK_HOST and not AUTH_REQUIRED:
        raise RuntimeError(
            "非回环地址部署必须启用 AUTH_REQUIRED，拒绝启动未鉴权服务"
        )
    if AUTH_REQUIRED and not API_ACCESS_TOKEN:
        raise RuntimeError(
            "AUTH_REQUIRED=1 时必须配置 SJFX_API_ACCESS_TOKEN"
        )
    # Interactive API documentation is a development-only surface.  It is
    # disabled whenever authentication is enabled or the server is reachable
    # beyond loopback, so route metadata cannot bypass the access guard.
    ENABLE_API_DOCS = (
        os.getenv("ENABLE_API_DOCS", "0").strip().lower()
        in {"1", "true", "yes", "on"}
        and _LOOPBACK_HOST
        and not AUTH_REQUIRED
    )
    _allowed_roots_raw = os.getenv("SCAN_ALLOWED_ROOTS", "").strip()
    if not _allowed_roots_raw and not _LOOPBACK_HOST:
        raise RuntimeError("非回环地址部署必须显式设置 SCAN_ALLOWED_ROOTS，拒绝使用宽泛默认扫描范围")
    _allowed_roots = _allowed_roots_raw or str(BASE_DIR.parent)
    SCAN_ALLOWED_ROOTS = tuple(Path(item).expanduser().resolve() for item in _split_path_list(_allowed_roots))
    MAX_EXTRACT_CHARS = int(os.getenv("MAX_EXTRACT_CHARS", "60000"))
    MAX_FULL_DOCUMENT_CHARS = int(os.getenv("MAX_FULL_DOCUMENT_CHARS", "2000000"))
    MAX_CONTENT_BYTES = content_byte_limit()
    MAX_SINGLE_FILE_BYTES = content_byte_limit("MAX_SINGLE_FILE_BYTES")
    MAX_PARSE_SECONDS = max(1, int(os.getenv("MAX_PARSE_SECONDS", "300")))
    # Large archives can become visible on a NAS before their copy finishes.
    # Observe them briefly so an incomplete ZIP is reported as changing input.
    SOURCE_STABILITY_SECONDS = max(0.0, min(10.0, float(os.getenv("SOURCE_STABILITY_SECONDS", "2"))))
    MAX_WORKER_MEMORY_MB = max(256, int(os.getenv("MAX_WORKER_MEMORY_MB", "8192")))
    MAX_PARSE_PROCESS_MEMORY_MB = max(
        256,
        int(os.getenv("MAX_PARSE_PROCESS_MEMORY_MB", str(MAX_WORKER_MEMORY_MB))),
    )
    # Parsing is CPU-bound and each isolated parser process may load Docling
    # and OCR runtimes.  Keep the default deliberately conservative for a
    # shared 32 GB host; operators can raise this after measuring RSS/IO.  A
    # hard cap prevents an accidental CPU-count setting from starting dozens
    # of multi-gigabyte parser processes.
    _cpu_count = max(1, int(os.cpu_count() or multiprocessing.cpu_count() or 1))
    PARSE_MAX_CONCURRENCY = max(
        1,
        min(8, int(os.getenv("PARSE_MAX_CONCURRENCY", "4"))),
    )
    PARSE_MAX_CONCURRENCY = min(PARSE_MAX_CONCURRENCY, max(1, _cpu_count - 2))
    ENABLE_PARSE_PROCESS_ISOLATION = os.getenv("ENABLE_PARSE_PROCESS_ISOLATION", "1").strip().lower() in {
        "1", "true", "yes", "on",
    }
    DOCLING_DEVICE = os.getenv("DOCLING_DEVICE", "cpu").strip().lower()
    if DOCLING_DEVICE not in {"cpu", "cuda", "auto"}:
        DOCLING_DEVICE = "cpu"
    DOCLING_CPU_THREADS = max(1, min(64, int(os.getenv("DOCLING_CPU_THREADS", "4"))))
    MAX_DOCUMENT_CHUNKS = max(4, min(256, int(os.getenv("MAX_DOCUMENT_CHUNKS", "64"))))
    # Candidate previews are intentionally bounded: one short model call per
    # file and a batch deadline after which local parser output is sufficient
    # to unlock the provisional directory.
    CANDIDATE_PREVIEW_MAX_CHARS = max(2000, min(16000, int(os.getenv("CANDIDATE_PREVIEW_MAX_CHARS", "8000"))))
    CANDIDATE_PREVIEW_BATCH_SIZE = max(1, min(8, int(os.getenv("CANDIDATE_PREVIEW_BATCH_SIZE", "4"))))
    CANDIDATE_PREVIEW_BATCH_CHARS = max(2000, min(8000, int(os.getenv("CANDIDATE_PREVIEW_BATCH_CHARS", "3200"))))
    CANDIDATE_PREVIEW_OUTPUT_TOKENS = max(160, min(360, int(os.getenv("CANDIDATE_PREVIEW_OUTPUT_TOKENS", "240"))))
    CANDIDATE_PREVIEW_MAX_WAIT_SECONDS = max(60, min(3600, int(os.getenv("CANDIDATE_PREVIEW_MAX_WAIT_SECONDS", "900"))))
    CANDIDATE_PREVIEW_TIMEOUT_SECONDS = max(15, min(180, int(os.getenv("CANDIDATE_PREVIEW_TIMEOUT_SECONDS", "60"))))
    # Runtime-only coalescing for preliminary file summaries. The queue and
    # import state machine continue to create one task per file.
    PRELIMINARY_SUMMARY_BATCH_SIZE = max(1, min(4, int(os.getenv("PRELIMINARY_SUMMARY_BATCH_SIZE", "4"))))
    # 初步摘要同时发起的模型请求数。默认 1 = 一次一批、串行，与历史行为完全一致。
    # 单卡解码是「读权重」瓶颈，K 路并发能把每 token 的解码时间摊薄到约 1/K；
    # 必须同时放开 vLLM --max-num-seqs、SJFX_GENERATION_CONCURRENCY 和
    # LLM_MAX_CONCURRENCY，这里才会真正并行。
    PRELIMINARY_SUMMARY_CONCURRENCY = max(
        1, min(8, int(os.getenv("PRELIMINARY_SUMMARY_CONCURRENCY", "1")))
    )
    # Ordinary preliminary summaries only; candidate/deep defaults remain unchanged.
    PRELIMINARY_SUMMARY_OPTIMIZATION_ENABLED = os.getenv("PRELIMINARY_SUMMARY_OPTIMIZATION_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
    PRELIMINARY_SUMMARY_INPUT_MAX_CHARS = max(2000, min(6500, int(os.getenv("PRELIMINARY_SUMMARY_INPUT_MAX_CHARS", "6500"))))
    # ZIP64 and streaming writes support a complete 10 GiB handoff without
    # loading the source package into memory.
    MAX_EXPORT_BYTES = content_byte_limit("MAX_EXPORT_BYTES")
    EXPORT_DISK_RESERVE_BYTES = int(os.getenv("EXPORT_DISK_RESERVE_BYTES", str(1024 * 1024 * 1024)))
    # Default conservatively, but allow an operator to raise the inventory
    # boundary for very large evidence packages after sizing memory/disk.
    MAX_SCAN_FILES = max(1, min(1_000_000, int(os.getenv("MAX_SCAN_FILES", "500000"))))
    # Bound recursive inventory construction before Python stack or hostile media
    # can exhaust the local analysis box.  Symlinks are never followed.
    MAX_SCAN_DEPTH = max(1, min(256, int(os.getenv("MAX_SCAN_DEPTH", "32"))))
    MAX_SCAN_DIRECTORIES = max(1, min(1_000_000, int(os.getenv("MAX_SCAN_DIRECTORIES", "500000"))))
    MAX_SCAN_NODES = max(
        2,
        min(2_000_000, int(os.getenv("MAX_SCAN_NODES", str(MAX_SCAN_FILES + MAX_SCAN_DIRECTORIES + 1)))),
    )
    # Natural ordering requires a bounded per-directory buffer. The cap keeps
    # a single hostile directory from materialising millions of DirEntry
    # objects while preserving the current directory-first ordering below it.
    MAX_SCAN_ENTRIES_PER_DIRECTORY = max(
        1, min(250000, int(os.getenv("MAX_SCAN_ENTRIES_PER_DIRECTORY", "50000")))
    )
    SCAN_SLICE_ENTRIES = max(
        100, min(10000, int(os.getenv("SCAN_SLICE_ENTRIES", "1000")))
    )
    SCAN_SLICE_SECONDS = max(
        2, min(120, int(os.getenv("SCAN_SLICE_SECONDS", "20")))
    )
    SIDECAR_PAYLOAD_BYTES = int(os.getenv("SIDECAR_PAYLOAD_BYTES", str(256 * 1024)))
    # Isolated catalog generation budgets; unrelated import policies stay separate.
    LP_CATALOG_MODEL_NAMING = os.getenv("SJFX_LP_CATALOG_MODEL_NAMING", "1").lower() not in {"0", "false", "off"}
    # 这三个是**下限/兜底**，不再是固定预算。真正的预算由 `_budget_for_groups()`
    # 按**实际工作量（需要命名的组数）**推导——命名工作量随叶组数线性增长，
    # 而固定预算在 100GB 规模必然不够：约 3.3 万文件产生约 1,200 个叶组，
    # 需要约 300 个批次 / 约 285 万 token / 约 74 分钟，时间和 token 都会先撞上
    # 上限。一旦耗尽，命名会永久停住、剩下的组只能拿兜底名（实测已复现）。
    # 注意：这里**不再用 min() 设硬上限**——原来的 min(1000,...) 与 min(3600,...)
    # 让人根本无法为大规模语料配置足够的预算。
    LP_CATALOG_REQUESTS = max(1, _positive_int(os.getenv("SJFX_LP_CATALOG_REQUESTS"), 100))
    LP_CATALOG_TOTAL_TOKENS = max(1, _positive_int(os.getenv("SJFX_LP_CATALOG_TOTAL_TOKENS"), 200000))
    LP_CATALOG_SECONDS = max(1, _positive_int(os.getenv("SJFX_LP_CATALOG_SECONDS"), 600))
    # 单位工作量的实测估算，用于把预算折算成"能覆盖多少工作量"。这两个是
    # **环境属性**（换模型/换机器会变），只用来算预算，不参与目录形状。
    # 单次调用 token 的**估算值**，只用于把预算折算成"能覆盖多少工作量"。
    # L3 之后每组输入到 3,000–6,500、批次输入上限 10000，单次调用实际可到
    # 10,000+，所以估算值同步上调；它不影响目录形状，只影响预算推导的松紧。
    LP_CATALOG_CALL_TOKENS = max(500, _positive_int(os.getenv("SJFX_LP_CATALOG_CALL_TOKENS"), 13000))
    LP_CATALOG_CALL_SECONDS = max(1, _positive_int(os.getenv("SJFX_LP_CATALOG_CALL_SECONDS"), 16))
    # 预算安全系数：为重试与主题层预留余量。
    LP_CATALOG_BUDGET_MARGIN = 1.6
    # L3 样本层（2026-09-20）：每组选样份数按**组内异质性自适应**，每份证据
    # 的正文上限单独可调。同质组少给样本（覆盖少数派只在需要时才付 token），
    # 异质组多给样本；实际份数还会被 LP_CATALOG_INPUT_TOKENS 反算出的可承受
    # 份数压住，不会出现"选了一堆却装不进提示词"。
    LP_CATALOG_SAMPLE_TOKENS = max(120, min(1200, _positive_int(os.getenv("SJFX_LP_CATALOG_SAMPLE_TOKENS"), 450)))
    LP_CATALOG_SAMPLES_MIN = max(2, min(24, _positive_int(os.getenv("SJFX_LP_CATALOG_SAMPLES_MIN"), 6)))
    LP_CATALOG_SAMPLES_MAX = max(
        LP_CATALOG_SAMPLES_MIN, min(32, _positive_int(os.getenv("SJFX_LP_CATALOG_SAMPLES_MAX"), 14)))
    # 账②（2026-09-20）：命名车道并发路数。vLLM 已开 --max-num-seqs 2，
    # 但此前 executor 与客户端都写死 1 路，二路批处理能力闲置。上限 4。
    LP_NAMING_CONCURRENCY = max(1, min(4, _positive_int(os.getenv("SJFX_LP_NAMING_CONCURRENCY"), 2)))
    # MMR 权重：score = λ·相关性 − (1−λ)·与已选的最大相似度。
    LP_CATALOG_MMR_LAMBDA = max(0.0, min(1.0, float(os.getenv("SJFX_LP_CATALOG_MMR_LAMBDA", "0.7"))))
    # L1 取文改造（2026-09-20）：每组输入预算。依据是本地服务实测的时间成本结构：
    # **输入 0.7ms/token，输出 22ms/token**（3,861 token 预填充 2.70s 且命中前缀
    # 缓存后 0.66s；输出 768 token 16.75s）。所以"给模型更多证据"很便宜，
    # "让模型多写字"才贵。L2 画像 + L3 自适应样本数会把每组输入用到 3,000–6,500，
    # 因此这里从 2500 提到 7000；输出侧同时收紧（见 naming.SYSTEM）。
    LP_CATALOG_INPUT_TOKENS = min(12000, _positive_int(os.getenv("SJFX_LP_CATALOG_INPUT_TOKENS"), 7000))
    # W-2b：单请求组数上限 4 → 8。
    # 实测命名调用中位 0.56s、token 中位 283 —— 一次请求装 4 组远没有用满一次调用的
    # 往返开销；而命名墙钟里**模型时间只占 15.2%**，瓶颈是"每次只装 4 组"。
    # 上限由 min(4,…) 提到 min(8,…)：设更大的值也仍然受这里的封顶。
    LP_CATALOG_BATCH_SIZE = min(8, _positive_int(os.getenv("SJFX_LP_CATALOG_BATCH_SIZE"), 8))
    # 本进程服务的工作流版本上限。**预留的排他机制**：框架 §11 要求"新包写
    # workflow_version，新旧 Worker 按版本排他领取；旧进程不能继续消费新包"。
    # 实现方向是"只消费 <= 自己版本的包"：这样将来出现 v3 包时，v2 进程会
    # 拒绝领取而不是误读格式。当前所有包都是 1/2，因此行为与改动前一致。
    LP_WORKFLOW_VERSION = max(1, _positive_int(os.getenv("SJFX_LP_WORKFLOW_VERSION"), 2))
    # 与 LP_CATALOG_INPUT_TOKENS 同步放宽：单组输入变大后，6500 的批次上限
    # 会把每次调用压到 1～2 组，反而放大往返开销。批次仍是"按实测 token 打包"。
    LP_CATALOG_BATCH_INPUT_TOKENS = min(10000, _positive_int(os.getenv("SJFX_LP_CATALOG_BATCH_INPUT_TOKENS"), 10000))
    # 输出预算必须跟着组数走：批量请求的输出是 cfg['output'] * 组数 + 32，
    # 原来 240×8+32 = 1952 > 480 的封顶，8 组一定会被截断。
    LP_CATALOG_OUTPUT_TOKENS = min(2400, _positive_int(os.getenv("SJFX_LP_CATALOG_OUTPUT_TOKENS"), 240))
    # **单组模型命名**（大包目录）的输出上限，与上面的批量车道分开。
    # 为什么必须单独给：批量车道一次 4 组、预算按组数放大；而大包目录是**一组一调用**，
    # 240 对"跨领域大组"不够——实测包 lp-6a7119c7（国务院公报语料）：被切断的组在
    # 240 下 JSON 从中间断掉 → 整组作废 → 重试得到同样结果 → 5 次后 deferred，
    # **7 个组 / 276 个文件永远不进目录**（目录页停在 7369/7754）。
    # 放开到 900 实测这些组最大只要 270 token（中位 129），所以 300 有余量。
    # 回滚：SJFX_LP_CATALOG_NAMING_OUTPUT_TOKENS=240。
    LP_CATALOG_NAMING_OUTPUT_TOKENS = min(
        2000, _positive_int(os.getenv("SJFX_LP_CATALOG_NAMING_OUTPUT_TOKENS"), 300))
    # 单请求组数翻倍后单次耗时也变长（实测 4 组 p90 约 10s），30s 超时太紧。
    LP_CATALOG_TIMEOUT = min(120, _positive_int(os.getenv("SJFX_LP_CATALOG_TIMEOUT"), 60))
    # Semantic grouping quality knobs (2026-09-12).  Each defaults to the
    # improved behaviour and can be disabled without code changes:
    #   SJFX_LP_CATALOG_CENTER=0           -> legacy absolute-threshold grouping
    #   SJFX_LP_CATALOG_SIMILARITY=0.94    -> fixed threshold instead of adaptive
    #   SJFX_LP_CATALOG_GROUP_MAX=0        -> never split oversized groups
    #   SJFX_LP_CATALOG_NAME_SINGLETONS=1  -> spend model budget on 1-file groups
    LP_CATALOG_CENTER = os.getenv("SJFX_LP_CATALOG_CENTER", "1").lower() not in {"0", "false", "off"}
    LP_CATALOG_GARBLE_SPLIT = os.getenv("SJFX_LP_CATALOG_GARBLE_SPLIT", "1").lower() not in {"0", "false", "off"}
    LP_CATALOG_NAME_SINGLETONS = os.getenv("SJFX_LP_CATALOG_NAME_SINGLETONS", "0").lower() not in {"0", "false", "off"}
    LP_CATALOG_SINGLETON_MIN_GROUPS = max(1, min(100000, _positive_int(os.getenv("SJFX_LP_CATALOG_SINGLETON_MIN_GROUPS"), 200)))
    LP_CATALOG_GROUP_MAX = max(0, min(5000, _positive_int(os.getenv("SJFX_LP_CATALOG_GROUP_MAX"), 60)))
    # 叶子大小的**自适应上限**（默认开）。固定上限与"一叶一主题"直接冲突：
    # 12,850 个文件按每叶 ≤60 切必然产生 ≥215 片，一个真实主题（平均 584 文件）
    # 会散成十几片，每片单独命名 —— 于是同名是**必然结果**，不是模型的问题。
    # 自适应口径：cap = ceil(N有向量 / LP_KM_TARGET_LEAVES × ratio)，下限仍是
    # LP_CATALOG_GROUP_MAX，上限 LP_CATALOG_GROUP_MAX_CEIL。
    # 回滚：SJFX_LP_CATALOG_GROUP_MAX_AUTO=0（完整恢复固定 60 的行为）。
    LP_CATALOG_GROUP_MAX_AUTO = os.getenv("SJFX_LP_CATALOG_GROUP_MAX_AUTO", "1").strip().lower() in {
        "1", "true", "yes", "on"}
    LP_CATALOG_GROUP_MAX_RATIO = max(1.0, min(8.0, float(os.getenv("SJFX_LP_CATALOG_GROUP_MAX_RATIO", "1.5"))))
    LP_CATALOG_GROUP_MAX_CEIL = max(60, min(5000, _positive_int(os.getenv("SJFX_LP_CATALOG_GROUP_MAX_CEIL"), 500)))
    # **同名合并**的成员上限倍率（2026-09-20）。同名合并（merge_identical_names）如果
    # 直接沿用叶子上限 cap=ceil(N/120×1.5)，就把自己该修的问题挡住了：实测包 48db486cf0a0
    # cap=102，而 6 组同名主题的成员合计 121~170，全部"超上限"被跳过 —— 于是"同名重复"
    # 一条都没并掉，只剩两组小到能塞进 102 的。但 cap 的设计意图恰恰是"让一个真实主题
    # 留在一个叶子里"（见 effective_group_max 的 docstring），用它拒绝并合同名主题是自相
    # 矛盾。同名是"同一主题"最强的证据，所以允许更宽的并合：上限取
    # max(叶子上限, ceil(N有向量/LP_KM_TARGET_LEAVES × 本倍率))，仍留一个上限防止
    # 一个泛化名把整个包并成一坨。回滚：SJFX_LP_CATALOG_GROUP_MERGE_RATIO=1.5（退回叶子
    # 上限口径，等价于改修前行为）。
    LP_CATALOG_GROUP_MERGE_RATIO = max(1.0, min(50.0, float(os.getenv("SJFX_LP_CATALOG_GROUP_MERGE_RATIO", "4.0"))))
    # 叶组数的**增长上限**：split_oversized 每轮都会被调用，原来没有任何累计上限，
    # 于是叶组只增不减（实测 cssj4 从 170 单调涨到 412，topic merge 跑了 23,405 次
    # 却永远收敛不了）。超过 target_leaves × 这个系数就停止再切。
    LP_CATALOG_LEAF_GROWTH_FACTOR = max(1.0, min(20.0, float(os.getenv("SJFX_LP_CATALOG_LEAF_GROWTH_FACTOR", "2.0"))))
    # 标签历史每组保留的版本数（恒留第 1 版）。历史表曾经涨到 489.6 MB / 41% 的
    # 库体积、35.3 倍冗余，而只有"回滚到指定版本"会读它。回滚：设为 0 或负数
    # 会退化成 1（只留最新一版）；要完全关闭裁剪请改 queue_groups 里的调用。
    LP_LABEL_HISTORY_KEEP = max(1, min(200, int(os.getenv("SJFX_LP_LABEL_HISTORY_KEEP", "5") or 5)))
    # 同名叶子归一：只在同一主题内合并，超过 2×cap 才允许再切。
    LP_CATALOG_NORMALIZE_LEAVES = os.getenv("SJFX_LP_CATALOG_NORMALIZE_LEAVES", "1").strip().lower() in {
        "1", "true", "yes", "on"}
    # 情报概览的"研究方向"：**独立预算通道，不占目录命名轮次预算**。
    # 理由：① 时序不同（目录预算的目的是结构定稿，方向依赖目录**加**深度摘要，是下游）；
    #      ② 轮次语义已被污染（实测 req=707/1000 就 exhausted，是 3600s 超时）；
    #      ③ 失败隔离（方向失败不该影响目录）；④ 只需 3 次调用，单列更易审计。
    # 但与目录**共用**：lp_catalog_calls 计量表 + 并发闸门 + 每包每日硬顶。
    LP_DIRECTIONS_ENABLED = os.getenv("SJFX_LP_DIRECTIONS", "1").strip().lower() in {
        "1", "true", "yes", "on"}
    # 产品口径：**1 个最推荐 + 2 个其他推荐，不做人工确认**。
    LP_DIRECTION_MAX = max(1, min(5, _positive_int(os.getenv("SJFX_LP_DIRECTION_MAX"), 3)))
    LP_DIRECTION_TIMEOUT = max(30, min(600, _positive_int(os.getenv("SJFX_LP_DIRECTION_TIMEOUT"), 150)))
    LP_DIRECTION_MAX_TOKENS = max(256, min(4000, _positive_int(os.getenv("SJFX_LP_DIRECTION_MAX_TOKENS"), 1400)))
    LP_DIRECTION_REQUESTS_PER_DAY = max(1, min(500, _positive_int(
        os.getenv("SJFX_LP_DIRECTION_REQUESTS_PER_DAY"), 20)))
    LP_DIRECTION_CATEGORIES = max(3, min(40, _positive_int(os.getenv("SJFX_LP_DIRECTION_CATEGORIES"), 14)))
    LP_CATALOG_ADAPTIVE_PERCENTILE = max(5, min(90, _positive_int(os.getenv("SJFX_LP_CATALOG_ADAPTIVE_PERCENTILE"), 40)))
    try:
        _lp_similarity = float(os.getenv("SJFX_LP_CATALOG_SIMILARITY", "0") or 0)
    except (TypeError, ValueError):
        _lp_similarity = 0.0
    LP_CATALOG_SIMILARITY = max(0.0, min(0.999, _lp_similarity))
    # 预览车道的并发路数。**默认仍是 2**（保持现有行为）；上限由 4 放宽到 8，
    # 因为 100GB/10 小时的预算模型显示：预览（21 万文件 ≈ 3.9h @并发2）是仅次
    # 于嵌入的第二大项，而机器有 16 核、这里只用 2 路。放宽上限不改默认行为，
    # 只有显式设 SJFX_LP_PREVIEW_CONCURRENCY 才会生效——并发扩展性需要先做一次
    # 基准（见 docs/large-catalog-evidence-l1-l3.md 第七节）。
    LP_PREVIEW_CONCURRENCY = min(8, _positive_int(os.getenv("SJFX_LP_PREVIEW_CONCURRENCY"), 2))
    # 大数据包"深度解析+模型摘要"阶段的并发文件数。
    # 默认 1 = 历史行为（一次一个文件，串行）。
    # 实测（2026-09-16，真实契约 text[:24000] + 500 输出 token，6 个真实 PDF）：
    #   并发 1 -> 每文件 16.13 秒，输出吞吐 31.0 tok/s，显存峰值 22489 MiB
    #   并发 2 -> 每文件 11.14 秒，输出吞吐 44.9 tok/s，显存峰值 22489 MiB（+0）
    #            提速 1.45x；vLLM 侧 Running: 2 reqs, Waiting: 0，KV 池占用 27.7%
    # 前提：服务端必须能同时跑 2 个序列（--max-num-seqs >= 2），否则设置无效。
    LP_DEEP_CONCURRENCY = max(1, min(8, _positive_int(os.getenv("SJFX_LP_DEEP_CONCURRENCY"), 1)))
    # ---- 目录分组模式（2026-09-16 新增）----
    # 0（默认）= 原有增量贪心分组，行为与改动前完全一致。
    # 1 = 全量嵌入后按 k-means 重新分组（每 KM_RECLUSTER_EVERY 个文件重算一次）。
    #     实测（zzdsjb 1004 文件）：原有算法 75% 的组只有 1 个文件；
    #     k-means 在 k=60 时为 0% 单人组，且主题不再混域。
    #     回滚：把 SJFX_LP_CATALOG_KMODE 设为 0 或删除该行，重启大包 Worker。
    LP_CATALOG_KMODE = os.getenv("SJFX_LP_CATALOG_KMODE", "0").strip().lower() in {"1", "true", "yes", "on"}
    # 【已废弃】曾经表示"每积累多少个已嵌入文件就重算一次 k-means"。
    # 这个"批次中途重算"正是目录失真的根因：它在语料只处理一半时就执行全局聚类，
    # 把尚未解析的文件当成"没有素材"倒进无向量的兜底分组（实测 1040 个文件里
    # 530 个被吞掉）。现在全局聚类只在"所有有素材的文件都有向量"之后执行一次，
    # 由 LP_KM_SIGNATURE 变化驱动，本项不再被读取。保留仅为兼容旧 .env。
    LP_KM_RECLUSTER_EVERY = max(50, min(20000, _positive_int(os.getenv("SJFX_LP_KM_RECLUSTER_EVERY"), 500)))
    # 叶组数量 = clamp(文件数 / per_group, KM_MIN, KM_MAX)，
    # 其中 per_group = max(KM_PER_GROUP, 文件数 / KM_TARGET_LEAVES)。
    # 固定 per_group 在 100GB 规模会失效：实测 1040 文件 → 88 个叶组是可浏览的，
    # 但按同一比例外推到约 3.5 万文件（100GB）会得到约 2170 个叶组，
    # 塞进 12 个主题后每个主题约 180 个条目，"两层目录"就失去意义。
    # 因此改为按"目标叶组数"推导：不论语料多大，叶组数都收敛到可浏览区间。
    #
    # ---- k 自适应（闭环，2026-09-20 实测后启用）----
    # 一律 k=120 的问题是：同一份 8,158 文件的语料，叶组中位 55 个文件时
    # 模型判"跨领域拼凑(mixed)"的比例实测 **40%**；把 k 提到 300（中位 19 个文件）
    # 后降到 **27%**，名称也明显更具体（「人口普查数据、经济指标与地理实体条目汇编」
    # → 「人口普查统计与行政区划资料」）。而 12,883 文件那个包在 k=120 时状态名 0%、
    # 只有 3% 带复核标记 —— 同一个 k 对两份语料一个太粗、一个正好。
    # 所以改成**用模型自己的判定当反馈**（不引入任何几何阈值：本语料相似度基线
    # 0.80–0.85、相关 0.94，判别区间太窄，实测几何判据判别力为零）：
    #   叶组定稿后统计"purity=mixed"的叶组占比，≥ 阈值就升一档 k 重新聚类，
    #   最多升 LP_KM_ESCALATE_MAX 档（每档命名成本 ×factor，必须先有上限）。
    # 代价实测：k 120→300，命名调用 158→364 次（本包 12→28 分钟）。
    # 回滚：SJFX_LP_KM_ADAPTIVE=0（退回一律 LP_KM_TARGET_LEAVES）。
    LP_KM_ADAPTIVE = os.getenv("SJFX_LP_KM_ADAPTIVE", "1").strip().lower() in {"1", "true", "yes", "on"}
    LP_KM_ESCALATE_SHARE = max(0.05, min(0.9, float(os.getenv("SJFX_LP_KM_ESCALATE_SHARE", "0.25"))))
    LP_KM_ESCALATE_FACTOR = max(1.2, min(4.0, float(os.getenv("SJFX_LP_KM_ESCALATE_FACTOR", "2.0"))))
    LP_KM_ESCALATE_MAX = max(0, min(3, _positive_int(os.getenv("SJFX_LP_KM_ESCALATE_MAX"), 1)))
    LP_KM_TARGET_LEAVES = max(8, min(2000, _positive_int(os.getenv("SJFX_LP_KM_TARGET_LEAVES"), 120)))
    LP_KM_PER_GROUP = max(2, min(200, _positive_int(os.getenv("SJFX_LP_KM_PER_GROUP"), 16)))
    LP_KM_MIN = max(2, min(100000, _positive_int(os.getenv("SJFX_LP_KM_MIN"), 8)))
    LP_KM_MAX = max(LP_KM_MIN, min(20000, _positive_int(os.getenv("SJFX_LP_KM_MAX"), 5000)))
    # 单个叶组的文件数上限。实测同质语料（165 个 3GPP 规范）用普通 k-means 会退化成
    # 134/12/8/3/2/2/2/1/1（一个组占 81%）；开启上限后超限的组会递归再分。
    LP_KM_MAX_GROUP = max(4, min(500, _positive_int(os.getenv("SJFX_LP_KM_MAX_GROUP"), 30)))
    # 单个叶组的文件数下限。**默认 1 = 不下限，允许单文件叶组。**
    # 实测：设成 4 时，1040 文件的语料里有 13/69 个叶组是"多个互不相关的原始簇
    # 被强行合并"出来的，模型命名只能退化成"概念A、概念B、概念C"式罗列
    # （如"心肌Bmal1性别转录调控、衰老线粒体特征、iPSC心肌转录变异性…"）。
    # 当初设下限是为了避免"单文件叶组变成只含 1 个子组的主题"，但那一条已经由
    # 主题层的 LP_TOPIC_MIN_CHILDREN + 吸收独立解决，下限不再有必要。
    # 语料里必然存在"和谁都不相干"的文件，强行合并只会毁掉名字——
    # 让它自己成为一个叶组才是诚实的。
    LP_KM_MIN_GROUP = max(1, min(100, _positive_int(os.getenv("SJFX_LP_KM_MIN_GROUP"), 1)))
    # 病态单元素组的**兜底吸收**开关（默认开）。只在 LP_KM_MIN_GROUP<=1
    # （即按上面的说明不设常规下限）**且**单元素组占比达到病态阈值时才生效：
    # 那时才用"按目标叶组数反推的下限"补一次 absorb_undersized。
    # 为什么必须是条件触发而不是直接设下限：上面已经记录过，常规下限（如 4）
    # 会把互不相关的原始簇强行合并，模型命名退化成概念罗列——正常语料不应受影响。
    # 但完全不兜底时，异构语料会失控：实测 6,925 文件的包在旧增量锚点分组下
    # 产出 4,870 个组、其中 4,435 个是单元素组（91%），目录不可浏览且命名节点数失控。
    # 回滚：SJFX_LP_KM_MIN_GROUP_AUTO=0，即完整恢复改动前行为。
    LP_KM_MIN_GROUP_AUTO = os.getenv("SJFX_LP_KM_MIN_GROUP_AUTO", "1").strip().lower() in {
        "1", "true", "yes", "on"}
    # 触发兜底的单元素组占比阈值。
    LP_KM_SINGLETON_ALARM = max(0.05, min(0.95, float(os.getenv("SJFX_LP_SINGLETON_ALARM", "0.25"))))
    # 叶组命名时若模型指出"某些成员明显不属于本组"，就把这些少数异类摘出去
    # 单独成为一个叶组，再各自命名。这是**语义判据**，不含任何语料常量，
    # 因此对不同语料普遍适用；基于相似度的几何阈值在这类语料上不可用
    # （实测判别区间只有 0.1，阈值 0.90 会把目录炸成 726 个叶组）。
    LP_LEAF_SPLIT = os.getenv("SJFX_LP_LEAF_SPLIT", "1").strip().lower() in {"1", "true", "yes", "on"}
    # 每个包允许的最大摘除次数（硬上限，落库计数）。
    # 这是**失控保护**而不是质量调参：实测模型在学术语料上会高估异类比例，
    # 没有上限时叶组会从 85 一路涨到 183、待办数不降反升。
    # 有上限后，无论模型怎么回答，命名调用的总代价都有界。
    # 注意：不能用 _positive_int——它会把显式的 0 抬成 1，而 0 在这里表示"自动取值"。
    try:
        _leaf_split_max = int(str(os.getenv("SJFX_LP_LEAF_SPLIT_MAX", "0")).strip())
    except Exception:
        _leaf_split_max = 0
    LP_LEAF_SPLIT_MAX = max(0, min(100000, _leaf_split_max))
    # 摘除出来的异类，是**新建一个命名叶组**（旧行为），还是移进本包的"待归并"桶
    # （km-other-*，默认）。默认移桶的原因见 naming.split_off_outliers 的注释：
    # 我们实测过四种"搬动前核对"的方案，在本语料上判别力全为零，无法验证这撮人
    # 真的自成一类，因此安全做法是不给它们起名——父组的名字不再被污染，文件仍然
    # 留在清单和成员表里，下次重聚类可以重新安置。
    # 置 1 回到旧行为（每次摘除多 1 个叶组、多 1 次命名调用）。
    LP_LEAF_SPLIT_NAMED = os.getenv("SJFX_LP_LEAF_SPLIT_NAMED", "0").strip().lower() in {"1", "true", "yes", "on"}
    # 「按子主题整体拆开」是**另一件事**，必须有自己的开关（2026-09-20 修正）。
    #
    # 起因：2b 上线时图省事让 split_into_subtopics 复用了 LP_LEAF_SPLIT_NAMED，而那个开关
    # 的语义是"摘出来的**异类**要不要直接成为命名叶组"——正是第③项明确要去掉的旧行为。
    # 于是 `SJFX_LP_LEAF_SPLIT_NAMED=1` 一开，子主题拆分开是开了，**异类直接成组也一起
    # 回来了**（第③项被静默覆盖）。两件事的价值取向相反：
    #   · 异类（"谁不属于本组"）：无法验证这撮人自成一类 → 不给名字，进待归并桶；
    #   · 子主题（"整组是跨领域拼凑"）：模型已在同一次调用里给出各子主题的名字与成员，
    #     零额外调用，且用户口径明确要求"拆开、各自独立、各自有名字"。
    # 所以拆成两个开关：本开关默认开（子主题拆分），LP_LEAF_SPLIT_NAMED 默认关（异类不成组）。
    # 回滚：SJFX_LP_LEAF_SPLIT_SUBTOPICS=0（子主题不再拆，异类仍走待归并桶）。
    LP_LEAF_SPLIT_SUBTOPICS = os.getenv("SJFX_LP_LEAF_SPLIT_SUBTOPICS", "1").strip().lower() in {
        "1", "true", "yes", "on"}
    # **重试耗尽的组**给一个抽取式兜底名（默认开）。为什么必须有这条路：
    # 模型命名失败的重试是同一份证据、同一个提示词，模型给出同一句话 → attempts 到
    # LP_NAME_MAX_ATTEMPTS 上限后任务不再被拾取；而模型命名开启时抽取式命名 name_batch
    # 是被禁用的（它会写组状态、把模型命名循环卡死）。两边一夹，那一组**永远没有名字**，
    # 目录覆盖率的窟窿没人补（生产实测：包 lp-6a7119c7 目录页停在 7369/7754）。
    # 兜底只写标签（model='fallback-naming'、needs_review=1），**不碰组状态**。
    # 回滚：SJFX_LP_NAME_FALLBACK=0。
    LP_NAME_FALLBACK = os.getenv("SJFX_LP_NAME_FALLBACK", "1").strip().lower() in {"1", "true", "yes", "on"}
    # 每个被合并叶组最多是叶子上限的几倍（见 merge_size_ceiling 的钳制）。
    LP_CATALOG_GROUP_MERGE_MAX_FACTOR = max(
        1.0, min(20.0, float(os.getenv("SJFX_LP_CATALOG_GROUP_MERGE_MAX_FACTOR", "3.0"))))
    # 目标叶组数 120 时允许 240 次摘除——足够纠正混杂，又不至于失控。
    # k-means 迭代次数与质心采样上限（全量文件只参与分配，不参与迭代，保证大包可用）。
    LP_KM_ITERS = max(5, min(200, _positive_int(os.getenv("SJFX_LP_KM_ITERS"), 40)))
    LP_KM_FIT_SAMPLE = max(200, min(50000, _positive_int(os.getenv("SJFX_LP_KM_FIT_SAMPLE"), 4000)))
    # 主题层：0=原行为（单层目录 + 层级归纳，保持不变）；
    #         1=在叶组命名后，由模型按**叶组名**归并出上位主题（两层目录）。
    LP_TOPIC_MERGE = os.getenv("SJFX_LP_TOPIC_MERGE", "0").strip().lower() in {"1", "true", "yes", "on"}
    LP_TOPIC_MERGE_MIN_GROUPS = max(4, min(4000, _positive_int(os.getenv("SJFX_LP_TOPIC_MERGE_MIN_GROUPS"), 12)))
    # 【形状参数】每个主题下希望看到多少个叶组。这是人浏览第二层的舒适度，
    # 与语料规模无关。**主题层不再有固定数量配置**：
    #   主题数   = clamp(叶组数 / 本值, 6, 24)
    #   子组上限 = ceil(叶组数 / 主题数 * 1.8)
    # 这样不论导入 100 个还是 10 万个文件，顶层都落在可浏览的 [6, 24]。
    # 固定值在大规模下会崩：1,200 个叶组配固定 target=12/cap=20 会算出 72 个主题。
    LP_TOPIC_LEAVES_PER_TOPIC = max(3, min(100, _positive_int(os.getenv("SJFX_LP_TOPIC_LEAVES_PER_TOPIC"), 12)))
    # 【已废弃】主题数固定值与子组数固定上限。主题层改为按实际叶组数推导后不再读取。
    # 保留仅为兼容旧 .env，避免历史配置直接报错。
    LP_TOPIC_TARGET = max(3, min(50, _positive_int(os.getenv("SJFX_LP_TOPIC_TARGET"), 12)))
    # 主题层的下限仍然是形状常量：少于 3 个子组就不算一个主题。
    LP_TOPIC_MIN_CHILDREN = max(2, min(50, _positive_int(os.getenv("SJFX_LP_TOPIC_MIN_CHILDREN"), 3)))
    LP_TOPIC_MAX_CHILDREN = max(LP_TOPIC_MIN_CHILDREN,
                                min(200, _positive_int(os.getenv("SJFX_LP_TOPIC_MAX_CHILDREN"), 20)))
    # 【方案 A】情报概览的两个里程碑：
    #   catalog = 智能目录（含主题层）确定后 —— 主动生成**第一版**并冻结快照
    #   deep    = 深度摘要排空后        —— 依据深度摘要内容**更新**为第二版
    # 两版只差"证据层级"（快速预览 → 深度摘要）与研究方向的重估，
    # **不改变目录结构**。生成失败绝不影响主流程。
    LP_OVERVIEW_EAGER = os.getenv("SJFX_LP_OVERVIEW_EAGER", "1").strip().lower() in {"1", "true", "yes", "on"}
    # 目录结构在 catalog_ready 之后冻结：深度摘要会改写正文并触发重新嵌入，
    # 若继续重算聚类，group id 会全部变化，已生成的目录名与第一版情报概览同时作废。
    # 需要恢复"深度摘要后重算目录"时设为 0。
    LP_CATALOG_FREEZE_AFTER_CATALOG = os.getenv(
        "SJFX_LP_CATALOG_FREEZE_AFTER_CATALOG", "1").strip().lower() in {"1", "true", "yes", "on"}

    LARGE_PACKAGE_THRESHOLD_BYTES = int(os.getenv("LARGE_PACKAGE_THRESHOLD_BYTES", str(1024 * 1024 * 1024)))
    LARGE_PACKAGE_THRESHOLD_FILES = int(os.getenv("LARGE_PACKAGE_THRESHOLD_FILES", "3000"))
    # Packages at or above the normal large-package threshold use a separate
    # directory-first workflow. It builds a complete map and bounded model
    # cards, then waits for an explicit deep-analysis selection.
    LARGE_PACKAGE_DIRECTORY_MODE = os.getenv(
        "LARGE_PACKAGE_DIRECTORY_MODE", "1"
    ).strip().lower() in {"1", "true", "yes", "on"}
    LARGE_PACKAGE_DIRECTORY_MODEL_FILE_LIMIT = max(
        12, min(500, int(os.getenv("LARGE_PACKAGE_DIRECTORY_MODEL_FILE_LIMIT", "120")))
    )
    LARGE_PACKAGE_DIRECTORY_PREVIEW_BYTES_PER_FILE = max(
        8192, min(256 * 1024, int(os.getenv("LARGE_PACKAGE_DIRECTORY_PREVIEW_BYTES_PER_FILE", str(64 * 1024))))
    )
    LARGE_PACKAGE_DIRECTORY_PREVIEW_TOTAL_BYTES = max(
        LARGE_PACKAGE_DIRECTORY_PREVIEW_BYTES_PER_FILE,
        int(os.getenv("LARGE_PACKAGE_DIRECTORY_PREVIEW_TOTAL_BYTES", str(8 * 1024 * 1024 * 1024))),
    )
    LARGE_PACKAGE_DIRECTORY_BATCH_FILES = max(
        100, min(5000, int(os.getenv("LARGE_PACKAGE_DIRECTORY_BATCH_FILES", "1000")))
    )
    # Explicit selections are parsed in bounded, resumable slices. Keep these
    # values in Config because API/status and the planner both expose them.
    LARGE_PACKAGE_SELECTED_PARSE_MAX_FILES = max(
        100,
        min(10000, int(os.getenv("LARGE_PACKAGE_SELECTED_PARSE_MAX_FILES", "2000"))),
    )
    LARGE_PACKAGE_SELECTED_PARSE_MAX_BYTES = max(
        256 * 1024 * 1024,
        min(
            TEN_GIB_BYTES,
            int(os.getenv("LARGE_PACKAGE_SELECTED_PARSE_MAX_BYTES", str(4 * 1024 * 1024 * 1024))),
        ),
    )
    LARGE_PACKAGE_SELECTED_MODEL_FILE_LIMIT = max(
        12,
        min(
            500,
            int(os.getenv(
                "LARGE_PACKAGE_SELECTED_MODEL_FILE_LIMIT",
                os.getenv("LARGE_PACKAGE_DIRECTORY_MODEL_FILE_LIMIT", "120"),
            )),
        ),
    )
    LARGE_PACKAGE_SELECTED_MODEL_MAX_BYTES = max(
        64 * 1024 * 1024,
        min(
            TEN_GIB_BYTES,
            int(os.getenv("LARGE_PACKAGE_SELECTED_MODEL_MAX_BYTES", str(1024 * 1024 * 1024))),
        ),
    )
    LARGE_PACKAGE_SELECTED_BATCH_FILES = max(
        50, min(500, int(os.getenv("LARGE_PACKAGE_SELECTED_BATCH_FILES", "250"))),
    )
    LARGE_PACKAGE_INITIAL_PARSE_FILES = max(
        1, min(500, int(os.getenv("LARGE_PACKAGE_INITIAL_PARSE_FILES", "500")))
    )
    LARGE_PACKAGE_DEEPEN_BATCH_FILES = max(
        1, min(500, int(os.getenv("LARGE_PACKAGE_DEEPEN_BATCH_FILES", "500")))
    )
    # Unknown large packages are first explored with deterministic bounded
    # samples.  At the current 50k-file default the 8 GiB slice is sufficient
    # for every file at 96 KiB while still providing a hard operator budget.
    LARGE_PACKAGE_PREVIEW_BYTES_PER_FILE = max(
        4096, min(1024 * 1024, int(os.getenv("LARGE_PACKAGE_PREVIEW_BYTES_PER_FILE", str(96 * 1024))))
    )
    LARGE_PACKAGE_PREVIEW_TOTAL_BYTES = max(
        LARGE_PACKAGE_PREVIEW_BYTES_PER_FILE,
        int(os.getenv("LARGE_PACKAGE_PREVIEW_TOTAL_BYTES", str(8 * 1024 * 1024 * 1024))),
    )
    LARGE_PACKAGE_PREVIEW_ZIP_MEMBERS = max(
        1, min(1000, int(os.getenv("LARGE_PACKAGE_PREVIEW_ZIP_MEMBERS", "80")))
    )
    LARGE_PACKAGE_PREVIEW_ZIP_MEMBER_BYTES = max(
        256, min(64 * 1024, int(os.getenv("LARGE_PACKAGE_PREVIEW_ZIP_MEMBER_BYTES", "8192")))
    )
    LARGE_PACKAGE_PREVIEW_SLICE_FILES = max(
        1, min(1000, int(os.getenv("LARGE_PACKAGE_PREVIEW_SLICE_FILES", "100")))
    )
    LARGE_PACKAGE_PREVIEW_SLICE_SECONDS = max(
        5, min(300, int(os.getenv("LARGE_PACKAGE_PREVIEW_SLICE_SECONDS", "30")))
    )
    # Both preview and selected deep-analysis work advance in durable bounded
    # batches. Individual file results are checkpointed immediately.
    LARGE_PACKAGE_BATCH_FILES = max(
        1, min(500, int(os.getenv("LARGE_PACKAGE_BATCH_FILES", "500")))
    )
    CONVERSATION_ANALYSIS_BATCH_FILES = max(
        20, min(50, int(os.getenv("CONVERSATION_ANALYSIS_BATCH_FILES", "30")))
    )
    CONVERSATION_MAX_CANDIDATE_EVIDENCE = max(
        100,
        min(5000, int(os.getenv("CONVERSATION_MAX_CANDIDATE_EVIDENCE", "1600"))),
    )
    # Interactive turns must remain responsive while large background
    # analyses continue.  These limits apply only to the conversational
    # retrieval window; the durable evidence index itself remains complete.
    CONVERSATION_RETRIEVAL_CANDIDATE_LIMIT = max(
        100, min(2000, int(os.getenv("CONVERSATION_RETRIEVAL_CANDIDATE_LIMIT", "600")))
    )
    CONVERSATION_RETRIEVAL_DOCUMENT_LIMIT = max(
        50, min(1000, int(os.getenv("CONVERSATION_RETRIEVAL_DOCUMENT_LIMIT", "500")))
    )
    CONVERSATION_RETRIEVAL_TIMEOUT_SECONDS = max(
        2.0, min(30.0, float(os.getenv("CONVERSATION_RETRIEVAL_TIMEOUT_SECONDS", "8")))
    )
    CONVERSATION_MODEL_TIMEOUT_SECONDS = max(
        10, min(180, int(os.getenv("CONVERSATION_MODEL_TIMEOUT_SECONDS", "45")))
    )
    CONVERSATION_MAX_REVISION_ATTEMPTS = max(
        0, min(2, int(os.getenv("CONVERSATION_MAX_REVISION_ATTEMPTS", "1")))
    )
    CONVERSATION_MAX_PROMOTION_DEPTH = max(
        1, min(12, int(os.getenv("CONVERSATION_MAX_PROMOTION_DEPTH", "3")))
    )
    LARGE_PACKAGE_BACKGROUND_BATCH_FILES = max(
        1, min(500, int(os.getenv("LARGE_PACKAGE_BACKGROUND_BATCH_FILES", "500")))
    )
    LARGE_PACKAGE_BACKGROUND_BACKFILL = os.getenv(
        "LARGE_PACKAGE_BACKGROUND_BACKFILL", "1"
    ).strip().lower() in {"1", "true", "yes", "on"}
    LOGICAL_PARTITION_BYTES = max(
        256 * 1024,
        min(64 * 1024 * 1024, int(os.getenv("LOGICAL_PARTITION_BYTES", str(1024 * 1024)))),
    )
    LOGICAL_PARTITION_ROWS = max(
        500, min(100000, int(os.getenv("LOGICAL_PARTITION_ROWS", "10000")))
    )
    MAX_LOGICAL_UNITS_PER_CONTAINER = max(
        1000, min(200000, int(os.getenv("MAX_LOGICAL_UNITS_PER_CONTAINER", "200000")))
    )
    BACKGROUND_MAX_LOAD_RATIO = max(
        0.1, min(4.0, float(os.getenv("BACKGROUND_MAX_LOAD_RATIO", "0.90")))
    )
    BACKGROUND_MIN_AVAILABLE_MEMORY_MB = max(
        512,
        int(os.getenv(
            "BACKGROUND_MIN_AVAILABLE_MEMORY_MB",
            str(max(2048, MAX_PARSE_PROCESS_MEMORY_MB + 1024)),
        )),
    )
    BACKGROUND_RESOURCE_RETRY_SECONDS = max(
        5, min(300, int(os.getenv("BACKGROUND_RESOURCE_RETRY_SECONDS", "30")))
    )
    # Package-wide structures retain only a head/middle/tail semantic sketch.
    LARGE_PACKAGE_OVERVIEW_CHARS_PER_FILE = max(
        1000, min(12000, int(os.getenv("LARGE_PACKAGE_OVERVIEW_CHARS_PER_FILE", "4000")))
    )
    LARGE_PACKAGE_OVERVIEW_EVIDENCE_PER_FILE = max(
        1, min(20, int(os.getenv("LARGE_PACKAGE_OVERVIEW_EVIDENCE_PER_FILE", "6")))
    )
    MAX_ARCHIVE_ENTRIES = max(1, min(50000, int(os.getenv("MAX_ARCHIVE_ENTRIES", "5000"))))
    # Python's ZipFile must materialise the central directory when opening a
    # ZIP. Preflight its declared count and reject a pathological directory
    # before the standard library allocates an unbounded ZipInfo list.
    MAX_ZIP_CENTRAL_DIRECTORY_ENTRIES = max(
        MAX_ARCHIVE_ENTRIES,
        min(200000, int(os.getenv("MAX_ZIP_CENTRAL_DIRECTORY_ENTRIES", "200000"))),
    )
    MAX_ARCHIVE_FILE_BYTES = content_byte_limit("MAX_ARCHIVE_FILE_BYTES")
    MAX_ARCHIVE_MEMBER_BYTES = content_byte_limit("MAX_ARCHIVE_MEMBER_BYTES")
    MAX_ARCHIVE_UNCOMPRESSED_BYTES = content_byte_limit("MAX_ARCHIVE_UNCOMPRESSED_BYTES")
    MAX_ARCHIVE_COMPRESSION_RATIO = max(
        1.0, min(10000.0, float(os.getenv("MAX_ARCHIVE_COMPRESSION_RATIO", "200")))
    )
    MAX_ARCHIVE_MEMBER_PATH_DEPTH = max(
        0, min(256, int(os.getenv("MAX_ARCHIVE_MEMBER_PATH_DEPTH", "32")))
    )
    # Mail research is enabled for this SJFX deployment. These budgets only
    # govern services/mail and do not alter the established document pipeline.
    MAIL_FEATURE_ENABLED = os.getenv("MAIL_FEATURE_ENABLED", "1").strip().lower() in {
        "1", "true", "yes", "on"
    }
    MAIL_IMPORT_SLICE_MESSAGES = max(1, min(1000, int(os.getenv("MAIL_IMPORT_SLICE_MESSAGES", "50"))))
    MAIL_IMPORT_SLICE_BYTES = max(1024 * 1024, min(TEN_GIB_BYTES, int(os.getenv("MAIL_IMPORT_SLICE_BYTES", "134217728"))))
    MAIL_MAX_MESSAGE_BYTES = max(1024 * 1024, min(TEN_GIB_BYTES, int(os.getenv("MAIL_MAX_MESSAGE_BYTES", "52428800"))))
    MAIL_MAX_NESTED_MESSAGE_DEPTH = max(0, min(16, int(os.getenv("MAIL_MAX_NESTED_MESSAGE_DEPTH", "3"))))
    MAIL_MAX_MIME_PARTS_PER_MESSAGE = max(1, min(10000, int(os.getenv("MAIL_MAX_MIME_PARTS_PER_MESSAGE", "1000"))))
    MAIL_MAX_ATTACHMENT_BYTES = max(1024 * 1024, min(TEN_GIB_BYTES, int(os.getenv("MAIL_MAX_ATTACHMENT_BYTES", "536870912"))))
    MAIL_TEXT_SEGMENT_CHARS = max(256, min(16000, int(os.getenv("MAIL_TEXT_SEGMENT_CHARS", "1800"))))
    MAIL_TOOL_CONTEXT_MAX_CHARS = max(1000, min(200000, int(os.getenv("MAIL_TOOL_CONTEXT_MAX_CHARS", "24000"))))
    MAIL_RESEARCH_MAX_TOOL_CALLS = max(1, min(100, int(os.getenv("MAIL_RESEARCH_MAX_TOOL_CALLS", "18"))))
    MAIL_RESEARCH_MAX_MODEL_CALLS = max(0, min(20, int(os.getenv("MAIL_RESEARCH_MAX_MODEL_CALLS", "7"))))
    MAIL_RESEARCH_TIMEOUT_SECONDS = max(60, min(3600, int(os.getenv("MAIL_RESEARCH_TIMEOUT_SECONDS", "600"))))
    MAIL_OCR_MAX_CONCURRENCY = max(1, min(8, int(os.getenv("MAIL_OCR_MAX_CONCURRENCY", "1"))))
    MAIL_ENABLE_VECTOR_INDEX = os.getenv("MAIL_ENABLE_VECTOR_INDEX", "0").strip().lower() in {"1", "true", "yes", "on"}
    # Mail-only additions. Disable each independently; no global model settings change.
    MAIL_ENABLE_AGENT_MINING = os.getenv("MAIL_ENABLE_AGENT_MINING", "1").lower() in {"1", "true", "yes", "on"}
    MAIL_ENABLE_CONVERSATIONS = os.getenv("MAIL_ENABLE_CONVERSATIONS", "1").lower() in {"1", "true", "yes", "on"}
    MAIL_ENABLE_FACT_EXTRACTION = os.getenv("MAIL_ENABLE_FACT_EXTRACTION", "1").lower() in {"1", "true", "yes", "on"}
    MAIL_ENABLE_GRAPH_METRICS = os.getenv("MAIL_ENABLE_GRAPH_METRICS", "1").lower() in {"1", "true", "yes", "on"}
    MAIL_ENABLE_ATTACHMENT_VERSIONS = os.getenv("MAIL_ENABLE_ATTACHMENT_VERSIONS", "1").lower() in {"1", "true", "yes", "on"}
    MAIL_ENABLE_ISOLATED_PARSING = os.getenv("MAIL_ENABLE_ISOLATED_PARSING", "1").lower() in {"1", "true", "yes", "on"}
    MAIL_ENABLE_NESTED_MESSAGES = os.getenv("MAIL_ENABLE_NESTED_MESSAGES", "1").lower() in {"1", "true", "yes", "on"}
    MAIL_EMBED_BASE_URL = _local_model_url(os.getenv("MAIL_EMBED_BASE_URL", VLLM_EMBED_BASE_URL))
    MAIL_EMBED_MODEL = os.getenv("MAIL_EMBED_MODEL", VLLM_EMBED_MODEL)
    MAIL_EMBED_BATCH = max(1, min(8, int(os.getenv("MAIL_EMBED_BATCH", "4"))))
    MAIL_EMBED_MAX_CHARS = max(128, min(1180, int(os.getenv("MAIL_EMBED_MAX_CHARS", "1180"))))
    MAIL_EMBED_INTERVAL_SECONDS = max(0.0, float(os.getenv("MAIL_EMBED_INTERVAL_SECONDS", "2")))
    MAIL_EMBED_PAUSED = os.getenv("MAIL_EMBED_PAUSED", "0").lower() in {"1", "true", "yes", "on"}
    MAIL_RRF_K = max(1, int(os.getenv("MAIL_RRF_K", "60")))
    MAIL_RERANK_ENABLED = os.getenv("MAIL_RERANK_ENABLED", "1").lower() in {"1", "true", "yes", "on"}
    MAIL_RERANK_TOP_N = max(5, min(50, int(os.getenv("MAIL_RERANK_TOP_N", "30"))))
    MAIL_FACTEXTRACT_SLICE_MESSAGES = max(1, min(20, int(os.getenv("MAIL_FACTEXTRACT_SLICE_MESSAGES", "5"))))
    MAIL_SUBJECT_CANDIDATE_WINDOW_DAYS = max(1, int(os.getenv("MAIL_SUBJECT_CANDIDATE_WINDOW_DAYS", "90")))
    MAIL_PARSE_TIMEOUT_SECONDS = max(5, int(os.getenv("MAIL_PARSE_TIMEOUT_SECONDS", "60")))
    MAIL_PARSE_MEMORY_MB = max(256, int(os.getenv("MAIL_PARSE_MEMORY_MB", "2048")))
    MAIL_HTML_REMOTE_RESOURCES = os.getenv("MAIL_HTML_REMOTE_RESOURCES", "0").strip().lower() in {"1", "true", "yes", "on"}
    MAX_STRUCTURED_PROFILE_ROWS = max(100, int(os.getenv("MAX_STRUCTURED_PROFILE_ROWS", "100000")))
    MAX_STRUCTURED_PROFILE_BYTES = int(os.getenv("MAX_STRUCTURED_PROFILE_BYTES", str(256 * 1024 * 1024)))
    MAX_ANALYSIS_JOBS = max(1, int(os.getenv("MAX_ANALYSIS_JOBS", "1")))
    WORKER_POLL_SECONDS = max(0.1, float(os.getenv("WORKER_POLL_SECONDS", "0.5")))
    WORKER_MONITOR_SECONDS = max(0.1, min(2.0, float(os.getenv("WORKER_MONITOR_SECONDS", "0.5"))))
    WORKER_HEARTBEAT_SECONDS = max(1.0, min(30.0, float(os.getenv("WORKER_HEARTBEAT_SECONDS", "3"))))
    WORKER_TERMINATE_GRACE_SECONDS = max(1.0, min(30.0, float(os.getenv("WORKER_TERMINATE_GRACE_SECONDS", "5"))))
    WORKER_STALE_SECONDS = max(60, int(os.getenv("WORKER_STALE_SECONDS", "900")))
    # End-to-end task boundaries are deliberately separate from a single file
    # or model request timeout. Large package analysis/export can run for hours,
    # while an interactive summary must release the queue within minutes.
    JOB_DEFAULT_TIMEOUT_SECONDS = max(60, int(os.getenv("JOB_DEFAULT_TIMEOUT_SECONDS", "3600")))
    JOB_SCAN_TIMEOUT_SECONDS = max(300, int(os.getenv("JOB_SCAN_TIMEOUT_SECONDS", "86400")))
    JOB_ANALYSIS_TIMEOUT_SECONDS = max(300, int(os.getenv("JOB_ANALYSIS_TIMEOUT_SECONDS", "86400")))
    JOB_SUMMARY_TIMEOUT_SECONDS = max(60, int(os.getenv("JOB_SUMMARY_TIMEOUT_SECONDS", "420")))
    JOB_DOCUMENT_SUMMARY_TIMEOUT_SECONDS = max(
        JOB_SUMMARY_TIMEOUT_SECONDS,
        int(os.getenv("JOB_DOCUMENT_SUMMARY_TIMEOUT_SECONDS", "1800")),
    )
    JOB_REPORT_TIMEOUT_SECONDS = max(60, int(os.getenv("JOB_REPORT_TIMEOUT_SECONDS", "900")))
    JOB_CONVERSATION_TURN_TIMEOUT_SECONDS = max(
        60, int(os.getenv("JOB_CONVERSATION_TURN_TIMEOUT_SECONDS", "900"))
    )
    JOB_EXPORT_TIMEOUT_SECONDS = max(300, int(os.getenv("JOB_EXPORT_TIMEOUT_SECONDS", "21600")))
    JOB_TRANSLATION_TIMEOUT_SECONDS = max(
        60, int(os.getenv("JOB_TRANSLATION_TIMEOUT_SECONDS", "7200"))
    )
    JOB_TRANSLATION_PACKAGE_TIMEOUT_SECONDS = max(
        300, int(os.getenv("JOB_TRANSLATION_PACKAGE_TIMEOUT_SECONDS", "86400"))
    )
    MAX_JOB_RESUME_ATTEMPTS = max(1, min(256, int(os.getenv("MAX_JOB_RESUME_ATTEMPTS", "64"))))
    HISTORY_RETENTION_DAYS = max(
        0, min(3650, int(os.getenv("HISTORY_RETENTION_DAYS", "0")))
    )
    HISTORY_MAX_SCANS = max(
        0, min(100000, int(os.getenv("HISTORY_MAX_SCANS", "0")))
    )
    HISTORY_CLEANUP_INTERVAL_SECONDS = max(
        300,
        min(86400, int(os.getenv("HISTORY_CLEANUP_INTERVAL_SECONDS", "3600"))),
    )


def ensure_runtime_directories():
    """Create private runtime directories during an explicit process startup.

    Importing :mod:`config` is intentionally read-only.  This keeps test
    discovery, static analysis and management commands from touching the live
    database/state volume merely because they imported application modules.
    """
    if os.name != "nt":
        os.umask(0o077)
    for directory in (
        Config.DATA_DIR,
        Config.DOCUMENT_CACHE_DIR,
        Config.PARSE_TEMP_DIR,
        Config.OUTPUT_DIR,
        Config.LOG_DIR,
        Config.MODELS_DIR,
        Config.DOCLING_ARTIFACTS_DIR,
        Config.RAPIDOCR_MODEL_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        for private_dir in (
            Config.DATA_DIR,
            Config.DOCUMENT_CACHE_DIR,
            Config.PARSE_TEMP_DIR,
            Config.LOG_DIR,
            Config.OUTPUT_DIR,
        ):
            try:
                private_dir.chmod(0o700)
            except OSError:
                pass
