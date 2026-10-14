"""Bounded passive inspection of immutable bytes; never executes or follows content.

``no_detection`` means no rule fired within the declared coverage, never a claim
that arbitrary content is safe. Unknown/opaque formats, parser failures and any
budget exhaustion are incomplete. Antivirus is independently required by default.
The optional ``av_adapter`` is for synthetic tests only and cannot satisfy that
requirement. Production AV uses a local ClamAV CLI and records its real revision.
"""
from __future__ import annotations

import bz2
import codecs
import email.errors
import email.policy
from email.parser import BytesParser
import hashlib
from html.parser import HTMLParser
import io
import json
import lzma
import ntpath
import os
from pathlib import PurePosixPath
import re
import shutil
import stat
import struct
import subprocess
import tarfile
import tempfile
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
import zipfile
import zlib

REVISION = "sjfx-local-passive-20261014-v3"
DEFAULTS = {
    "required_av": True, "max_bytes": 20 * 1024 * 1024,
    "max_expanded_bytes": 32 * 1024 * 1024, "max_members": 200,
    "max_depth": 4, "max_html_bytes": 2 * 1024 * 1024,
    "max_findings": 200, "max_ratio": 200, "av_timeout": 45,
    "av_max_bytes": 2 * 1024 ** 3, "max_decoder_memory_bytes": 64 * 1024 * 1024,
}
EXECUTABLE = frozenset({".exe", ".com", ".scr", ".msi", ".dll", ".js", ".jse",
    ".vbs", ".vbe", ".wsf", ".wsh", ".lnk", ".hta", ".bat", ".cmd", ".ps1",
    ".psm1", ".url", ".reg", ".so", ".dylib", ".app", ".sh"})
DOCUMENT = frozenset({".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".txt", ".jpg", ".jpeg", ".png", ".csv"})
TEXT = frozenset({".txt", ".csv", ".tsv", ".json", ".jsonl", ".xml", ".log",
    ".md", ".yaml", ".yml", ".ini", ".cfg", ".ics"})
HTML = frozenset({".html", ".htm", ".shtml", ".svg", ".xhtml"})
OFFICE = {".docx": "word/document.xml", ".docm": "word/document.xml",
    ".dotx": "word/document.xml", ".dotm": "word/document.xml",
    ".xlsx": "xl/workbook.xml", ".xlsm": "xl/workbook.xml", ".xlam": "xl/workbook.xml",
    ".pptx": "ppt/presentation.xml", ".pptm": "ppt/presentation.xml"}
OPAQUE = frozenset({".rar", ".7z", ".doc", ".xls", ".ppt", ".msg", ".ole",
    ".cab", ".iso", ".dmg", ".odt", ".ods", ".odp"})
ACTIVE_SCHEMES = frozenset({"javascript", "vbscript", "file", "data", "ms-msdt",
    "search-ms", "shell", "powershell"})

# This complete 9,395-byte opaque print profile is carried unchanged by the
# public python-pptx default.pptx template. SJFX extracts slide/table/notes text
# and images, never invokes a printer driver, and python-pptx retains this part
# as a raw OPC Part. Recognize only that exact profile, not arbitrary driver
# data, and only when its package content type and internal relationship agree.
_PPTX_PRINTER_PART = "ppt/printerSettings/printerSettings1.bin"
_PPTX_PRINTER_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.printerSettings"
_PPTX_PRINTER_RELATIONSHIP = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/printerSettings"
_PPTX_PRINTER_SIZE = 9395
_PPTX_PRINTER_SHA256 = "d7768f87e07d29634782e448ece1cddd05a52b9499254222b1190bc8f6dc579e"
_OPC_TYPES_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"
_OPC_RELS_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _pptx_printer_context(content_types, relationships):
    """Bind the known profile to bounded, canonical, internal OPC metadata."""
    if not content_types or not relationships:
        return False
    decoded = []
    for data in (content_types, relationships):
        if len(data) > 64 * 1024:
            return False
        try:
            text = data.decode("utf-8-sig", "strict")
        except UnicodeError:
            return False
        if "\x00" in text or re.search(r"<!DOCTYPE|<!ENTITY", text, re.I):
            return False
        decoded.append(text)
    try:
        types, rels = (ET.fromstring(text) for text in decoded)
    except ET.ParseError:
        return False
    if types.tag != _OPC_TYPES_NS + "Types" or rels.tag != _OPC_RELS_NS + "Relationships":
        return False
    if any(e.tag not in {_OPC_TYPES_NS + "Default", _OPC_TYPES_NS + "Override"} for e in types):
        return False
    defaults = [e.attrib.get("ContentType") for e in types
                if e.tag == _OPC_TYPES_NS + "Default" and e.attrib.get("Extension") == "bin"]
    overrides = [e.attrib.get("ContentType") for e in types
                 if e.tag == _OPC_TYPES_NS + "Override" and e.attrib.get("PartName") == "/" + _PPTX_PRINTER_PART]
    if len(defaults) > 1 or len(overrides) > 1:
        return False
    declared = overrides or defaults
    if declared != [_PPTX_PRINTER_CONTENT_TYPE]:
        return False
    ids = [e.attrib.get("Id") for e in rels]
    if any(e.tag != _OPC_RELS_NS + "Relationship" for e in rels) or not all(ids) or len(set(ids)) != len(ids):
        return False
    targets = [e for e in rels if e.attrib.get("Target") == "printerSettings/printerSettings1.bin"]
    return (len(targets) == 1 and targets[0].attrib.get("Type") == _PPTX_PRINTER_RELATIONSHIP
            and targets[0].attrib.get("TargetMode", "") in {"", "Internal"})


def _suffix(name):
    return ntpath.splitext(ntpath.basename(name).rstrip(" ."))[1].lower()


class _Inspection:
    def __init__(self, policy):
        self.policy = policy
        self.findings = []
        self.reasons = set()
        self.formats = set()
        self.members = []
        self.remaining = policy["max_expanded_bytes"]
        self.count = 0
        self.finding_limit = False
        self.dangerous = False
        self.mail_evidence = []

    def add(self, rule, detail="", severity="review"):
        if severity == "dangerous":
            self.dangerous = True
        if severity == "incomplete":
            self.reasons.add(rule)
        if len(self.findings) >= self.policy["max_findings"] - 1:
            self.reasons.add("findings_limit")
            # A later dangerous hit must not disappear behind earlier low-risk
            # noise. Keep at least one such finding within the same output cap.
            if severity == "dangerous" and not any(f["severity"] == "dangerous" for f in self.findings):
                self.findings[-1] = {"rule": rule, "detail": str(detail)[:1000], "severity": severity}
            if not self.finding_limit:
                self.finding_limit = True
                self.findings.append({"rule": "findings_limit", "detail": "Finding budget exhausted",
                                      "severity": "incomplete"})
            return
        self.findings.append({"rule": rule, "detail": str(detail)[:1000], "severity": severity})

    def filename(self, name):
        base = ntpath.basename(name)
        suffix = _suffix(name)
        if suffix in EXECUTABLE:
            self.add("executable_content", base, "dangerous")
            stem = ntpath.splitext(base.rstrip(" ."))[0]
            if _suffix(stem) in DOCUMENT:
                self.add("double_extension", base)
        if re.search("[\u202a-\u202e\u2066-\u2069]", name):
            self.add("bidirectional_filename", name)
        if base.rstrip(" .") != base or ":" in base:
            self.add("ambiguous_filename", name)
        if suffix in {".docm", ".xlsm", ".pptm", ".xlam", ".dotm"}:
            self.add("macro_document", base)

    def child(self, data, name, parent, depth, content_type="", charset=None, office=False, printer_settings=False):
        self.members.append({"parent_sha256": hashlib.sha256(parent).hexdigest(),
            "member_name": name[:1000], "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data), "depth": depth})
        self.payload(data, name, depth, content_type, charset, office, printer_settings)

    def reserve_member(self, name, depth):
        self.count += 1
        if self.count > self.policy["max_members"]:
            self.add("member_limit", name, "incomplete")
            return False
        if depth > self.policy["max_depth"]:
            self.add("depth_limit", name, "incomplete")
            return False
        return not self.finding_limit

    def reserve_bytes(self, size, name, compressed=None):
        if size > self.remaining or (compressed is not None and
                size > max(compressed, 1) * self.policy["max_ratio"]):
            self.add("expansion_limit", name, "incomplete")
            return False
        self.remaining -= size
        return True

    def decode(self, data, name, charset=None, html=False):
        encoding = charset
        if data.startswith(codecs.BOM_UTF8):
            encoding = "utf-8-sig"
        elif data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
            encoding = "utf-32"
        elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            encoding = "utf-16"
        elif not encoding and data[:4] in (b"<\x00?\x00", b"<\x00h\x00", b"\x00<\x00?"):
            encoding = "utf-16-be" if data.startswith(b"\x00") else "utf-16-le"
        if html and not encoding:
            match = re.search(br"charset\s*=\s*[\"']?\s*([a-zA-Z0-9._-]+)", data[:8192], re.I)
            if match:
                encoding = match[1].decode("ascii")
        if not encoding:
            match = re.match(br"\s*<\?xml[^>]*encoding\s*=\s*[\"']([^\"']+)", data[:200], re.I)
            if match:
                encoding = match[1].decode("ascii", "replace")
        try:
            text = data.decode(encoding or "utf-8", "strict")
        except (UnicodeError, LookupError):
            self.add("text_decode_error", name, "incomplete")
            text = data.decode("utf-8", "replace")
        if "\x00" in text:
            self.add("binary_text", name, "incomplete")
        return text

    def payload(self, data, name, depth=0, content_type="", charset=None, office=False, printer_settings=False):
        self.filename(name)
        suffix = _suffix(name)
        # The magic is independently checked, even when the executable is truncated.
        magic = None
        if data.startswith(b"MZ"):
            magic = "pe_or_dos"
        elif data.startswith(b"\x7fELF"):
            magic = "elf"
        elif data[:4] in (b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
                           b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"):
            magic = "mach_o_or_fat"
        elif data.startswith(b"L\x00\x00\x00\x01\x14\x02\x00\x00\x00\x00\x00\xc0\x00\x00\x00"):
            magic = "windows_shortcut"
        if magic:
            self.formats.add(magic)
            if suffix not in EXECUTABLE:
                self.add("disguised_executable", name, "dangerous")
            else:
                self.add("executable_magic", magic)
            return
        if suffix == ".msg":
            import importlib.util
            present = importlib.util.find_spec("extract_msg") is not None
            self.formats.add("outlook_msg")
            # services.mail.native_adapter uses asEmailMessage; that conversion
            # does not certify RTF/embedded MSG and every original attachment.
            self.mail_evidence.append({"format": "msg", "status": "incomplete",
                "parser": "extract_msg", "component_available": present,
                "reason": "Dedicated bounded original-container coverage is not implemented"})
            self.add("msg_original_container_incomplete", name, "incomplete")
            return
        if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") or suffix in OPAQUE:
            self.formats.add("opaque_container")
            self.add("unsupported_container", name, "incomplete")
            return
        if data.startswith((b"Rar!\x1a\x07", b"7z\xbc\xaf\x27\x1c", b"MSCF", b"x\x9f>\x22")):
            self.add("unsupported_container", name, "incomplete")
            return
        if data.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")) or suffix in OFFICE or suffix == ".zip":
            self.formats.add("zip")
            self.archive_zip(data, name, depth)
            return
        if data.startswith(b"\x1f\x8b") or suffix in {".gz", ".tgz"}:
            self.compressed(data, name, depth, "gzip")
            return
        if data.startswith(b"BZh") or suffix in {".bz2", ".tbz2"}:
            self.compressed(data, name, depth, "bzip2")
            return
        if data.startswith(b"\xfd7zXZ\x00") or suffix in {".xz", ".txz"}:
            self.compressed(data, name, depth, "xz")
            return
        if suffix == ".tar" or data[257:262] == b"ustar":
            self.formats.add("tar")
            self.archive_tar(data, name, depth)
            return
        if suffix == ".eml" or content_type == "message/rfc822" or re.match(
                br"(?:From|Received|MIME-Version|Return-Path):", data, re.I):
            self.formats.add("email")
            self.mail(data, name, depth)
            return
        if data.startswith(b"%PDF-") or suffix == ".pdf":
            self.formats.add("pdf")
            self.pdf(data, name)
            return
        if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
            self.formats.add("image")
            self.image(data, name, suffix)
            return
        # Sniff HTML/SVG in text files so a renamed active document is covered.
        html = suffix in HTML or content_type in {"text/html", "image/svg+xml", "application/xhtml+xml"}
        sniff = data[:8192].replace(b"\x00", b"")
        html = html or bool(re.search(br"<(?:!doctype\s+html|html|script|iframe|form|svg|object)\b", sniff, re.I))
        if html:
            self.formats.add("html")
            self.html(data, name, charset)
            return
        if office and (suffix in {".xml", ".rels"} or ntpath.basename(name).casefold() == ".rels"):
            self.formats.add("office_xml")
            self.office_xml(data, name)
            return
        if suffix in TEXT or suffix in EXECUTABLE or content_type.startswith("text/"):
            self.formats.add("text")
            text = self.decode(data, name, charset)
            if suffix in {".csv", ".tsv"} and re.search(r'(?:^|[\n,\t])[ \r\"]*[=+@-]', text):
                self.add("spreadsheet_formula", name)
            if suffix == ".xml":
                if re.search(r"<!DOCTYPE|<!ENTITY", text, re.I):
                    self.add("xml_external_or_entity_declaration", name)
            if suffix == ".rtf":
                self.add("rtf_requires_parser", name, "incomplete")
            return
        # Office fixed binary parts are only passed here in a validated OOXML ZIP.
        if office and suffix in {".bin", ".dat"}:
            if (printer_settings and name == _PPTX_PRINTER_PART and len(data) == _PPTX_PRINTER_SIZE
                    and hashlib.sha256(data).hexdigest() == _PPTX_PRINTER_SHA256):
                self.formats.add("office_known_printer_metadata")
            else:
                self.add("office_binary_part_requires_parser", name, "incomplete")
        else:
            self.add("unsupported_format", name or "(unnamed)", "incomplete")

    def html(self, data, name, charset=None, depth=0):
        if depth > self.policy["max_depth"]:
            self.add("html_nested_depth_limit", name, "incomplete")
            return
        if len(data) > self.policy["max_html_bytes"]:
            self.add("html_size_limit", name, "incomplete")
            data = data[:self.policy["max_html_bytes"]]
        parser = _HTML(self, name, depth)
        try:
            parser.feed(self.decode(data, name, charset, True))
            parser.close()
            if parser.anchor:
                parser.close_anchor()
            if parser.password and parser.form:
                self.add("credential_form", name, "dangerous")
        except (ValueError, RecursionError) as exc:
            self.add("html_parse_error", type(exc).__name__, "incomplete")

    def archive_path(self, name):
        item = name.replace("\\", "/")
        if item.startswith("/") or ".." in PurePosixPath(item).parts or ntpath.splitdrive(item)[0] or ":" in item:
            self.add("archive_path_escape", name, "dangerous")
        if any(p.rstrip(" .").split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", "COM1", "LPT1"}
               or p != p.rstrip(" .") for p in item.split("/") if p):
            self.add("archive_ambiguous_path", name)
        return item

    def archive_zip(self, data, name, depth):
        suffix = _suffix(name)
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                infos = archive.infolist()
                names = {i.filename.replace("\\", "/") for i in infos}
                office = "[Content_Types].xml" in names
                if suffix in OFFICE and not {"[Content_Types].xml", OFFICE[suffix]} <= names:
                    self.add("invalid_office_package", name, "incomplete")
                seen = set()
                # Read these two existing members through the ordinary bounded
                # ZIP/CRC/child-inspection path before classifying the profile.
                # No second unbudgeted archive read or filename-only exception.
                printer_package = suffix == ".pptx" and {"[Content_Types].xml", "ppt/presentation.xml"} <= {i.orig_filename for i in infos}
                printer_metadata = {"[Content_Types].xml": None, "ppt/_rels/presentation.xml.rels": None}
                ordered = sorted(infos, key=lambda i: 0 if i.orig_filename in printer_metadata else 1) if printer_package else infos
                for member in ordered:
                    item = self.archive_path(member.filename)
                    self.filename(item)
                    if not self.reserve_member(item, depth + 1):
                        break
                    if item in seen:
                        self.add("duplicate_archive_member", item, "incomplete")
                    seen.add(item)
                    mode = member.external_attr >> 16
                    if stat.S_ISLNK(mode):
                        self.add("archive_link", item, "dangerous")
                    if member.flag_bits & 1:
                        self.add("encrypted_archive_member", item, "incomplete")
                        continue
                    lower = item.lower()
                    if lower.endswith("vbaproject.bin") or lower.endswith("vbadata.xml"):
                        self.add("embedded_macro", item)
                    if "/embeddings/" in lower or "/activex/" in lower:
                        self.add("office_active_or_embedded_object", item)
                    if member.is_dir():
                        continue
                    if not self.reserve_bytes(member.file_size, item, member.compress_size):
                        continue
                    with archive.open(member) as source:
                        payload = source.read(member.file_size + 1)
                    if len(payload) != member.file_size:
                        self.add("archive_member_size_mismatch", item, "incomplete")
                        continue
                    printer_settings = (printer_package and member.orig_filename == member.filename == _PPTX_PRINTER_PART
                        and _pptx_printer_context(printer_metadata["[Content_Types].xml"],
                                                 printer_metadata["ppt/_rels/presentation.xml.rels"]))
                    self.child(payload, item, data, depth + 1, office=office, printer_settings=printer_settings)
                    if printer_package and member.orig_filename == member.filename and member.filename in printer_metadata:
                        printer_metadata[member.filename] = payload
        except (OSError, ValueError, RuntimeError, NotImplementedError, zipfile.BadZipFile, EOFError, zlib.error) as exc:
            self.add("unreadable_archive", type(exc).__name__, "incomplete")

    def archive_tar(self, data, name, depth):
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
                for member in archive:
                    item = self.archive_path(member.name)
                    self.filename(item)
                    if not self.reserve_member(item, depth + 1):
                        break
                    if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                        self.add("archive_link_or_special_member", item, "dangerous")
                        continue
                    if not member.isfile():
                        continue
                    if not self.reserve_bytes(member.size, item):
                        continue
                    source = archive.extractfile(member)
                    if source is None:
                        self.add("unreadable_archive_member", item, "incomplete")
                        continue
                    with source:
                        payload = source.read(member.size + 1)
                    if len(payload) != member.size:
                        self.add("archive_member_size_mismatch", item, "incomplete")
                        continue
                    self.child(payload, item, data, depth + 1)
        except (tarfile.TarError, OSError, ValueError, EOFError) as exc:
            self.add("unreadable_archive", type(exc).__name__, "incomplete")

    def compressed(self, data, name, depth, kind):
        self.formats.add(kind)
        if not self.reserve_member(name, depth + 1):
            return
        limit = min(self.remaining, max(len(data), 1) * self.policy["max_ratio"])
        try:
            decoder = {"gzip": lambda: zlib.decompressobj(16 + zlib.MAX_WBITS),
                       "bzip2": bz2.BZ2Decompressor,
                       "xz": lambda: lzma.LZMADecompressor(memlimit=self.policy["max_decoder_memory_bytes"])}[kind]()
            payload = decoder.decompress(data, limit + 1)
            if len(payload) > limit or not decoder.eof:
                self.add("expansion_limit", name, "incomplete")
                return
            if decoder.unused_data:
                # Concatenated streams require a bounded multi-stream adapter.
                self.add("compressed_trailing_data", name, "incomplete")
            if not self.reserve_bytes(len(payload), name):
                return
            child_name = re.sub(r"\.(?:gz|bz2|xz)$", "", name, flags=re.I)
            if _suffix(name) in {".tgz", ".tbz2", ".txz"}:
                child_name = ntpath.splitext(name)[0] + ".tar"
            self.child(payload, child_name, data, depth + 1)
        except (OSError, EOFError, ValueError, zlib.error, lzma.LZMAError) as exc:
            self.add("unreadable_archive", type(exc).__name__, "incomplete")

    def mail(self, data, name, depth):
        try:
            # The stdlib builds the full MIME tree before yielding leaves. Bound
            # its obvious part markers first, rather than only walking a huge
            # already-allocated tree. Conservative false positives stay blocked.
            markers = 0
            for _ in re.finditer(br"(?im)^(?:--[^\r\n]+|Content-Type:)", data):
                markers += 1
                if markers > self.policy["max_members"] * 3:
                    self.add("mail_parser_member_limit", name, "incomplete")
                    return
            message = BytesParser(policy=email.policy.default).parsebytes(data)
            from sjfx_local_protection.mail_risk import inspect_message
            self.mail_evidence.append(inspect_message(message, self.add, self.policy))
            if not any(message.get(k) for k in ("From", "To", "Subject", "MIME-Version", "Content-Type")):
                self.add("invalid_mail_format", name, "incomplete")
            stack = [(message, depth)]
            while stack:
                part, level = stack.pop()
                filename = part.get_filename() or ""
                if not self.reserve_member(filename or name, level + 1):
                    break
                if part.is_multipart():
                    children = part.get_payload()
                    if not isinstance(children, list):
                        self.add("mail_parse_error", name, "incomplete")
                        continue
                    stack.extend((child, level + 1) for child in reversed(children))
                    if filename:
                        self.filename(filename)
                    if part.defects:
                        self.add("mail_parse_defect", repr(part.defects), "incomplete")
                    continue
                payload = part.get_payload(decode=True)
                if payload is None:
                    self.add("mail_decode_error", filename or name, "incomplete")
                    continue
                if part.defects:
                    self.add("mail_parse_defect", repr(part.defects), "incomplete")
                if not self.reserve_bytes(len(payload), filename or name):
                    continue
                ctype = part.get_content_type()
                child_name = filename or ("body.html" if ctype == "text/html" else
                    "body.txt" if ctype.startswith("text/") else "unnamed-attachment")
                if filename and _suffix(filename) in EXECUTABLE:
                    self.add("executable_attachment", filename)
                self.child(payload, child_name, data, level + 1, ctype, part.get_content_charset())
        except (email.errors.MessageError, UnicodeError, ValueError, RecursionError) as exc:
            self.add("mail_parse_error", type(exc).__name__, "incomplete")

    def office_xml(self, data, name):
        text = self.decode(data, name)
        if re.search(r"<!DOCTYPE|<!ENTITY", text, re.I):
            self.add("office_xml_entity", name, "incomplete")
            return
        try:
            root = ET.fromstring(text)
            for elem in root.iter():
                attrs = {k.rsplit("}", 1)[-1]: v for k, v in elem.attrib.items()}
                if attrs.get("TargetMode", "").lower() == "external":
                    self.add("office_external_relationship", attrs.get("Target", name))
                if "macroenabled" in attrs.get("ContentType", "").lower() or "vbaproject" in attrs.get("ContentType", "").lower():
                    self.add("office_macro_content_type", name)
                if elem.tag.rsplit("}", 1)[-1].lower() in {"ddeitem", "ddelink", "oleobject", "control", "externalreference"}:
                    self.add("office_active_or_external_xml", name)
                if elem.tag.rsplit("}", 1)[-1] in {"f", "definedName"} and elem.text and re.search(
                        r"DDE|WEBSERVICE\s*\(|HYPERLINK\s*\(|\[[^]]+\]", elem.text, re.I):
                    self.add("office_active_formula", elem.text)
        except ET.ParseError:
            self.add("office_xml_parse_error", name, "incomplete")

    def pdf(self, data, name):
        if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-2048:]:
            self.add("invalid_pdf_format", name, "incomplete")
        # Decode escaped PDF names (/Java#53cript), but do not evaluate streams.
        normalized = re.sub(br"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m[1], 16)]), data)
        if re.search(br"/(?:JavaScript|JS|Launch|OpenAction|AA)\b", normalized):
            self.add("pdf_active_object", name)
        if re.search(br"/(?:EmbeddedFile|Filespec|RichMedia|XFA)\b", normalized):
            self.add("pdf_embedded_or_active_requires_parser", name, "incomplete")
        if re.search(br"/(?:Encrypt|ObjStm|Filter)\b", normalized):
            self.add("pdf_opaque_stream_requires_parser", name, "incomplete")

    def image(self, data, name, suffix):
        valid = False
        if suffix == ".png":
            if data.startswith(b"\x89PNG\r\n\x1a\n"):
                offset = 8
                first = True
                while offset + 12 <= len(data):
                    size = int.from_bytes(data[offset:offset + 4], "big")
                    kind = data[offset + 4:offset + 8]
                    end = offset + 12 + size
                    if end > len(data):
                        break
                    payload = data[offset + 8:offset + 8 + size]
                    crc = int.from_bytes(data[end - 4:end], "big")
                    if zlib.crc32(kind + payload) & 0xffffffff != crc:
                        break
                    if first and (kind != b"IHDR" or size != 13):
                        break
                    if first and struct.unpack("!II", payload[:8])[0] * struct.unpack("!II", payload[:8])[1] > 100000000:
                        self.add("image_dimension_limit", name, "incomplete")
                    first = False
                    if kind == b"IEND":
                        valid = size == 0 and end == len(data)
                        break
                    offset = end
        elif suffix in {".jpg", ".jpeg"}:
            # Word-generated OOXML thumbnails can carry a single zero padding
            # byte after EOI. Accept bounded zero padding, never an arbitrary
            # appended payload (which still needs independent inspection).
            unpadded = data.rstrip(b"\0")
            valid = data.startswith(b"\xff\xd8\xff") and unpadded.endswith(b"\xff\xd9") and len(data) - len(unpadded) <= 16
        elif suffix == ".gif":
            valid = data.startswith((b"GIF87a", b"GIF89a")) and data.endswith(b";")
        elif suffix == ".webp":
            valid = data.startswith(b"RIFF") and data[8:12] == b"WEBP" and int.from_bytes(data[4:8], "little") + 8 == len(data)
        elif suffix == ".bmp":
            valid = data.startswith(b"BM") and int.from_bytes(data[2:6], "little") == len(data)
        if not valid:
            self.add("invalid_image_format", name, "incomplete")


class _HTML(HTMLParser):
    def __init__(self, inspection, name, depth):
        super().__init__(convert_charrefs=True)
        self.inspection, self.name = inspection, name
        self.depth = depth
        self.password = self.form = False
        self.anchor = None

    def uri(self, target):
        # Browsers ignore tab/newline controls inside schemes. Percent decoding
        # is intentionally conservative: flag even if a particular browser won't.
        cleaned = re.sub(r"[\x00-\x20\x7f]", "", unquote(target))
        try:
            url = urlsplit(cleaned)
            if url.scheme.lower() in ACTIVE_SCHEMES:
                self.inspection.add("active_uri", target)
            if url.username is not None or url.password is not None:
                self.inspection.add("url_userinfo", target)
            return url
        except ValueError:
            self.inspection.add("malformed_link", target)
            return None

    def handle_starttag(self, tag, attrs):
        attrs = {k.lower(): (v or "") for k, v in attrs}
        if tag in {"script", "iframe", "object", "embed", "applet", "foreignobject"}:
            self.inspection.add("active_html", tag)
        if any(k.startswith("on") for k in attrs):
            self.inspection.add("html_event_handler", tag)
        if tag == "input" and (attrs.get("type", "").lower() == "password" or
                attrs.get("autocomplete", "").lower() in {"current-password", "new-password"}):
            self.password = True
        if tag == "form" or "formaction" in attrs:
            self.form = True
            self.inspection.add("html_form", attrs.get("action", attrs.get("formaction", "(current document)")))
        if "srcdoc" in attrs:
            self.inspection.add("active_html_srcdoc", self.name)
            self.inspection.html(attrs["srcdoc"].encode("utf-8"), self.name, depth=self.depth + 1)
        if tag == "meta" and attrs.get("http-equiv", "").lower() == "refresh":
            self.inspection.add("html_redirect", attrs.get("content", ""))
        for key in ("href", "src", "action", "formaction", "xlink:href", "data", "background"):
            if attrs.get(key):
                self.uri(attrs[key])
        if re.search(r"(?:expression\s*\(|url\s*\(\s*[\"']?\s*(?:javascript|data|file):)", attrs.get("style", ""), re.I):
            self.inspection.add("active_css", self.name)
        if tag == "a":
            if self.anchor:
                self.close_anchor()
            self.anchor = [attrs.get("href", ""), ""]

    handle_startendtag = handle_starttag

    def handle_data(self, data):
        if self.anchor:
            self.anchor[1] += data[:4096 - len(self.anchor[1])]

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor:
            self.close_anchor()

    def close_anchor(self):
        target, shown = self.anchor
        self.anchor = None
        actual, visible = self.uri(target), self.uri(shown.strip())
        if actual and visible and actual.hostname and visible.hostname and visible.scheme.lower() in {"http", "https"}:
            if actual.hostname.rstrip(".").casefold() != visible.hostname.rstrip(".").casefold():
                self.inspection.add("link_target_mismatch", json.dumps({"display": shown, "target": target}, ensure_ascii=False))


def _antivirus(data, name, policy):
    adapter = policy.get("av_adapter")
    if adapter is not None:
        result = {"status": "error", "real": False, "source": "injected_test", "revision": "test-only"}
        try:
            supplied = adapter(data, name)
            if not isinstance(supplied, dict) or supplied.get("status") not in {"completed", "error", "incomplete", "unavailable"}:
                raise ValueError("Invalid test adapter result")
            result.update({k: supplied[k] for k in ("status", "detected", "detail") if k in supplied})
        except Exception as exc:
            result["detail"] = type(exc).__name__
        return result
    from sjfx_local_protection.antivirus import scan
    return scan(data=data, name=name, policy=policy)


def inspect_bytes(data: bytes, name: str, policy: dict | None = None) -> dict:
    """Inspect one byte version; callers must bind evidence to its original name.

    ``required_av=False`` is for a caller separately performing real antivirus;
    ``engines.av.status=not_requested`` records that split. Bounds use the DEFAULTS
    keys. ``av_adapter`` can return status/detected/detail, but remains test evidence.
    Changing the name or policy requires fresh evidence even for the same SHA-256.
    """
    if not isinstance(data, bytes) or not isinstance(name, str):
        raise TypeError("data must be bytes and name must be str")
    options = {**DEFAULTS, **(policy or {})}
    for key in ("max_bytes", "max_expanded_bytes", "max_members", "max_depth", "max_html_bytes",
                "max_findings", "max_ratio", "av_timeout", "av_max_bytes", "max_decoder_memory_bytes"):
        if isinstance(options[key], bool) or not isinstance(options[key], (int, float)) or options[key] < 1:
            raise ValueError("Invalid inspection budget: " + key)
        if key != "av_timeout" and not isinstance(options[key], int):
            raise ValueError("Inspection budgets must be integers: " + key)
    if options["max_findings"] < 2 or not isinstance(options["required_av"], bool):
        raise ValueError("Invalid required_av or finding budget")
    context = _Inspection(options)
    if len(data) > options["max_bytes"]:
        context.filename(name)
        context.add("input_size_limit", name, "incomplete")
    else:
        try:
            context.payload(data, name)
        except Exception as exc:
            # A parser implementation failure must never produce no_detection.
            context.add("static_engine_error", type(exc).__name__, "incomplete")
    av = _antivirus(data, name, options) if options["required_av"] or options.get("av_adapter") else {
        "status": "not_requested", "real": False, "source": "external_caller", "revision": "unknown"}
    if av.get("detected"):
        context.add("antivirus_detection", av.get("detail", av["source"]), "dangerous")
    from sjfx_local_protection.antivirus import receipt_complete
    if options["required_av"] and not receipt_complete(av):
        context.add("required_antivirus_incomplete", av.get("detail", av["status"]), "incomplete")
    elif av["status"] in {"error", "incomplete", "unavailable"} and options.get("av_adapter"):
        context.add("antivirus_adapter_incomplete", av.get("detail", av["status"]), "incomplete")
    complete = not context.reasons
    verdict = ("dangerous" if context.dangerous else
               "incomplete" if not complete else "review" if context.findings else "no_detection")
    static_reasons = sorted(context.reasons - {"required_antivirus_incomplete", "antivirus_adapter_incomplete"})
    return {"verdict": verdict, "complete": complete, "findings": context.findings,
        "engines": {"static": {"status": "completed" if not static_reasons else "incomplete",
            "complete": not static_reasons, "revision": REVISION, "formats": sorted(context.formats),
            "incomplete_reasons": static_reasons, "member_count": context.count,
            "expanded_bytes": options["max_expanded_bytes"] - context.remaining,
            "limits": {k: options[k] for k in ("max_bytes", "max_expanded_bytes", "max_members", "max_depth", "max_html_bytes", "max_findings", "max_ratio", "max_decoder_memory_bytes")}},
            "av": av}, "revision": REVISION, "members": context.members,
        "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "mail": context.mail_evidence}
