"""Mail source inventory with stable, safe source locators."""

from __future__ import annotations

import hashlib
import io
import mailbox
import os
import shutil
import stat
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path

from services.native_tools import readpst_executable, readpst_environment, msg_interpreter, msg_parser_probe

from .models import MailContainerAdapter, MailMember, SourceRef
from .policy import MailPolicy, MailPolicyError


def file_identity(path):
    """Persist the object inventoried, rather than only its pathname."""
    value = Path(path).stat()
    if not stat.S_ISREG(value.st_mode):
        raise MailPolicyError("邮件来源不是普通文件")
    return {"size": int(value.st_size), "modified_at_ns": int(value.st_mtime_ns),
            "device": int(value.st_dev), "inode": int(value.st_ino)}


def _stat_identity(value):
    return (int(value.st_dev), int(value.st_ino), int(value.st_size), int(value.st_mtime_ns))


class _ReadOnlyMbox(mailbox.mbox):
    """Reuse stdlib MBOX offsets/line handling on an already-open stream."""
    def __init__(self, stream):
        self._file = stream
        self._toc = None
        self._next_key = 0
        self._pending = self._pending_sync = self._locked = False
        self._file_length = None
        self._factory = None
        self._message_factory = mailbox.mboxMessage


def inventory_mbox(source, policy, stream, identity):
    box = _ReadOnlyMbox(stream)
    total = 0
    for ordinal, key in enumerate(box.iterkeys()):
        if ordinal >= policy.max_archive_entries:
            raise MailPolicyError("MBOX 邮件数超过限制")
        with box.get_file(key, from_=False) as message:
            raw = message.read(policy.max_message_bytes + 1).replace(os.linesep.encode(), b"\n")
        policy.validate_size(len(raw), limit=policy.max_message_bytes, label="MBOX 邮件")
        total += len(raw)
        if total > policy.max_total_uncompressed_bytes:
            raise MailPolicyError("MBOX 邮件总大小超过限制")
        yield MailMember(str(key), ordinal, len(raw), {'kind':'mbox', 'path':source.path,
                        'key':str(key), 'identity':identity})


@contextmanager
def open_authorized_file(locator, policy, *, owner_id=None, source_kind=None, source_revision=None):
    """Authorize the opened object and consume its fd without reopening a path."""
    from services.package_analysis import _open_inventory_source, _opened_handle_path, SourceFileChangedError
    value = policy.resolve_locator(locator, owner_id=owner_id, source_kind=source_kind,
                                   source_revision=source_revision)
    candidate = Path(value["path"])
    identity = value.get("identity") or file_identity(candidate)
    from sjfx_protection.business import enabled, protected_path
    if enabled():
        # Ownership and the original occurrence identity were validated above.
        # Only a checked immutable snapshot reaches the message parser.
        approved = protected_path(candidate, owner=owner_id, operation='read')
        with approved.open('rb') as stream:
            yield stream
        return
    node = dict(identity, path=candidate.name)
    try:
        descriptor, before = _open_inventory_source(candidate.parent, node)
    except SourceFileChangedError as exc:
        raise MailPolicyError(str(exc)) from exc
    try:
        opened_path = _opened_handle_path(descriptor)
        if opened_path is None or opened_path != candidate:
            raise MailPolicyError("邮件来源句柄与授权路径不一致")
        # The parent itself can have been replaced between resolve and open.
        # Check ownership on the actual handle before any content is read.
        policy.resolve_locator(dict(value, path=str(opened_path)), owner_id=owner_id,
                               source_kind=source_kind, source_revision=source_revision)
        stream = os.fdopen(descriptor, "rb")
        descriptor = None
        with stream:
            yield stream
            if _stat_identity(before) != _stat_identity(os.fstat(stream.fileno())):
                raise MailPolicyError("邮件来源在读取期间发生变化")
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextmanager
def authorized_source_snapshot(locator, policy, **authorization):
    """Give path-only parsers a private copy made from an authorized fd."""
    from config import Config
    Config.PARSE_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    with open_authorized_file(locator, policy, **authorization) as stream:
        before = os.fstat(stream.fileno())
        size = before.st_size
        if size > int(Config.MAX_CONTENT_BYTES):
            raise MailPolicyError("邮件来源超过内容大小限制")
        if shutil.disk_usage(Config.PARSE_TEMP_DIR).free < size + int(Config.PARSE_TEMP_DISK_RESERVE_BYTES or 0):
            raise OSError("解析临时卷剩余空间不足")
        with tempfile.TemporaryDirectory(prefix="sjfx-mail-source-", dir=Config.PARSE_TEMP_DIR) as scratch:
            snapshot = Path(scratch) / Path(locator["path"]).name
            with snapshot.open("xb") as target:
                copied = 0
                while True:
                    block = stream.read(1024 * 1024)
                    if not block:
                        break
                    copied += len(block)
                    if copied > size:
                        raise MailPolicyError("邮件来源在快照期间增长")
                    target.write(block)
            if copied != size or _stat_identity(os.fstat(stream.fileno())) != _stat_identity(before):
                raise MailPolicyError("邮件来源在快照期间发生变化")
            yield snapshot


def sha256_file(path: Path, *, policy=None, owner_id=None) -> str:
    digest = hashlib.sha256()
    context = (open_authorized_file({'kind':'file', 'path':str(path)}, policy, owner_id=owner_id)
               if policy is not None else path.open("rb"))
    with context as handle:
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
        return open_locator(member.locator)


class MboxAdapter(MailContainerAdapter):
    format_name = "mbox"

    def supports(self, source):
        return Path(source.path).suffix.casefold() in {".mbox", ".mbx"}

    def inventory(self, source, policy):
        box = mailbox.mbox(source.path, create=False)
        try:
            total = 0
            for ordinal, key in enumerate(box.iterkeys()):
                if ordinal >= policy.max_archive_entries:
                    raise MailPolicyError("MBOX 邮件数超过限制")
                with box.get_file(key, from_=False) as stream:
                    raw = stream.read(policy.max_message_bytes + 1)
                policy.validate_size(len(raw), limit=policy.max_message_bytes, label="MBOX 邮件")
                total += len(raw)
                if total > getattr(policy, "max_total_uncompressed_bytes", 4 * 1024**3):
                    raise MailPolicyError("MBOX 邮件总大小超过限制")
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
        return msg_parser_probe()['ok'] if self.format_name=="msg" else bool(readpst_executable())

    @staticmethod
    def _converted_files(root, policy):
        """Validate all members before publishing or yielding a conversion."""
        from sjfx_multiuser.uploads import UploadError, _assert_no_links
        files=[];total=0
        for path in root.rglob("*"):
            if path.suffix.casefold()!=".eml" or not path.is_file():
                continue
            try:
                _assert_no_links(path)
                path.resolve().relative_to(root.resolve())
            except (UploadError, ValueError, OSError) as exc:
                raise MailPolicyError("原生邮件转换成员路径不安全") from exc
            size=path.stat().st_size;total+=size
            policy.validate_size(size,limit=policy.max_message_bytes,label="原生邮件")
            if total>policy.max_total_uncompressed_bytes:raise MailPolicyError("原生邮件总大小超过限制")
            files.append(path)
            if len(files)>policy.max_archive_entries:raise MailPolicyError("原生邮件成员数超过限制")
        if not files:raise MailPolicyError("原生邮件转换未生成可导入 EML；原件已保留")
        return sorted(files)

    def inventory(self,source,policy):
        if not self.available():
            raise MailPolicyError("{} 解析器未安装：{}".format(self.format_name.upper(),self.dependency))
        import subprocess,json,uuid
        from config import Config
        cache=Path(Config.DATA_DIR)/"mail-native"/hashlib.sha256(str(source.source_revision).encode()).hexdigest()
        marker=cache/"complete.json"
        if not marker.exists():
            cache.mkdir(parents=True,exist_ok=True)
            attempt=uuid.uuid4().hex
            staging=cache/("attempt-"+attempt)
            published=cache/("published-"+attempt)
            staging.mkdir(mode=0o700)
            temporary_marker=cache/(".complete-"+attempt+".json")
            try:
                if self.format_name=="msg":
                    if os.getenv('SJFX_MSG_PYTHON','').strip():
                        app_root=str(Path(__file__).resolve().parents[2])
                        bootstrap="import sys; sys.path.insert(0,{!r}); from services.mail.native_adapter import main; main()".format(app_root)
                        command=[msg_interpreter(),"-I","-B","-c",bootstrap,str(Path(source.path).resolve()),str(staging)]
                        child_env=os.environ.copy()
                        child_env.pop('PYTHONPATH',None);child_env.pop('PYTHONHOME',None)
                        child_env['PYTHONDONTWRITEBYTECODE']='1'
                    else:
                        command=[msg_interpreter(),"-m","services.mail.native_adapter",str(Path(source.path).resolve()),str(staging)]
                        child_env=None
                else:
                    binary=readpst_executable()
                    command=[binary,"-e","-o",str(staging),str(Path(source.path).resolve())]
                    child_env=readpst_environment(binary)
                # The child cannot inherit sys.path additions made by a CLI
                # launched elsewhere. Resolve the module from the app root;
                # executing the file directly would shadow stdlib threading.
                result=subprocess.run(command,cwd=str(Path(__file__).resolve().parents[2]),env=child_env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=Config.MAIL_PARSE_TIMEOUT_SECONDS)
                if result.returncode:raise MailPolicyError("原生邮件转换失败 ({})".format(result.returncode))
                self._converted_files(staging,policy)
                staging.rename(published)
                temporary_marker.write_text(json.dumps({"revision":source.source_revision,"subdir":published.name}),encoding="utf-8")
                os.replace(temporary_marker,marker)
            except subprocess.TimeoutExpired as exc:raise MailPolicyError("原生邮件转换超时") from exc
            finally:
                # Only this invocation's disposable attempt is removed. Older
                # sources, caches and successfully published locators survive.
                if staging.exists():shutil.rmtree(staging)
                temporary_marker.unlink(missing_ok=True)
        try:
            completed=json.loads(marker.read_text(encoding="utf-8"))
            if completed.get("revision")!=source.source_revision:raise ValueError("revision")
            subdir=completed.get("subdir")
            if subdir is not None and (not isinstance(subdir,str) or not subdir.startswith("published-")
                or len(subdir)!=42 or any(c not in "0123456789abcdef" for c in subdir[10:])):
                raise ValueError("subdir")
            member_root=cache/subdir if subdir else cache
            if not member_root.is_dir():raise ValueError("directory")
        except (OSError,ValueError,TypeError,AttributeError) as exc:
            raise MailPolicyError("原生邮件转换缓存标记无效；原件已保留") from exc
        files=self._converted_files(member_root,policy)
        for ordinal,path in enumerate(files):
            size=path.stat().st_size
            yield MailMember(str(path.relative_to(member_root)),ordinal,size,{"kind":"file","path":str(path)})

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


def open_locator(locator, *, policy=None, owner_id=None, source_kind=None, source_revision=None):
    """Open an occurrence locator created by an adapter, never a client path."""
    if policy is not None:
        return _open_authorized_locator(locator, policy, owner_id=owner_id,
                                        source_kind=source_kind, source_revision=source_revision)
    kind = str((locator or {}).get("kind") or "")
    if kind == "nested":
        from .parsers.eml import parse_eml
        with open_locator(locator["parent"]) as stream:
            parent_raw=stream.read()
            parsed=parse_eml(parent_raw)
        part=next((p for p in parsed.parts if p.mime_path==locator["mime_path"] and p.part_role=="nested_message"),None)
        if not part:raise MailPolicyError("嵌套邮件来源不可用")
        from sjfx_protection.business import inspect_bytes
        return io.BytesIO(inspect_bytes(part.raw_payload or b"",str(locator["mime_path"])+".eml",hashlib.sha256(parent_raw).hexdigest()))
    if kind == "file":
        from sjfx_protection.business import protected_path
        return protected_path(locator["path"]).open("rb")
    if kind == "zip":
        from sjfx_protection.business import file_execution,inspect_bytes
        with file_execution(locator['path']) as approved:
            with zipfile.ZipFile(approved) as archive:
                member=locator['member']
                if 'header_offset' in locator:
                    member=next((info for info in archive.infolist() if info.header_offset==locator['header_offset'] and info.filename==locator['member']),None)
                    if member is None:raise MailPolicyError('压缩包来源条目已变化')
                return io.BytesIO(inspect_bytes(archive.read(member),locator['member']))
    if kind == "mbox":
        from sjfx_protection.business import file_execution,inspect_bytes
        with file_execution(locator['path']) as approved:
            box = mailbox.mbox(str(approved), create=False)
            try:
                return io.BytesIO(inspect_bytes(box.get_bytes(int(locator["key"]), from_=False),str(locator['key'])+'.eml'))
            finally:
                box.close()
    raise MailPolicyError("邮件来源定位器不可用")


@contextmanager
def _open_authorized_locator(locator, policy, **authorization):
    kind = str((locator or {}).get("kind") or "")
    if kind == "nested":
        from .parsers.eml import parse_eml
        with open_locator(locator["parent"], policy=policy, **authorization) as stream:
            raw = stream.read(policy.max_message_bytes + 1)
        policy.validate_size(len(raw), limit=policy.max_message_bytes, label="邮件")
        parsed = parse_eml(raw, max_parts=policy.max_mime_parts,
                           max_nested_message_depth=policy.max_nested_message_depth)
        part = next((p for p in parsed.parts if p.mime_path == locator["mime_path"]
                     and p.part_role == "nested_message"), None)
        if part is None:
            raise MailPolicyError("嵌套邮件来源不可用")
        with io.BytesIO(part.raw_payload or b"") as result:
            yield result
    elif kind == "file":
        with open_authorized_file(locator, policy, **authorization) as stream:
            yield stream
    elif kind == "zip":
        with open_authorized_file(locator, policy, **authorization) as stream:
            with zipfile.ZipFile(stream) as archive:
                member = locator["member"]
                if "header_offset" in locator:
                    member = next((info for info in archive.infolist()
                                   if info.header_offset == locator["header_offset"]
                                   and info.filename == locator["member"]), None)
                    if member is None:
                        raise MailPolicyError("压缩包来源条目已变化")
                info = archive.getinfo(member) if isinstance(member, str) else member
                policy.validate_member_name(info.filename)
                policy.validate_archive_member(info.compress_size, info.file_size)
                with archive.open(info) as message:
                    raw = message.read(policy.max_message_bytes + 1)
                policy.validate_size(len(raw), limit=policy.max_message_bytes, label="邮件")
            with io.BytesIO(raw) as result:
                yield result
    elif kind == "mbox":
        with open_authorized_file(locator, policy, **authorization) as stream:
            box = _ReadOnlyMbox(stream)
            with box.get_file(int(locator["key"]), from_=False) as message:
                raw = message.read(policy.max_message_bytes + 1).replace(os.linesep.encode(), b"\n")
            policy.validate_size(len(raw), limit=policy.max_message_bytes, label="MBOX 邮件")
            with io.BytesIO(raw) as result:
                yield result
    else:
        raise MailPolicyError("邮件来源定位器不可用")


class ProtectedAdapter(MailContainerAdapter):
    """Business parsing receives approved snapshots and separately checked members."""
    def __init__(self,adapter):self.adapter=adapter;self.format_name=adapter.format_name
    def supports(self,source):return self.adapter.supports(source)
    def inventory(self,source,policy):
        from sjfx_protection.business import file_execution
        if self.format_name=='directory':
            yield from self.adapter.inventory(source,policy)
        else:
            with file_execution(source.path,operation='read') as snapshot:
                approved=SourceRef(source.package_id,source.kind,str(snapshot),source.source_revision)
                yield from self.adapter.inventory(approved,policy)
    def open_message(self,source,member):
        from sjfx_protection.business import file_execution,inspect_bytes
        parent=member.locator.get('path') if self.format_name=='directory' else source.path
        with file_execution(parent,operation='read') as snapshot:
            locator=dict(member.locator)
            if self.format_name in {'directory','eml'}:
                raw=snapshot.read_bytes()
            else:
                locator['path']=str(snapshot)
                approved_member=MailMember(member.name,member.ordinal,member.byte_size,locator)
                approved=SourceRef(source.package_id,source.kind,str(snapshot),source.source_revision)
                with self.adapter.open_message(approved,approved_member) as stream:raw=stream.read()
                raw=inspect_bytes(raw,member.name)
            return io.BytesIO(raw)


def safe_copy_mbox_message(cache_root: Path, package_id: str, occurrence_id: str, stream) -> str:
    """Persist non-addressable MBOX raw bytes only in the controlled cache."""
    target_dir = cache_root / "mail-raw" / package_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / (occurrence_id + ".eml")
    with target.open("wb") as output:
        shutil.copyfileobj(stream, output, 1024 * 1024)
    return str(target)
