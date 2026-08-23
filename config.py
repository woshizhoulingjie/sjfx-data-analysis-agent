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
            "物理断网部署只允许本机 Ollama 地址（127.0.0.1/localhost/::1），拒绝远程模型地址"
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
    # The server already provides a local Ollama instance. Keep local mode as
    # the default so an absent .env can never send document text to the internet.
    ENABLE_SHARED_OLLAMA = os.getenv("ENABLE_SHARED_OLLAMA", "0").strip().lower() in {"1", "true", "yes"}
    OLLAMA_BASE_URL = _local_model_url(os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1"))
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
    OWNER_ID = re.sub(
        r"[^A-Za-z0-9_.-]+", "-", os.getenv("SJFX_OWNER_ID", "primary").strip()
    ).strip("-")[:64] or "primary"
    API_TOKEN_EXPIRES_AT = _token_expiry(os.getenv("SJFX_API_TOKEN_EXPIRES_AT", ""))
    DOWNLOAD_TICKET_TTL_SECONDS = max(
        30, min(600, int(os.getenv("DOWNLOAD_TICKET_TTL_SECONDS", "120")))
    )
    AUTH_REQUIRED = os.getenv("AUTH_REQUIRED", "0" if HOST in {"127.0.0.1", "localhost", "::1"} else "1").strip().lower() in {"1", "true", "yes"}
    _allowed_roots_raw = os.getenv("SCAN_ALLOWED_ROOTS", "").strip()
    if not _allowed_roots_raw and HOST not in {"127.0.0.1", "localhost", "::1"}:
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
        min(8, int(os.getenv("PARSE_MAX_CONCURRENCY", "2"))),
    )
    PARSE_MAX_CONCURRENCY = min(PARSE_MAX_CONCURRENCY, max(1, _cpu_count - 2))
    ENABLE_PARSE_PROCESS_ISOLATION = os.getenv("ENABLE_PARSE_PROCESS_ISOLATION", "1").strip().lower() in {
        "1", "true", "yes", "on",
    }
    DOCLING_DEVICE = os.getenv("DOCLING_DEVICE", "cpu").strip().lower()
    if DOCLING_DEVICE not in {"cpu", "cuda", "auto"}:
        DOCLING_DEVICE = "cpu"
    DOCLING_CPU_THREADS = max(1, min(64, int(os.getenv("DOCLING_CPU_THREADS", "4"))))
    MAX_DOCUMENT_CHUNKS = int(os.getenv("MAX_DOCUMENT_CHUNKS", "12"))
    # ZIP64 and streaming writes support a complete 10 GiB handoff without
    # loading the source package into memory.
    MAX_EXPORT_BYTES = content_byte_limit("MAX_EXPORT_BYTES")
    EXPORT_DISK_RESERVE_BYTES = int(os.getenv("EXPORT_DISK_RESERVE_BYTES", str(1024 * 1024 * 1024)))
    # Default conservatively, but allow an operator to raise the inventory
    # boundary for very large evidence packages after sizing memory/disk.
    MAX_SCAN_FILES = max(1, min(1_000_000, int(os.getenv("MAX_SCAN_FILES", "50000"))))
    # Bound recursive inventory construction before Python stack or hostile media
    # can exhaust the local analysis box.  Symlinks are never followed.
    MAX_SCAN_DEPTH = max(1, min(256, int(os.getenv("MAX_SCAN_DEPTH", "32"))))
    MAX_SCAN_DIRECTORIES = max(1, min(1_000_000, int(os.getenv("MAX_SCAN_DIRECTORIES", "50000"))))
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
    SIDECAR_PAYLOAD_BYTES = int(os.getenv("SIDECAR_PAYLOAD_BYTES", str(256 * 1024)))
    LARGE_PACKAGE_THRESHOLD_BYTES = int(os.getenv("LARGE_PACKAGE_THRESHOLD_BYTES", str(1024 * 1024 * 1024)))
    LARGE_PACKAGE_THRESHOLD_FILES = int(os.getenv("LARGE_PACKAGE_THRESHOLD_FILES", "3000"))
    LARGE_PACKAGE_INITIAL_PARSE_FILES = int(os.getenv("LARGE_PACKAGE_INITIAL_PARSE_FILES", "700"))
    LARGE_PACKAGE_DEEPEN_BATCH_FILES = int(os.getenv("LARGE_PACKAGE_DEEPEN_BATCH_FILES", "500"))
    LARGE_PACKAGE_BATCH_FILES = max(1, min(1000, int(os.getenv("LARGE_PACKAGE_BATCH_FILES", "100"))))
    # The full payload stays in a sidecar. Package-wide structures retain only
    # a head/middle/tail semantic sketch, giving a strict 50k-file memory bound.
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
        min(200000, int(os.getenv("MAX_ZIP_CENTRAL_DIRECTORY_ENTRIES", "50000"))),
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
    JOB_EXPORT_TIMEOUT_SECONDS = max(300, int(os.getenv("JOB_EXPORT_TIMEOUT_SECONDS", "21600")))
    MAX_JOB_RESUME_ATTEMPTS = max(1, min(256, int(os.getenv("MAX_JOB_RESUME_ATTEMPTS", "64"))))


if os.name != "nt":
    os.umask(0o077)
Config.DATA_DIR.mkdir(parents=True, exist_ok=True)
Config.DOCUMENT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
Config.PARSE_TEMP_DIR.mkdir(parents=True, exist_ok=True)
Config.OUTPUT_DIR.mkdir(exist_ok=True)
Config.LOG_DIR.mkdir(exist_ok=True)
Config.MODELS_DIR.mkdir(exist_ok=True)
Config.DOCLING_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
Config.RAPIDOCR_MODEL_DIR.mkdir(parents=True, exist_ok=True)
if os.name != "nt":
    for _private_dir in (
        Config.DATA_DIR, Config.DOCUMENT_CACHE_DIR, Config.PARSE_TEMP_DIR,
        Config.LOG_DIR, Config.OUTPUT_DIR,
    ):
        try:
            _private_dir.chmod(0o700)
        except OSError:
            pass
