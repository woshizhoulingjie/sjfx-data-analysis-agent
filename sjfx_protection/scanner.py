"""Bounded, passive content inspection. No links are followed or macros executed."""
from __future__ import annotations

import email
import email.policy
import hashlib
import json
import os
import shutil
import subprocess
import socket
import struct
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .threat_rules import inspect_payload


class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.current = [], None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() == "a":
            self.current = [attrs.get("href", ""), ""]

    def handle_data(self, data):
        if self.current is not None:
            self.current[1] += data[:4096]

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self.current is not None:
            self.links.append(self.current)
            self.current = None


def stable_hash(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        import stat
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("不是普通文件")
        hasher = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
        after = os.fstat(stream.fileno())
        current = os.stat(path, follow_symlinks=False)
        key = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        # Windows fstat/stat expose different legacy ctime semantics on some Python builds.
        path_key = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
        if key(before) != key(after) or path_key(after) != path_key(current) or (
                os.name != 'nt' and after.st_ctime_ns != current.st_ctime_ns):
            raise ValueError("读取期间文件发生变化，请重新登记")
    return {"sha256": hasher.hexdigest(), "size": after.st_size, "mtime_ns": after.st_mtime_ns,
            "identity": "%s:%s" % (after.st_dev, after.st_ino)}


def _run(args, timeout=45):
    try:
        with __import__('tempfile').TemporaryFile() as out:
            result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                    timeout=timeout, check=False)
            out.seek(0)
            text = out.read(128000).decode("utf-8", "replace")
        return result.returncode, text
    except subprocess.TimeoutExpired:
        return -1, "扫描超时"
    except OSError:
        return -1, "扫描器无法运行"


def _clamd_scan(path, address):
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
            client.settimeout(45)
            client.connect(address)
            client.sendall(b'zINSTREAM\0')
            with open(path,'rb') as source:
                for block in iter(lambda:source.read(65536),b''):
                    client.sendall(struct.pack('!I',len(block))+block)
            client.sendall(b'\0\0\0\0')
            raw=b''
            while len(raw)<128000:
                block=client.recv(4096)
                if not block:break
                raw+=block
                if b'\0' in raw:break
            output=raw.rstrip(b'\0\r\n').decode('utf-8','replace')
            return (1 if output.endswith(' FOUND') else 0 if output.endswith(' OK') else -1),output
    except (OSError,ValueError):
        return -1,'本地病毒扫描服务不可用'


def bounded_read(path, max_bytes=20*1024*1024, expected_sha256=None):
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_BINARY',0)|getattr(os,'O_NONBLOCK',0))
    with os.fdopen(fd,'rb') as stream:
        import stat
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('仅支持普通文件')
        raw=stream.read(max_bytes+1)
    if len(raw)>max_bytes:
        raise ValueError('文件超过安全预览大小限制')
    if expected_sha256 and hashlib.sha256(raw).hexdigest()!=expected_sha256:
        raise ValueError('文件版本已变化，请重新建立清单')
    return raw


def inspect_file(path, *, max_bytes=20 * 1024 * 1024, clam=None, yara=None, yara_rules=None, rspamd=None):
    path = Path(path)
    findings, engines = [], {}
    risk = "unchecked"
    size = path.stat().st_size
    if size > max_bytes:
        return {"risk": "unchecked", "findings": [{"rule": "size_limit", "detail": "超过静态检查大小限制"}],
                "engines": {"static": "skipped_size_limit"}}
    raw = bounded_read(path, max_bytes)
    findings.extend(inspect_payload(raw, path.name))
    engines["static"] = "completed"
    if path.suffix.lower() == '.msg':
        findings.append({'rule': 'msg_requires_mail_engine', 'severity': 'review',
                         'detail': 'MSG 容器不能按 EML 完整检查，需专用引擎复核'})
    if path.suffix.lower() == ".eml":
        message = email.message_from_bytes(raw, policy=email.policy.default)
        mail_budget = {'bytes': 8 * 1024 * 1024, 'members': 200}
        for index, part in enumerate(message.walk()):
            if index > 200:
                engines["static"] = "partial_part_limit"
                break
            filename = part.get_filename() or ""
            if not part.is_multipart():
                findings.extend(inspect_payload(part.get_payload(decode=True) or b'', filename,
                    content_type=part.get_content_type(), budget=mail_budget))
            if Path(filename).suffix.lower() in {".exe", ".js", ".vbs", ".lnk", ".hta", ".scr", ".bat", ".cmd", ".ps1"}:
                findings.append({"rule": "executable_attachment", "severity": "review", "detail": filename[:500]})
            if part.get_content_type() != "text/html":
                continue
            data = part.get_payload(decode=True) or b""
            parser = Links()
            try:
                text = data[:2 * 1024 * 1024].decode(part.get_content_charset() or 'utf-8', 'replace')
            except LookupError:
                text = data[:2 * 1024 * 1024].decode('utf-8', 'replace')
                engines['static'] = 'partial_charset'
            parser.feed(text)
            for target, shown in parser.links[:500]:
                try:
                    actual, visible = urlsplit(target.strip()), urlsplit(shown.strip())
                    if visible.scheme in {"http", "https"} and actual.hostname and visible.hostname:
                        if actual.hostname.lower() != visible.hostname.lower():
                            findings.append({"rule": "link_target_mismatch", "severity": "review",
                                             "detail": {"display": shown[:1000], "target": target[:1000]}})
                    if actual.scheme.lower() in {"javascript", "file", "data"}:
                        findings.append({"rule": "active_link", "severity": "review", "detail": target[:1000]})
                except ValueError:
                    findings.append({"rule": "malformed_link", "severity": "review"})
        if rspamd:
            parsed = urlsplit(rspamd)
            if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.scheme not in {"http", "https"}:
                raise ValueError("Rspamd 只允许本机扫描地址")
            try:
                with urlopen(Request(rspamd.rstrip('/') + '/checkv2', data=raw,
                                     headers={"Content-Type": "message/rfc822"}), timeout=30) as resp:
                    result = json.loads(resp.read(1024 * 1024))
                engines["rspamd"] = "completed"
                for name, value in (result.get("symbols") or {}).items():
                    if value.get("score", 0) > 0:
                        findings.append({"rule": "rspamd:" + name, "severity": "review",
                                         "detail": value.get("options", []), "score": value.get("score")})
            except Exception:
                engines["rspamd"] = "error"
    clamd=os.getenv('SJFX_PROTECTION_CLAMD_SOCKET') if clam is None else None
    clam = clam or os.getenv('SJFX_PROTECTION_CLAM') or shutil.which("clamscan")
    if clam or clamd:
        status, output = _clamd_scan(path,clamd) if clamd else _run([clam, "--no-summary", "--alert-exceeds-max=yes", "--alert-encrypted=yes", str(path)])
        engines["clamav"] = "completed" if status in (0, 1) else "error"
        if status == 1:
            for line in output.splitlines():
                if line.endswith(" FOUND"):
                    incomplete = any(s in line for s in ("Exceeded", "Encrypted", "Limits"))
                    findings.append({"rule": "clamav", "severity": "review" if incomplete else "dangerous",
                                     "detail": line[-2000:]})
                    if incomplete:
                        engines["clamav"] = "partial"
    else:
        engines["clamav"] = "unavailable"
    if yara and yara_rules:
        status, output = _run([yara, "scan", str(yara_rules), str(path)])
        engines["yara_x"] = "completed" if status == 0 else "error"
        if status == 0 and output.strip():
            findings.append({"rule": "yara_x", "severity": "review", "detail": output[:8000]})
    if any(f.get("severity") == "dangerous" for f in findings):
        risk = "dangerous"
    elif findings:
        risk = "review"
    elif engines.get("clamav") == "completed":
        risk = "no_detection"
    if risk!='dangerous' and (engines.get('clamav') in {'error','unavailable'} or any(
            v in {'error','partial_part_limit','partial_charset'} for v in engines.values())):
        risk='unchecked'
    return {"risk": risk, "findings": findings[:200], "engines": engines}


def safe_mail_preview(path, expected_sha256=None):
    # Plain text only: no embedded HTML, remote images, active links or scripts.
    raw = bounded_read(path,expected_sha256=expected_sha256)
    if len(raw) > 20 * 1024 * 1024:
        raise ValueError("邮件超过安全预览大小限制")
    message = email.message_from_bytes(raw, policy=email.policy.default)
    bodies, attachments = [], []
    for i, part in enumerate(message.walk()):
        if i > 200:
            break
        if part.get_filename():
            attachments.append(part.get_filename())
        elif part.get_content_type() == "text/plain":
            data = part.get_payload(decode=True) or b""
            try:
                bodies.append(data[:100000].decode(part.get_content_charset() or 'utf-8', 'replace'))
            except LookupError:
                bodies.append(data[:100000].decode('utf-8', 'replace'))
    return {"subject": str(message.get("Subject", "")), "from": str(message.get("From", "")),
            "to": str(message.get("To", "")), "text": "\n".join(bodies)[:200000],
            "attachments": attachments, "html_rendered": False}
