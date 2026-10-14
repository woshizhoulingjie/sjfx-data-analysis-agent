"""Local ClamAV receipts over staged, hash-bound bytes; no network or updater."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import threading

REVISION = "sjfx-clamav-receipt-20261014-v2"
_DB_CACHE = {}
_LOCK = threading.RLock()
_INCOMPLETE = re.compile(r"Exceeded|Encrypted|Limits|ERROR|WARNING|size limit|timed? out|Can't|cannot|not supported|skipp", re.I)


def configuration(policy=None):
    policy = policy or {}
    executable = policy.get("clamav_path") or os.getenv("SJFX_PROTECTION_CLAM") or shutil.which("clamscan")
    database = policy.get("clamav_database") or os.getenv("SJFX_PROTECTION_CLAM_DATABASE")
    certificates = (policy.get('clamav_certs') or os.getenv('SJFX_PROTECTION_CLAM_CERTS')
                    or os.getenv('CVD_CERTS_DIR'))
    if not database and executable:
        bundled = Path(executable).resolve().parent / "database"
        database = str(bundled) if bundled.is_dir() else None
    if not database and os.name != "nt" and Path("/var/lib/clamav").is_dir():
        database = "/var/lib/clamav"
    workspace = policy.get("av_work_dir") or os.getenv("SJFX_PROTECTION_SCAN_WORKDIR")
    if not workspace:
        try:
            if os.name == "nt":
                base = Path(os.environ["LOCALAPPDATA"]) if os.environ.get("LOCALAPPDATA") else Path.home() / "AppData/Local"
            else:
                base = Path(os.environ["XDG_STATE_HOME"]) if os.environ.get("XDG_STATE_HOME") else Path.home() / ".local/state"
            workspace = str(base / "SJFX/protection/scan-work")
        except RuntimeError:
            workspace = None
    return {"executable": str(Path(executable).resolve()) if executable else None,
            "database": str(Path(database).resolve()) if database else None,
            "certificates": str(Path(certificates).resolve()) if certificates else None,
            "workspace": str(Path(workspace).absolute()) if workspace else None, "adapter_revision": REVISION}


def certificates_identity(directory):
    """Fingerprint the explicit portable trust bundle; never disable verification."""
    if directory is None:
        return {'status': 'engine_default', 'fingerprint': 'engine_default'}
    root = Path(directory)
    if not root.is_dir() or root.is_symlink():
        raise ValueError('ClamAV certificate directory unavailable')
    entries = sorted(root.rglob('*'))
    if len(entries) > 10000 or any(path.is_symlink() for path in entries):
        raise ValueError('ClamAV certificate bundle exceeds limits or contains links')
    files = [path for path in entries if path.is_file()]
    if sum(path.stat().st_size for path in files) > 32 * 1024 * 1024:
        raise ValueError('ClamAV certificate bundle exceeds size budget')
    manifest = [(str(path.relative_to(root)), path.stat().st_size, path.stat().st_mtime_ns, path.stat().st_ctime_ns) for path in files]
    records = [{'name': str(path.relative_to(root)), 'size': path.stat().st_size,
                'sha256': _hash_file(path)} for path in files]
    current = sorted(path for path in root.rglob('*') if path.is_file())
    if files != current or manifest != [(str(path.relative_to(root)), path.stat().st_size, path.stat().st_mtime_ns, path.stat().st_ctime_ns) for path in current]:
        raise ValueError('ClamAV certificate bundle changed during fingerprinting')
    # Empty explicit directories can be valid for legacy embedded-signature
    # CVDs. Only ClamAV's verified full scan may establish completion; this
    # fingerprint itself makes no claim that any signing certificate exists.
    return {'status': 'configured' if files else 'configured_empty', 'fingerprint': hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest(), 'files': records}


def database_identity(directory):
    """Content digest includes every local signature/config file, not a declared revision."""
    root = Path(directory)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("ClamAV database directory unavailable")
    files = sorted(p for p in root.iterdir() if p.is_file() and p.suffix.lower() in {
        ".cvd", ".cld", ".cud", ".hdb", ".hsb", ".mdb", ".msb", ".ndb", ".ndu", ".ldb", ".ldu",
        ".sdb", ".zmd", ".rmd", ".db", ".yar", ".yara", ".ign", ".ign2", ".fp", ".sfp", ".cfg", ".crb",
        ".cdb", ".idb", ".cbc", ".ftm", ".wdb", ".pdb", ".gdb"})
    if not files or any(p.is_symlink() for p in files):
        raise ValueError("ClamAV signature files unavailable")
    manifest = [(p.name, p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_ctime_ns) for p in files]
    cache_key = (str(root), tuple(manifest))
    with _LOCK:
        if cache_key in _DB_CACHE:
            return dict(_DB_CACHE[cache_key])
    records = []
    for path in files:
        digest = hashlib.sha256()
        with path.open("rb") as incoming:
            header = incoming.read(512)
            digest.update(header)
            for block in iter(lambda: incoming.read(1024 * 1024), b""):
                digest.update(block)
        record = {"name": path.name, "sha256": digest.hexdigest(), "size": path.stat().st_size}
        if header.startswith(b"ClamAV-VDB:"):
            fields = header.decode("ascii", "replace").strip().split(":")
            if len(fields) > 5:
                record.update(version=fields[2], declared_signatures=fields[3], built=fields[1])
        records.append(record)
    if manifest != [(p.name, p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_ctime_ns) for p in files]:
        raise ValueError("ClamAV database changed during fingerprinting")
    result = {"fingerprint": hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest(), "files": records}
    with _LOCK:
        _DB_CACHE.clear()
        _DB_CACHE[cache_key] = result
    return dict(result)


def runtime_identity(policy=None):
    config = configuration(policy)
    result = dict(config)
    try:
        result["database_identity"] = database_identity(config["database"])
        result['certificate_identity'] = certificates_identity(config['certificates'])
        exe = Path(config["executable"])
        info = exe.stat()
        result["executable_stat"] = [info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        result["status"] = "available"
    except (OSError, TypeError, ValueError) as exc:
        result.update(status="unavailable", reason=str(exc))
    return result


def _private_root(path):
    root = Path(path).absolute()
    if any(p.is_symlink() or (os.name == "nt" and p.is_junction()) for p in (root, *root.parents)):
        raise ValueError("Scan workspace cannot contain links")
    if os.name == "nt":
        public = Path(os.environ.get("PUBLIC", "C:/Users/Public")).resolve()
        if root.resolve().is_relative_to(public):
            raise ValueError("Public scan workspace is forbidden")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt":
        # A protected DACL grants only this account, SYSTEM and administrators.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        token = wintypes.HANDLE()
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        descriptor = ctypes.c_void_p()
        sid_text = wintypes.LPWSTR()
        try:
            length = wintypes.DWORD()
            advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
            buffer = ctypes.create_string_buffer(length.value)
            if not advapi.GetTokenInformation(token, 1, buffer, length, ctypes.byref(length)):
                raise ctypes.WinError(ctypes.get_last_error())
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
                raise ctypes.WinError(ctypes.get_last_error())
            sddl = "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;" + sid_text.value + ")"
            if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if not advapi.SetFileSecurityW(str(root), 0x80000004, descriptor):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            if descriptor: kernel.LocalFree(descriptor)
            if sid_text: kernel.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
            kernel.CloseHandle(token)
    else:
        info = root.stat()
        if info.st_uid != os.getuid():
            raise ValueError("Scan workspace belongs to another account")
        os.chmod(root, 0o700)
        if stat.S_IMODE(root.stat().st_mode) != 0o700:
            raise ValueError("Scan workspace permissions unavailable")
    return root


@contextmanager
def _database_argument(database, cwd):
    # CreateProcess sets a Unicode cwd. Relative arguments remove a shared
    # Unicode ancestor (e.g. C:\Users\中文); arbitrary remaining Unicode is
    # mapped temporarily to an unused local DOS drive and removed exactly.
    try:
        argument = os.path.relpath(database, cwd)
    except ValueError:
        argument = str(database)  # Separate Windows volumes.
    if os.name != "nt" or argument.isascii():
        yield argument
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.DefineDosDeviceW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR]
    kernel.QueryDosDeviceW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    mutex = kernel.CreateMutexW(None, False, "Local\\SJFXClamAVDatabaseAlias")
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    acquired = False
    drive = None
    target = "\\??\\" + str(Path(database).resolve())
    try:
        acquired = kernel.WaitForSingleObject(mutex, 30000) in (0, 0x80)
        if not acquired:
            raise ValueError("Database path alias unavailable")
        buffer = ctypes.create_unicode_buffer(32768)
        for letter in reversed("DEFGHIJKLMNOPQRSTUVWXYZ"):
            candidate = letter + ":"
            if not kernel.QueryDosDeviceW(candidate, buffer, len(buffer)):
                if kernel.DefineDosDeviceW(0x1 | 0x8, candidate, target):
                    drive = candidate
                    break
        if not drive:
            raise ValueError("No private database path alias available")
        yield drive + "\\"
    finally:
        removed = not drive or kernel.DefineDosDeviceW(0x1 | 0x2 | 0x4 | 0x8, drive, target)
        if acquired: kernel.ReleaseMutex(mutex)
        kernel.CloseHandle(mutex)
        if not removed:
            raise ValueError('Temporary database path alias could not be removed')


def parse_receipt(exit_code, output, expected_files=1):
    result = {"status": "error", "detected": False, "detail": output, "exit_code": exit_code}
    def number(label):
        hits = re.findall(r"^" + re.escape(label) + r":\s*(\d+)\s*$", output, re.M)
        return int(hits[0]) if len(hits) == 1 else None
    known, scanned, infected = number("Known viruses"), number("Scanned files"), number("Infected files")
    versions = re.findall(r"^Engine version:\s*([\w.-]+)\s*$", output, re.M)
    hits = [line for line in output.splitlines() if line.endswith(" FOUND") and not _INCOMPLETE.search(line)]
    result.update(known_signatures=known, scanned_files=scanned, infected_files=infected, detections=hits,
                  detected=bool(hits), engine_version=versions[0] if len(versions) == 1 else None)
    full = (output.count("----------- SCAN SUMMARY -----------") == 1 and known is not None and known > 0
            and scanned == expected_files and infected is not None and len(versions) == 1
            and re.search(r"^End Date:\s*\S.+$", output, re.M) is not None)
    if _INCOMPLETE.search(output):
        result["status"] = "incomplete"
    elif full and exit_code == 0 and infected == 0 and not hits and re.search(r":\s*OK\s*$", output, re.M):
        result["status"] = "completed"
    elif full and exit_code == 1 and infected == expected_files and hits:
        result["status"] = "completed"
    return result


def receipt_complete(result):
    count = result.get('known_signatures')
    return (result.get('status') == 'completed' and result.get('real') is True
            and isinstance(count, int) and not isinstance(count, bool) and count > 0
            and result.get('scanned_files') == 1 and result.get('output_complete') is True
            and re.fullmatch(r'ClamAV\s+[0-9][\w.-]*/[0-9]+/[^\r\n]+', str(result.get('revision', ''))) is not None)


def scan(*, data=None, path=None, name="target.bin", policy=None):
    policy = policy or {}
    config = configuration(policy)
    result = {"status": "error", "real": True, "source": "local_clamav", "revision": "unknown",
              "adapter_revision": REVISION, "configuration": config, "detected": False}
    if not config["executable"]:
        return {**result, "status": "unavailable", "real": False, "detail": "Local ClamAV unavailable"}
    limit = policy.get("av_max_bytes", 2 * 1024 ** 3)
    timeout = policy.get("av_timeout", 300)
    try:
        before_db = database_identity(config["database"])
        before_certs = certificates_identity(config['certificates'])
        root = _private_root(config["workspace"])
        with tempfile.TemporaryDirectory(prefix="scan-", dir=root) as private:
            directory = Path(private)
            suffix = Path(name).suffix.lower()
            suffix = suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else ".bin"
            target = directory / ("target" + suffix)
            hasher = hashlib.sha256()
            copied = 0
            with target.open("xb") as outgoing:
                if data is not None:
                    if len(data) > limit:
                        return {**result, "status": "incomplete", "detail": "AV size budget exhausted"}
                    outgoing.write(data); hasher.update(data); copied = len(data)
                else:
                    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
                    with os.fdopen(os.open(path, flags), "rb") as incoming:
                        initial = os.fstat(incoming.fileno())
                        if not stat.S_ISREG(initial.st_mode):
                            raise ValueError("AV input must be a regular file")
                        for block in iter(lambda: incoming.read(1024 * 1024), b""):
                            copied += len(block)
                            if copied > limit:
                                return {**result, "status": "incomplete", "detail": "AV size budget exhausted"}
                            outgoing.write(block); hasher.update(block)
                        final = os.fstat(incoming.fileno())
                        current = os.stat(path, follow_symlinks=False)
                        key = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
                        if key(initial) != key(final) or key(final) != key(current):
                            raise ValueError("AV input changed during staging")
            if not copied:
                return {**result, "status": "incomplete", "detail": "Empty targets may be skipped by ClamAV"}
            result.update(input_sha256=hasher.hexdigest(), input_size=copied, original_name=name,
                          staged_name=target.name, database_identity=before_db, certificate_identity=before_certs)
            environment = dict(os.environ, TEMP=".", TMP=".", TMPDIR=".", LC_ALL="C", LANG="C")
            with _database_argument(config["database"], directory) as database, ExitStack() as aliases:
                common = [config["executable"], "--database=" + database]
                if config['certificates']:
                    certificate_path = aliases.enter_context(_database_argument(config['certificates'], directory))
                    common.append('--cvdcertsdir=' + certificate_path)
                    environment['CVD_CERTS_DIR'] = certificate_path
                with tempfile.TemporaryFile(dir=directory) as output:
                    version = subprocess.run(common + ["--version"], stdin=subprocess.DEVNULL, stdout=output,
                        stderr=subprocess.STDOUT, cwd=directory, env=environment, timeout=min(timeout, 10), check=False)
                    output.seek(0); raw_version = output.read(4001)
                revision = raw_version.decode("utf-8", "replace").strip()
                if version.returncode or len(raw_version) > 4000 or not re.fullmatch(r"ClamAV\s+[0-9][\w.-]*/[0-9]+/[^\r\n]+", revision):
                    return {**result, "detail": "Actual AV/database revision unavailable"}
                result["revision"] = revision
                with tempfile.TemporaryFile(dir=directory) as output:
                    process = subprocess.run(common + ["--stdout", "--alert-exceeds-max=yes", "--alert-encrypted=yes",
                        "--tempdir=.", "--max-filesize=" + str(limit), "--max-scansize=" + str(limit),
                        "--max-recursion=" + str(policy.get("max_depth", 16)), "--max-files=" + str(policy.get("max_members", 100000)), target.name],
                        stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, cwd=directory,
                        env=environment, timeout=timeout, check=False)
                    output.seek(0); raw = output.read(128001)
                text = raw[:128000].decode("utf-8", "replace").replace("\r\r\n", "\n").replace("\r\n", "\n")
                receipt = parse_receipt(process.returncode, text)
                result.update(receipt, output_sha256=hashlib.sha256(raw).hexdigest(), output_complete=len(raw) <= 128000)
                if len(raw) > 128000:
                    result.update(status="incomplete", detail=text + "\nOutput budget exhausted")
                if database_identity(config["database"])["fingerprint"] != before_db["fingerprint"]:
                    result.update(status="incomplete", detail=text + "\nDatabase changed during scan")
                if certificates_identity(config['certificates'])['fingerprint'] != before_certs['fingerprint']:
                    result.update(status='incomplete', detail=text + '\nCertificate bundle changed during scan')
                staged_hash = hashlib.sha256(target.read_bytes()).hexdigest() if copied <= 20 * 1024 * 1024 else _hash_file(target)
                if staged_hash != hasher.hexdigest():
                    result.update(status="incomplete", detail=text + "\nStaged version changed during scan")
                if result.get("engine_version") != revision.split()[1].split("/")[0]:
                    result.update(status="incomplete", detail=text + "\nEngine revision changed during scan")
    except subprocess.TimeoutExpired:
        result.update(status="incomplete", detail="Local antivirus timeout")
    except (OSError, ValueError, TypeError) as exc:
        result["detail"] = type(exc).__name__ + ": " + str(exc)
    return result


def _hash_file(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as incoming:
        for block in iter(lambda: incoming.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()
