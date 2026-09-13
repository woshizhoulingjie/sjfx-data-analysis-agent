"""Mail source inventory with stable, safe source locators."""

from __future__ import annotations

import hashlib
import io
import mailbox
import os
import shutil
import zipfile
from pathlib import Path

from .models import MailContainerAdapter, MailMember, SourceRef
from .policy import MailPolicy, MailPolicyError


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


class EmlAdapter(MailContainerAdapter):
    format_name = "eml"

    def supports(self, source):
        return Path(source.path).suffix.casefold() == ".eml"

    def inventory(self, source, policy):
        path = Path(source.path)
        policy.validate_size(path.stat().st_size, limit=policy.max_message_bytes, label="邮件")
        yield MailMember(path.name, 0, path.stat().st_size, {"kind": "file", "path": str(path)})

    def open_message(self, source, member):
        return Path(member.locator["path"]).open("rb")


class DirectoryAdapter(MailContainerAdapter):
    format_name = "directory"

    def supports(self, source):
        return Path(source.path).is_dir()

    def inventory(self, source, policy):
        root = Path(source.path)
        ordinal = 0
        for candidate in sorted(root.rglob("*")):
            if not candidate.is_file() or candidate.suffix.casefold() != ".eml":
                continue
            try:
                candidate.resolve().relative_to(root.resolve())
            except ValueError:
                continue
            policy.validate_size(candidate.stat().st_size, limit=policy.max_message_bytes, label="邮件")
            if ordinal >= policy.max_archive_entries:
                raise MailPolicyError("邮件目录成员数超过限制")
            relative = candidate.relative_to(root).as_posix()
            yield MailMember(relative, ordinal, candidate.stat().st_size, {"kind": "file", "path": str(candidate)})
            ordinal += 1

    def open_message(self, source, member):
        return Path(member.locator["path"]).open("rb")


class ZipAdapter(MailContainerAdapter):
    format_name = "zip"

    def supports(self, source):
        return Path(source.path).suffix.casefold() == ".zip"

    def inventory(self, source, policy):
        path = Path(source.path)
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > policy.max_archive_entries:
                raise MailPolicyError("压缩包成员数超过限制")
            total = 0
            ordinal = 0
            for info in infos:
                if info.is_dir():
                    continue
                name = policy.validate_member_name(info.filename)
                total += int(info.file_size)
                if total > policy.max_total_uncompressed_bytes:
                    raise MailPolicyError("压缩包解压总大小超过限制")
                policy.validate_archive_member(info.compress_size, info.file_size)
                if not name.casefold().endswith(".eml"):
                    continue
                yield MailMember(name, ordinal, int(info.file_size), {"kind": "zip", "path": str(path), "member": info.filename})
                ordinal += 1

    def open_message(self, source, member):
        archive = zipfile.ZipFile(member.locator["path"])
        raw = archive.read(member.locator["member"])
        archive.close()
        return io.BytesIO(raw)


class MboxAdapter(MailContainerAdapter):
    format_name = "mbox"

    def supports(self, source):
        return Path(source.path).suffix.casefold() in {".mbox", ".mbx"}

    def inventory(self, source, policy):
        box = mailbox.mbox(source.path, create=False)
        try:
            for ordinal, key in enumerate(box.iterkeys()):
                if ordinal >= policy.max_archive_entries:
                    raise MailPolicyError("MBOX 邮件数超过限制")
                raw = box.get_bytes(key, from_=False)
                policy.validate_size(len(raw), limit=policy.max_message_bytes, label="MBOX 邮件")
                yield MailMember(str(key), ordinal, len(raw), {"kind": "mbox", "path": source.path, "key": str(key)})
        finally:
            box.close()

    def open_message(self, source, member):
        box = mailbox.mbox(member.locator["path"], create=False)
        try:
            # mailbox.mbox keys are integer offsets. Locators are persisted as
            # JSON, so convert the stable string representation back on read.
            return io.BytesIO(box.get_bytes(int(member.locator["key"]), from_=False))
        finally:
            box.close()


class OptionalAdapter(MailContainerAdapter):
    """A visible plug-in point for formats whose native dependency is optional."""

    def __init__(self, format_name, suffixes, dependency):
        self.format_name = format_name
        self._suffixes = set(suffixes)
        self.dependency = dependency

    def supports(self, source):
        return Path(source.path).suffix.casefold() in self._suffixes

    def inventory(self, source, policy):
        raise MailPolicyError("{} 需要可选解析器 {}；当前服务器未安装该组件".format(self.format_name.upper(), self.dependency))

    def open_message(self, source, member):
        raise MailPolicyError("{} 解析器不可用".format(self.format_name.upper()))


class NativeAdapter(OptionalAdapter):
    """Convert native containers to a stable EML cache under the mail source policy."""
    def available(self):
        import importlib.util
        return bool(importlib.util.find_spec("extract_msg")) if self.format_name=="msg" else bool(shutil.which("readpst"))

    def inventory(self,source,policy):
        if not self.available():
            raise MailPolicyError("{} 解析器未安装：{}".format(self.format_name.upper(),self.dependency))
        import subprocess,sys,json
        from config import Config
        cache=Path(Config.DATA_DIR)/"mail-native"/hashlib.sha256(str(source.source_revision).encode()).hexdigest()
        marker=cache/"complete.json"
        if not marker.exists():
            cache.mkdir(parents=True,exist_ok=True)
            if self.format_name=="msg":
                command=[sys.executable,"-m","services.mail.native_adapter",str(Path(source.path).resolve()),str(cache)]
            else:
                command=[shutil.which("readpst"),"-e","-o",str(cache),str(Path(source.path).resolve())]
            try:
                result=subprocess.run(command,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=Config.MAIL_PARSE_TIMEOUT_SECONDS)
                if result.returncode:raise MailPolicyError("原生邮件转换失败 ({})".format(result.returncode))
            except subprocess.TimeoutExpired as exc:raise MailPolicyError("原生邮件转换超时") from exc
            marker.write_text(json.dumps({"revision":source.source_revision}))
        files=sorted(f for f in cache.rglob("*") if f.is_file() and f.suffix.casefold()==".eml")
        if len(files)>policy.max_archive_entries:raise MailPolicyError("原生邮件成员数超过限制")
        total=0
        for ordinal,path in enumerate(files):
            size=path.stat().st_size;total+=size
            policy.validate_size(size,limit=policy.max_message_bytes,label="原生邮件")
            if total>policy.max_total_uncompressed_bytes:raise MailPolicyError("原生邮件总大小超过限制")
            yield MailMember(str(path.relative_to(cache)),ordinal,size,{"kind":"file","path":str(path)})

    def open_message(self,source,member):
        return Path(member.locator["path"]).open("rb")


class AdapterRegistry:
    def __init__(self):
        self.adapters = [EmlAdapter(), ZipAdapter(), DirectoryAdapter(), MboxAdapter(), NativeAdapter("msg", {".msg"}, "extract-msg"), NativeAdapter("pst", {".pst"}, "readpst")]

    def resolve(self, source: SourceRef):
        for adapter in self.adapters:
            if adapter.supports(source):
                return adapter
        raise MailPolicyError("暂不支持此邮件来源格式")

    def support_matrix(self):
        return [{"format": item.format_name, "available": item.available() if isinstance(item, NativeAdapter) else not isinstance(item, OptionalAdapter), "dependency": getattr(item, "dependency", None)} for item in self.adapters]


def open_locator(locator):
    """Open an occurrence locator created by an adapter, never a client path."""
    kind = str((locator or {}).get("kind") or "")
    if kind == "nested":
        from .parsers.eml import parse_eml
        with open_locator(locator["parent"]) as stream:
            parsed=parse_eml(stream.read())
        part=next((p for p in parsed.parts if p.mime_path==locator["mime_path"] and p.part_role=="nested_message"),None)
        if not part:raise MailPolicyError("嵌套邮件来源不可用")
        return io.BytesIO(part.raw_payload or b"")
    if kind == "file":
        return Path(locator["path"]).open("rb")
    if kind == "zip":
        with zipfile.ZipFile(locator["path"]) as archive:
            return io.BytesIO(archive.read(locator["member"]))
    if kind == "mbox":
        box = mailbox.mbox(locator["path"], create=False)
        try:
            return io.BytesIO(box.get_bytes(int(locator["key"]), from_=False))
        finally:
            box.close()
    raise MailPolicyError("邮件来源定位器不可用")


def safe_copy_mbox_message(cache_root: Path, package_id: str, occurrence_id: str, stream) -> str:
    """Persist non-addressable MBOX raw bytes only in the controlled cache."""
    target_dir = cache_root / "mail-raw" / package_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / (occurrence_id + ".eml")
    with target.open("wb") as output:
        shutil.copyfileobj(stream, output, 1024 * 1024)
    return str(target)
