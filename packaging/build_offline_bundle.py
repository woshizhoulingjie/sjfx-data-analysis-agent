#!/usr/bin/env python3
"""Build a self-contained SJFX offline bundle.

The builder deliberately uses only the Python standard library.  It copies a
prepared application tree, an already provisioned platform runtime, parser
tools, and model resources into a new staging directory, writes a file-level
SHA-256 manifest, and produces a ZIP (Windows) or tar.gz (Linux) archive.
It never copies instance state, credentials, caches, or databases.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
import zipfile


EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".ruff_cache",
    "outputs", "work", "data", "state", "logs", "test-temp", "tests", "tests_desktop",
    ".venv", "venv", "vendor_packages", ".cache", ".tmp", ".temp",
}
EXCLUDED_SUFFIXES = {
    ".pyc", ".pyo", ".log", ".db", ".sqlite", ".sqlite3", ".wal", ".shm",
    ".pem", ".key",
}
EXCLUDED_NAMES = {
    ".env", ".env.local", ".env.production", "credentials.json",
    "secrets.json", "token.json", "id_rsa", "id_ed25519",
}
EXCLUDED_PREFIXES = {
    "pip-build-", "pip-ephem-wheel-cache-", "pip-install-",
    "pip-pip-egg-info-", "pip-target-", "pip-unpack-", "pip-wheel-",
    "pip-download-", "pytest-of-", "topic-replay-", "torchinductor_",
}
PAYLOAD_EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".ruff_cache",
    "work", "test-temp", "tests", "logs",
}
APPLICATION_DIRS = {
    'services', 'sjfx_local_protection', 'sjfx_multiuser', 'sjfx_protection',
    'static', 'templates', 'docs', 'packaging', 'scripts',
}
APPLICATION_FILES = {
    'app.py', 'config.py', 'sjfxctl.py', 'worker.py', 'large_package_worker.py',
    'web_compat.py', 'README.md', 'pyproject.toml', 'LICENSE', 'LICENSE.md',
    'requirements.txt', 'requirements-app.txt', 'requirements-windows.txt',
    'requirements-vllm.txt', 'start_sjfx.bat', 'start_sjfx.sh',
}

# Audited Linux ELF programs and extensionless shell wrappers in the prepared
# resource closure. NTFS retains neither their execute bits nor their original
# POSIX modes. Keep this explicit so data and unreviewed resource files do not
# gain execute permissions when a Linux archive is built on Windows.
LINUX_EXECUTABLE_PATHS = {
    'runtime/bin/python3',
    'tools/7zip/7zz', 'tools/7zip/7zzs',
    'tools/bin/pdfinfo', 'tools/bin/pdftotext', 'tools/bin/qpdf',
    'tools/clamav/bin/clamscan', 'tools/clamav/bin/sigtool',
    'tools/clamav/native/bin/clamscan', 'tools/clamav/native/bin/sigtool',
    'tools/legacy-office/usr/bin/antiword', 'tools/legacy-office/usr/bin/catppt',
    'tools/mail-native/bin/readpst',
    'tools/tesseract/bin/tesseract',
    'tools/native/usr/bin/antiword', 'tools/native/usr/bin/catppt',
    'tools/native/usr/bin/pdfinfo', 'tools/native/usr/bin/pdftotext',
    'tools/native/usr/bin/qpdf', 'tools/native/usr/bin/readpst',
    'tools/native/usr/bin/tesseract',
}

# These imports are loaded by the launcher and its managed application roles.
# Checking them at build time prevents a syntactically valid but unusable
# runtime from being shipped as a production bundle.
RUNTIME_IMPORTS = (
    "fastapi", "jinja2", "uvicorn", "pydantic", "httpx", "werkzeug", "pypdf",
    "docx", "pptx", "openpyxl", "psutil", "cryptography", "torch", "transformers", "sentencepiece",
)


def _safe_relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _excluded(relative: Path) -> bool:
    parts = {part.lower() for part in relative.parts}
    name = relative.name.lower()
    if any(part in {item.lower() for item in EXCLUDED_DIRS} for part in parts):
        return True
    if any(part.startswith(prefix) for part in parts for prefix in EXCLUDED_PREFIXES):
        return True
    if name in {item.lower() for item in EXCLUDED_NAMES} or name.startswith(".env."):
        return True
    return relative.suffix.lower() in EXCLUDED_SUFFIXES


def _excluded_payload(relative: Path) -> bool:
    """Exclude sensitive/cache files without dropping resource directories.

    Prepared runtimes and model/tool roots can legitimately contain folders
    named ``data`` or ``state``. Those names mean instance state only in the
    application source tree; resource roots still need their contents.
    """
    parts = {part.lower() for part in relative.parts}
    name = relative.name.lower()
    if 'licenses' not in parts and any(part in PAYLOAD_EXCLUDED_DIRS for part in parts):
        return True
    if any(part.startswith(prefix) for part in parts for prefix in EXCLUDED_PREFIXES):
        return True
    if name in {item.lower() for item in EXCLUDED_NAMES} or name.startswith(".env."):
        return True
    # Certifi's public CA bundle is required by HTTP clients and contains no
    # private key. Preserve it even though arbitrary PEM inputs are excluded.
    if name == "cacert.pem" and "certifi" in parts:
        return False
    return relative.suffix.lower() in EXCLUDED_SUFFIXES


def _is_source_link(path: Path) -> bool:
    # NTFS junctions are directory links but Path.is_symlink() is false.
    # st_reparse_tag also works on Python versions before is_junction().
    return path.is_symlink() or (
        os.name == "nt" and getattr(path.lstat(), "st_reparse_tag", 0)
        == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1)
    )


def _source_entries(source: Path, exclude: bool | str):
    """Walk regular directories without entering links or excluded trees."""
    pending = [source]
    while pending:
        directory = pending.pop()
        for item in directory.iterdir():
            relative = item.relative_to(source)
            if exclude and (_excluded(relative) if exclude is True else _excluded_payload(relative)):
                continue
            yield item
            if item.is_dir() and not _is_source_link(item):
                pending.append(item)


def _copy_tree(source: Path, destination: Path, *, exclude: bool | str = False,
               source_root: Path | None = None, visited: set[Path] | None = None) -> int:
    """Copy a tree while following resource symlinks and preserving modes."""
    source = source.resolve(strict=True)
    source_root = source if source_root is None else source_root.resolve(strict=True)
    visited = set() if visited is None else visited
    if source.is_dir():
        if source in visited:
            raise RuntimeError(f"cyclic or duplicate source symlink: {source}")
        visited.add(source)
    count = 0
    directory_metadata = []
    if source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=True)
        return 1
    if not source.is_dir():
        raise FileNotFoundError(str(source))
    for item in _source_entries(source, exclude):
        relative = item.relative_to(source)
        target = destination / relative
        if _is_source_link(item):
            resolved = item.resolve(strict=True)
            try:
                resolved.relative_to(source_root)
            except ValueError as exc:
                raise RuntimeError(f"input symlink escapes its source tree: {item}") from exc
            if resolved.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                count += _copy_tree(resolved, target, exclude=exclude,
                                    source_root=source_root, visited=visited)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(resolved, target, follow_symlinks=True)
                count += 1
            continue
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            directory_metadata.append((item, target))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        # copy2 follows symlinks.  A bundle must not contain links back to a
        # build cache or a server path.
        shutil.copy2(item, target, follow_symlinks=True)
        count += 1
    # Restore directory modes after populating them so read-only prepared
    # inputs can be copied by an ordinary, non-root build user.
    for item, target in reversed(directory_metadata):
        try:
            shutil.copystat(item, target, follow_symlinks=False)
        except OSError:
            pass
    return count


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _target_mode(relative: Path, actual_mode: int, target: str) -> int:
    """Return portable permissions for a file in the requested bundle."""
    mode = stat.S_IMODE(actual_mode) & 0o777
    if target == "linux-x64":
        # Windows does not retain POSIX execute bits. These files must still
        # be runnable after a Linux tarball is built on a Windows host.
        if relative.as_posix().lower() in LINUX_EXECUTABLE_PATHS or relative.suffix.lower() == ".sh":
            return 0o755
    return mode


def _manifest(root: Path, *, target: str, source: Path, runtime: Path,
              models: Path, tools: Path, synthetic_override: bool = False) -> dict:
    files = []
    for item in sorted(root.rglob("*")):
        if not item.is_file():
            continue
        relative = _safe_relative(item, root)
        if relative == "manifest.json":
            continue
        files.append({
            "path": relative,
            "bytes": item.stat().st_size,
            "sha256": _sha256(item),
            "mode": _target_mode(Path(relative), item.stat().st_mode, target),
        })
    return {
        "schema": 1,
        "product": "sjfx",
        "target": target,
        "created_at": datetime.now(timezone.utc).isoformat(),
        # Do not put build-machine paths (which may contain a user profile or
        # server mount) into a customer-delivered manifest.
        "source_root": "app",
        "inputs": {
            "runtime": "runtime",
            "models": "models",
            "tools": "tools",
        },
        "validation": {
            "runtime": "synthetic_override" if synthetic_override else "binary_and_dependency_preflight",
            "resources": "synthetic_override" if synthetic_override else "non_empty_inputs",
        },
        "files": files,
        "file_count": len(files),
        "total_bytes": sum(row["bytes"] for row in files),
    }


def _runtime_executable(runtime: Path, target: str, *, allow_synthetic: bool = False) -> Path:
    candidate = runtime / ("python.exe" if target == "windows-x64" else "bin/python3")
    if not candidate.is_file() or _is_source_link(candidate):
        raise RuntimeError(f"runtime is missing the expected interpreter: {candidate}")
    if allow_synthetic:
        return candidate

    size = candidate.stat().st_size
    if size < 64 * 1024:
        raise RuntimeError(
            f"runtime interpreter is implausibly small ({size} bytes): {candidate}; "
            "use a complete target runtime or explicitly pass --allow-synthetic for fixture bundles"
        )
    with candidate.open("rb") as stream:
        header = stream.read(4096)
    if target == "windows-x64":
        if len(header) < 0x40 or header[:2] != b"MZ":
            raise RuntimeError(f"runtime interpreter is not an x64 PE executable: {candidate}")
        pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
        if pe_offset + 6 > len(header) or header[pe_offset:pe_offset + 4] != b"PE\0\0":
            raise RuntimeError(f"runtime interpreter has an invalid PE header: {candidate}")
        machine = struct.unpack_from("<H", header, pe_offset + 4)[0]
        if machine != 0x8664:
            raise RuntimeError(
                f"runtime interpreter is not x64 (PE machine 0x{machine:04x}): {candidate}"
            )
        has_stdlib = (runtime / "Lib" / "encodings" / "__init__.py").is_file()
        if not has_stdlib:
            for stdlib_zip in runtime.glob("python*.zip"):
                try:
                    with zipfile.ZipFile(stdlib_zip) as archive:
                        has_stdlib = "encodings/__init__.py" in archive.namelist()
                except (OSError, zipfile.BadZipFile):
                    continue
                if has_stdlib:
                    break
        site_packages = runtime / "Lib" / "site-packages"
    else:
        if len(header) < 20 or header[:4] != b"\x7fELF" or header[4] != 2:
            raise RuntimeError(f"runtime interpreter is not a 64-bit ELF executable: {candidate}")
        endian = ">" if header[5] == 2 else "<" if header[5] == 1 else None
        machine = struct.unpack_from(f"{endian}H", header, 18)[0] if endian else None
        if machine != 62:
            rendered = "unknown" if machine is None else f"0x{machine:04x}"
            raise RuntimeError(f"runtime interpreter is not x86_64 (ELF machine {rendered}): {candidate}")
        lib_root = runtime / "lib"
        has_stdlib = any((item / "encodings" / "__init__.py").is_file()
                         for item in lib_root.glob("python*"))
        site_packages = next(
            (item / "site-packages" for item in lib_root.glob("python*")
             if (item / "site-packages").is_dir()),
            lib_root / "site-packages",
        )
    if not has_stdlib:
        raise RuntimeError(
            f"runtime is missing its bundled Python standard library: {runtime}; "
            "include Lib/python*.zip or lib/python*/ before building"
        )
    ignored_packages = {"pip", "setuptools", "wheel"}
    has_application_packages = site_packages.is_dir() and any(
        item.is_file() and item.name.lower() not in {"readme.txt", "easy-install.pth"}
        or item.is_dir() and item.name.split("-")[0].lower() not in ignored_packages
        and not item.name.lower().endswith((".dist-info", ".egg-info"))
        for item in site_packages.iterdir()
    )
    if not has_application_packages:
        raise RuntimeError(
            f"runtime has no installed application packages under {site_packages}; "
            "prepare the complete site-packages closure before building"
        )

    native_target = (target == "windows-x64" and os.name == "nt") or (
        target == "linux-x64" and sys.platform.startswith("linux")
    )
    if native_target:
        try:
            subprocess.run(
                [str(candidate), "-I", "-X", "utf8", "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=10,
                text=True,
                encoding="utf-8",
                errors="backslashreplace",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"bundled runtime failed its --version probe: {candidate}: {exc}") from exc
    return candidate


def _runtime_site_packages(runtime: Path, target: str) -> Path:
    if target == "windows-x64":
        return runtime / "Lib" / "site-packages"
    lib_root = runtime / "lib"
    return next(
        (item / "site-packages" for item in lib_root.glob("python*")
         if (item / "site-packages").is_dir()),
        lib_root / "site-packages",
    )


def _runtime_dependency_preflight(runtime: Path, target: str, candidate: Path,
                                  *, allow_synthetic: bool = False) -> None:
    """Require the imports needed by the launcher/application closure.

    A cross-compiled target cannot execute its interpreter on the build host,
    so it receives a static top-level package check. Native targets also run
    the imports through the bundled interpreter to catch broken native wheels
    and incomplete transitive dependencies.
    """
    if allow_synthetic:
        return
    site_packages = _runtime_site_packages(runtime, target)
    missing = []
    for module in RUNTIME_IMPORTS:
        top_level = module.split(".", 1)[0]
        package_dir = site_packages / top_level
        module_file = site_packages / (top_level + ".py")
        if not package_dir.is_dir() and not module_file.is_file():
            missing.append(module)
    if missing:
        raise RuntimeError(
            "runtime is missing required application imports under {}: {}; "
            "install the complete requirements-app closure before building".format(
                site_packages, ", ".join(missing))
        )
    native_target = (target == "windows-x64" and os.name == "nt") or (
        target == "linux-x64" and sys.platform.startswith("linux")
    )
    if not native_target:
        return
    script = (
        "import importlib\n"
        "required = " + repr(RUNTIME_IMPORTS) + "\n"
        "missing = []\n"
        "for name in required:\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "    except Exception as exc:\n"
        "        missing.append(name + ': ' + type(exc).__name__ + ': ' + str(exc))\n"
        "if missing:\n"
        "    raise SystemExit('missing or unloadable application imports: ' + '; '.join(missing))\n"
    )
    try:
        subprocess.run(
            [str(candidate), "-I", "-B", "-X", "utf8", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=120,
            text=True,
            encoding="utf-8",
            errors="backslashreplace",
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(
            f"bundled runtime failed application import preflight: {candidate}{suffix}"
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            f"bundled runtime failed application import preflight: {candidate}: {exc}"
        ) from exc


def _resource_bytes(root: Path) -> int:
    # Resource roots can contain NTFS junctions; avoid following directory
    # links before the copy stage performs its containment/cycle checks.
    return sum(item.stat().st_size for item in _source_entries(root, "payload")
               if item.is_file())


def _validate_resources(models: Path, tools: Path, *, target: str, allow_synthetic: bool = False) -> None:
    if allow_synthetic:
        return
    # A production resource root must contain enough payload to be more than a
    # marker or placeholder. The thresholds are deliberately conservative; the
    # actual platform acceptance still checks each required model/tool.
    minimums = ((models, "models", 1024 * 1024), (tools, "tools", 64 * 1024))
    for root, label, minimum in minimums:
        total = _resource_bytes(root)
        if total < minimum:
            raise RuntimeError(
                f"{label} resource tree is only {total} bytes; refusing a placeholder bundle. "
                "Provide the complete resource closure or explicitly pass --allow-synthetic for fixtures"
            )
    required = [models/'multilingual-e5-small'/name for name in (
        'config.json', 'model.safetensors', 'tokenizer.json', 'tokenizer_config.json',
        'sentencepiece.bpe.model', 'special_tokens_map.json', 'SOURCE.json')]
    required += [models/'protection/clamav'/name for name in ('main.cvd', 'daily.cvd', 'bytecode.cvd')]
    required += [tools/'clamav'/('clamscan.exe' if target == 'windows-x64' else 'bin/clamscan')]
    if target == 'windows-x64':
        required += [tools/'desktop-helper/sjfx-assistant.exe']
    missing = [str(path.relative_to(models if models in path.parents else tools)) for path in required
               if not path.is_file() or not path.stat().st_size]
    if missing:
        raise RuntimeError('Complete offline embedding/protection resources required: '+', '.join(missing))
    if (models/'multilingual-e5-small/model.safetensors').stat().st_size < 400*1024*1024:
        raise RuntimeError('Bundled E5 model payload is incomplete')


def _write_templates(root: Path, target: str, packaging_dir: Path) -> None:
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    env = """# SJFX offline instance configuration. Generated by the installer.\n# Keep this file private; it may contain the local API token.\nSJFX_STATE_DIR=__DATA_DIR__/state\nSJFX_OUTPUT_DIR=__DATA_DIR__/outputs\nSJFX_MODELS_DIR=__INSTALL_DIR__/models\nSJFX_TOOLS_DIR=__INSTALL_DIR__/tools\nSJFX_PARSE_TEMP_DIR=__DATA_DIR__/parse_temp\nHF_HOME=__DATA_DIR__/cache/huggingface\nSCAN_ALLOWED_ROOTS=__DATA_DIR__/outputs/_uploads\nHOST=127.0.0.1\nPORT=18000\nAUTH_REQUIRED=1\nLLM_BACKEND=__LLM_BACKEND__\nENABLE_VLLM=__ENABLE_VLLM__\nENABLE_SHARED_OLLAMA=__ENABLE_OLLAMA__\nENABLE_OPENAI_COMPATIBLE=__ENABLE_OPENAI__\nHF_HUB_OFFLINE=1\nTRANSFORMERS_OFFLINE=1\nTRANSLATION_PROVIDER=offline_quickmt\nTRANSLATION_FILE_BACKEND=offline_quickmt\nTRANSLATION_QUICKMT_MODEL_DIR=__INSTALL_DIR__/models/quickmt\n"""
    env += """SJFX_OFFLINE_INSTALLATION=1
SJFX_MANAGED_EMBEDDING=1
SJFX_MANAGED_EMBED_PORT=18083
SJFX_MANAGED_EMBED_MODEL_PATH=__INSTALL_DIR__/models/multilingual-e5-small
SJFX_EMBED_BACKEND=openai_compatible
SJFX_EMBED_BASE_URL=http://127.0.0.1:18083/v1
SJFX_EMBED_MODEL=multilingual-e5-small
SJFX_EMBED_DIMENSION=384
SJFX_EMBED_ENCODING=e5
SJFX_EMBED_MODEL_PATH=__INSTALL_DIR__/models/multilingual-e5-small
SJFX_EMBED_TOKENIZER_PATH=__INSTALL_DIR__/models/multilingual-e5-small
VLLM_EMBED_BASE_URL=http://127.0.0.1:18083/v1
VLLM_EMBED_MODEL=multilingual-e5-small
VLLM_EMBED_MAX_BATCH_SIZE=8
VLLM_EMBED_TIMEOUT_SECONDS=60
MAIL_EMBED_BASE_URL=http://127.0.0.1:18083/v1
MAIL_EMBED_MODEL=multilingual-e5-small
SJFX_LP_EMBED_BASE_URL=http://127.0.0.1:18083/v1
SJFX_LOCAL_AGENT_ENDPOINT=http://127.0.0.1:18767
SJFX_DESKTOP_ACCESS_AUDIT=1
SJFX_PROTECTION_CLAM_DATABASE=__DATA_DIR__/models/protection/clamav
SJFX_PROTECTION_SCAN_WORKDIR=__DATA_DIR__/state/desktop_protection/scan-work
OPENAI_COMPATIBLE_BASE_URL=http://127.0.0.1:8001/v1
OPENAI_COMPATIBLE_MODEL=local-model
"""
    if target == 'windows-x64':
        env += 'SJFX_DESKTOP_HELPER_BINARY=__INSTALL_DIR__/tools/desktop-helper/sjfx-assistant.exe\n'
        env += 'SJFX_PROTECTION_CLAM=__INSTALL_DIR__/tools/clamav/clamscan.exe\n'
    else:
        env += 'SJFX_PROTECTION_CLAM=__INSTALL_DIR__/tools/clamav/bin/clamscan\n'
    # A fresh offline installation starts all bundled local services. Generation
    # remains selectable and is enabled after its external engine is configured.
    backend = 'openai_compatible'
    values = env.replace("__LLM_BACKEND__", backend)
    values = values.replace("__ENABLE_VLLM__", "1" if backend == "vllm" else "0")
    values = values.replace("__ENABLE_OLLAMA__", "1" if backend == "ollama" else "0")
    values = values.replace("__ENABLE_OPENAI__", "0")
    (config / "sjfx.env.template").write_text(values, encoding="utf-8")
    install_guide = packaging_dir.parent / "docs" / "offline-installation.md"
    (root / "INSTALL.md").write_text(install_guide.read_text(encoding="utf-8") if install_guide.is_file() else (
        "SJFX offline bundle\n\n"
        "Run install_windows.ps1 on Windows or install_linux.sh on Linux from the extracted bundle.\n"
        "The installer keeps application files and read-only resources under the install\n"
        "directory, and puts the database, logs, cache, uploads, and configuration under\n"
        "the user data directory. It does not download dependencies or model files.\n"
        "Generation backends (vLLM, Ollama, or an OpenAI-compatible local service) are\n"
        "configured as local services and are not downloaded or started by this installer.\n"),
        encoding="utf-8",
    )
    if target == "windows-x64":
        (root / "start_sjfx.bat").write_text(
            "@echo off\r\n"
            "setlocal\r\n"
            "cd /d \"%~dp0\"\r\n"
            "set PYTHONUTF8=1\r\n"
            "set PYTHONDONTWRITEBYTECODE=1\r\n"
            "if exist \"runtime\\python.exe\" (\r\n"
            "  \"runtime\\python.exe\" -B -X utf8 \"app\\sjfxctl.py\" start --open-browser %*\r\n"
            ") else (\r\n"
            "  echo Missing bundled runtime\\python.exe 1>&2\r\n"
            "  exit /b 1\r\n"
            ")\r\n",
            encoding="utf-8",
        )
    else:
        launcher = (
            "#!/bin/sh\nset -eu\n"
            "sjfx_root=$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd)\n"
            "cd \"$sjfx_root\"\n"
            "export PYTHONUTF8=1\n"
            "export PYTHONDONTWRITEBYTECODE=1\n"
            "if [ -x \"$sjfx_root/runtime/bin/python3\" ]; then\n"
            "  exec \"$sjfx_root/runtime/bin/python3\" -B -X utf8 \"$sjfx_root/app/sjfxctl.py\" start \"$@\"\n"
            "fi\n"
            "echo 'Missing bundled runtime/bin/python3' >&2\nexit 1\n"
        )
        (root / "start_sjfx.sh").write_text(launcher, encoding="utf-8")
        (root / "start_sjfx.sh").chmod(0o755)
    for name in (
        "install_windows.ps1", "install_linux.sh",
        "upgrade_windows.ps1", "upgrade_linux.sh",
        "uninstall_windows.ps1", "uninstall_linux.sh",
    ):
        template = packaging_dir / name
        if template.is_file():
            destination = root / name
            shutil.copy2(template, destination)
            if name.endswith(".sh"):
                destination.chmod(destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _archive(root: Path, output: Path, target: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    # Own the new output exclusively. A second build in the same second must
    # never replace an earlier archive, including an incomplete prior build.
    stream = output.open("xb")
    try:
        with stream:
            _write_archive(root, stream, target)
    except BaseException:
        # Only this invocation created this file. Preserve all older outputs.
        output.unlink(missing_ok=True)
        raise


def _write_archive(root: Path, stream, target: str) -> None:
    if target == "windows-x64":
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for item in sorted(root.rglob("*")):
                if item.is_file():
                    # ZIP dates before 1980 are invalid.  Some prepared
                    # runtimes contain epoch-zero files, so normalize only
                    # the archive metadata while retaining file contents.
                    mtime = max(item.stat().st_mtime, 315532800)
                    info = zipfile.ZipInfo(
                        root.name + "/" + item.relative_to(root).as_posix(),
                        time.gmtime(mtime)[:6],
                    )
                    info.compress_type = zipfile.ZIP_DEFLATED
                    relative = item.relative_to(root)
                    info.external_attr = _target_mode(relative, item.stat().st_mode, target) << 16
                    with item.open("rb") as source, archive.open(info, "w") as target_stream:
                        shutil.copyfileobj(source, target_stream, length=1024 * 1024)
    else:
        with tarfile.open(fileobj=stream, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
            archive.add(root, arcname=root.name, recursive=True,
                        filter=lambda info: _tar_filter(info, root, target))


def _tar_filter(info: tarfile.TarInfo, root: Path, target: str) -> tarfile.TarInfo:
    # Never preserve links from an input cache. The copy stage resolves them.
    # Keep only portable permission bits; setuid/setgid/sticky bits from a
    # prepared input tree must not become privileged bits in a customer bundle.
    relative = Path(info.name).relative_to(root.name)
    info.mode = _target_mode(relative, info.mode, target)
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def _copy_application(source: Path, destination: Path) -> None:
    """Package reviewed application locations, excluding workspace scratch roots."""
    destination.mkdir()
    for item in sorted(source.iterdir()):
        if item.name in APPLICATION_DIRS and item.is_dir():
            if _is_source_link(item) or any(_is_source_link(child) for child in _source_entries(item, True)):
                raise RuntimeError(f'application source directory must not contain links: {item.name}')
            _copy_tree(item, destination / item.name, exclude=True, source_root=source)
        elif item.name in APPLICATION_FILES and item.is_file():
            if _is_source_link(item):
                raise RuntimeError(f'application source must not be a link: {item.name}')
            shutil.copy2(item, destination / item.name)


def build(args: argparse.Namespace) -> Path:
    source = Path(args.source).expanduser().resolve()
    runtime = Path(args.runtime).expanduser().resolve()
    models = Path(args.models).expanduser().resolve()
    tools = Path(args.tools).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    target = args.target
    allow_synthetic = bool(getattr(args, "allow_synthetic", False))
    if not source.is_dir() or not runtime.is_dir() or not models.is_dir() or not tools.is_dir():
        raise SystemExit("source, runtime, models, and tools must all be existing directories")
    if any(output == item or item in output.parents or output in item.parents
           for item in (runtime, models, tools)):
        raise SystemExit("output directory must not overlap source, runtime, models, or tools")
    if output == source or source in output.parents:
        relative = output.relative_to(source) if source in output.parents else Path()
        if not relative.parts or relative.parts[0].lower() not in {
                item.lower() for item in EXCLUDED_DIRS}:
            raise SystemExit(
                "output directory must be outside source or under an excluded source directory"
            )
    required_templates = (
        "verify_bundle.py", "install_windows.ps1", "install_linux.sh",
        "upgrade_windows.ps1", "upgrade_linux.sh",
        "uninstall_windows.ps1", "uninstall_linux.sh",
    )
    packaging_dir = source / "packaging"
    missing_templates = [name for name in required_templates
                         if not (packaging_dir / name).is_file()]
    if missing_templates:
        raise SystemExit("source packaging is missing: " + ", ".join(missing_templates))
    runtime_executable = _runtime_executable(
        runtime, target, allow_synthetic=allow_synthetic
    )
    _runtime_dependency_preflight(
        runtime, target, runtime_executable, allow_synthetic=allow_synthetic
    )
    if not any(item.is_file() for item in models.rglob("*")):
        raise SystemExit("models directory is empty; refusing to build a non-self-contained bundle")
    if not any(item.is_file() for item in tools.rglob("*")):
        raise SystemExit("tools directory is empty; refusing to build a non-self-contained bundle")
    _validate_resources(models, tools, target=target, allow_synthetic=allow_synthetic)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output.mkdir(parents=True, exist_ok=True)
    temporary_stage = (tempfile.TemporaryDirectory(prefix=f'.build-{target}-{stamp}-', dir=output)
                       if getattr(args, 'archive_only', False) else None)
    root = (Path(temporary_stage.name) if temporary_stage else output) / f"sjfx-offline-{target}-{stamp}"
    if root.exists():
        raise FileExistsError(str(root))
    root.mkdir(parents=True)
    try:
        _copy_application(source, root / "app")
        # Runtime and resource roots are already isolated inputs. Their
        # internal names (for example ``data`` in a package or model layout)
        # are payload, not instance state, so do not apply the application
        # source exclusion list to them.
        _copy_tree(runtime, root / "runtime", exclude="payload")
        _copy_tree(models, root / "models", exclude="payload")
        _copy_tree(tools, root / "tools", exclude="payload")
        # Probe the copied runtime as well: exclusion rules and relocation
        # must not turn a valid input into an unusable delivered payload.
        staged_executable = _runtime_executable(
            root / "runtime", target, allow_synthetic=allow_synthetic
        )
        _runtime_dependency_preflight(
            root / "runtime", target, staged_executable,
            allow_synthetic=allow_synthetic,
        )
        for label in ("models", "tools"):
            if not any(item.is_file() for item in (root / label).rglob("*")):
                raise RuntimeError(f"{label} payload is empty after exclusions")
        _validate_resources(root / "models", root / "tools", target=target,
                            allow_synthetic=allow_synthetic)
        _write_templates(root, target, packaging_dir)
        verifier = root / "app" / "packaging" / "verify_bundle.py"
        if not verifier.is_file():
            raise RuntimeError("bundle is missing app/packaging/verify_bundle.py")
        manifest = _manifest(root, target=target, source=source, runtime=runtime,
                             models=models, tools=tools,
                             synthetic_override=allow_synthetic)
        (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                              encoding="utf-8")
        archive = output / (f"sjfx-offline-{target}-{stamp}.zip" if target == "windows-x64"
                            else f"sjfx-offline-{target}-{stamp}.tar.gz")
        _archive(root, archive, target)
        return archive
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    finally:
        # This owner created a new temporary directory for this invocation;
        # previous packages, extracted payloads and evidence are never removed.
        if temporary_stage is not None:
            temporary_stage.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="validated application source tree")
    parser.add_argument("--runtime", required=True, help="prepared CPython runtime directory")
    parser.add_argument("--models", required=True, help="complete offline models/OCR resource root")
    parser.add_argument('--archive-only', action='store_true',
                        help='Use a fresh temporary build stage and retain only the complete archive')
    parser.add_argument("--tools", required=True, help="complete native parser/OCR tool root")
    parser.add_argument("--output", required=True, help="new timestamped output directory parent")
    parser.add_argument("--target", choices=("windows-x64", "linux-x64"), required=True)
    parser.add_argument(
        "--allow-synthetic",
        action="store_true",
        help="allow placeholder runtime/resources for regression fixtures; never use for delivery",
    )
    args = parser.parse_args()
    print(build(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
