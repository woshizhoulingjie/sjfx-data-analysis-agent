import os
from datetime import datetime, timezone
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def load_local_env():
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
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


class Config:
    BASE_DIR = BASE_DIR
    DATA_DIR = BASE_DIR / "data"
    OUTPUT_DIR = BASE_DIR / "outputs"
    LOG_DIR = BASE_DIR / "logs"
    MODELS_DIR = BASE_DIR / "models"
    DOCLING_ARTIFACTS_DIR = MODELS_DIR / "docling"
    RAPIDOCR_MODEL_DIR = MODELS_DIR / "rapidocr"
    DB_PATH = DATA_DIR / "agent.db"
    DOCUMENT_CACHE_DIR = DATA_DIR / "document_payloads"
    # The server already provides a local Ollama instance. Keep local mode as
    # the default so an absent .env can never send document text to the internet.
    ENABLE_SHARED_OLLAMA = os.getenv("ENABLE_SHARED_OLLAMA", "0").strip().lower() in {"1", "true", "yes"}
    OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/")
    OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen-agent:latest")
    OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "qwen-embed:latest")
    LLM_MAX_CONCURRENCY = max(1, int(os.getenv("LLM_MAX_CONCURRENCY", "1")))
    # The available Ollama service is shared by the laboratory. Do not send
    # long document prompts to it unless a dedicated scheduling decision was made.
    ENABLE_SHARED_OLLAMA_EMBEDDINGS = os.getenv("ENABLE_SHARED_OLLAMA_EMBEDDINGS", "0").strip().lower() in {"1", "true", "yes"}
    SHARED_OLLAMA_REQUEST_TIMEOUT = max(60, int(os.getenv("SHARED_OLLAMA_REQUEST_TIMEOUT", "600")))
    SHARED_OLLAMA_MAX_CHARS = max(4000, int(os.getenv("SHARED_OLLAMA_MAX_CHARS", "48000")))
    HOST = os.getenv("HOST", "127.0.0.1")
    PORT = int(os.getenv("PORT", "8000"))
    API_ACCESS_TOKEN = os.getenv("SJFX_API_ACCESS_TOKEN", "").strip()
    API_TOKEN_EXPIRES_AT = _token_expiry(os.getenv("SJFX_API_TOKEN_EXPIRES_AT", ""))
    AUTH_REQUIRED = os.getenv("AUTH_REQUIRED", "0" if HOST in {"127.0.0.1", "localhost", "::1"} else "1").strip().lower() in {"1", "true", "yes"}
    _allowed_roots = os.getenv("SCAN_ALLOWED_ROOTS", str(BASE_DIR.parent))
    SCAN_ALLOWED_ROOTS = tuple(Path(item).expanduser().resolve() for item in _split_path_list(_allowed_roots))
    MAX_EXTRACT_CHARS = int(os.getenv("MAX_EXTRACT_CHARS", "60000"))
    MAX_FULL_DOCUMENT_CHARS = int(os.getenv("MAX_FULL_DOCUMENT_CHARS", "2000000"))
    MAX_SINGLE_FILE_BYTES = int(os.getenv("MAX_SINGLE_FILE_BYTES", str(1024 * 1024 * 1024)))
    MAX_PARSE_SECONDS = max(1, int(os.getenv("MAX_PARSE_SECONDS", "300")))
    MAX_WORKER_MEMORY_MB = max(256, int(os.getenv("MAX_WORKER_MEMORY_MB", "8192")))
    MAX_PARSE_PROCESS_MEMORY_MB = max(
        256,
        int(os.getenv("MAX_PARSE_PROCESS_MEMORY_MB", str(MAX_WORKER_MEMORY_MB))),
    )
    ENABLE_PARSE_PROCESS_ISOLATION = os.getenv("ENABLE_PARSE_PROCESS_ISOLATION", "1").strip().lower() in {
        "1", "true", "yes", "on",
    }
    DOCLING_DEVICE = os.getenv("DOCLING_DEVICE", "cpu").strip().lower()
    if DOCLING_DEVICE not in {"cpu", "cuda", "auto"}:
        DOCLING_DEVICE = "cpu"
    DOCLING_CPU_THREADS = max(1, min(64, int(os.getenv("DOCLING_CPU_THREADS", "4"))))
    MAX_DOCUMENT_CHUNKS = int(os.getenv("MAX_DOCUMENT_CHUNKS", "12"))
    # ZIP64 is used by the exporter.  A 4-5 GB analysis package may legitimately
    # need a larger handoff archive than the previous demonstration cap.
    MAX_EXPORT_BYTES = int(os.getenv("MAX_EXPORT_BYTES", str(5 * 1024 * 1024 * 1024)))
    MAX_SCAN_FILES = int(os.getenv("MAX_SCAN_FILES", "50000"))
    # Bound recursive inventory construction before Python stack or hostile media
    # can exhaust the local analysis box.  Symlinks are never followed.
    MAX_SCAN_DEPTH = max(1, min(256, int(os.getenv("MAX_SCAN_DEPTH", "32"))))
    SIDECAR_PAYLOAD_BYTES = int(os.getenv("SIDECAR_PAYLOAD_BYTES", str(256 * 1024)))
    LARGE_PACKAGE_THRESHOLD_BYTES = int(os.getenv("LARGE_PACKAGE_THRESHOLD_BYTES", str(1024 * 1024 * 1024)))
    LARGE_PACKAGE_THRESHOLD_FILES = int(os.getenv("LARGE_PACKAGE_THRESHOLD_FILES", "3000"))
    LARGE_PACKAGE_INITIAL_PARSE_FILES = int(os.getenv("LARGE_PACKAGE_INITIAL_PARSE_FILES", "700"))
    LARGE_PACKAGE_DEEPEN_BATCH_FILES = int(os.getenv("LARGE_PACKAGE_DEEPEN_BATCH_FILES", "500"))
    LARGE_PACKAGE_OVERVIEW_CHARS_PER_FILE = int(os.getenv("LARGE_PACKAGE_OVERVIEW_CHARS_PER_FILE", "30000"))
    MAX_ARCHIVE_ENTRIES = max(1, int(os.getenv("MAX_ARCHIVE_ENTRIES", "1500")))
    MAX_ARCHIVE_MEMBER_BYTES = int(os.getenv("MAX_ARCHIVE_MEMBER_BYTES", str(128 * 1024 * 1024)))
    MAX_ARCHIVE_UNCOMPRESSED_BYTES = int(os.getenv("MAX_ARCHIVE_UNCOMPRESSED_BYTES", str(2 * 1024 * 1024 * 1024)))
    MAX_STRUCTURED_PROFILE_ROWS = max(100, int(os.getenv("MAX_STRUCTURED_PROFILE_ROWS", "100000")))
    MAX_STRUCTURED_PROFILE_BYTES = int(os.getenv("MAX_STRUCTURED_PROFILE_BYTES", str(256 * 1024 * 1024)))
    MAX_ANALYSIS_JOBS = max(1, int(os.getenv("MAX_ANALYSIS_JOBS", "1")))
    WORKER_POLL_SECONDS = max(0.1, float(os.getenv("WORKER_POLL_SECONDS", "0.5")))
    WORKER_STALE_SECONDS = max(60, int(os.getenv("WORKER_STALE_SECONDS", "900")))


Config.DATA_DIR.mkdir(exist_ok=True)
Config.DOCUMENT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
Config.OUTPUT_DIR.mkdir(exist_ok=True)
Config.LOG_DIR.mkdir(exist_ok=True)
Config.MODELS_DIR.mkdir(exist_ok=True)
Config.DOCLING_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
Config.RAPIDOCR_MODEL_DIR.mkdir(parents=True, exist_ok=True)
