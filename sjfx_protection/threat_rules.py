"""Bounded passive checks. Never extract archives, execute content, or follow URLs."""
from __future__ import annotations

import io
import ntpath
import zipfile
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import urlsplit

EXECUTABLE = {'.exe', '.com', '.scr', '.msi', '.dll', '.js', '.jse', '.vbs', '.vbe',
              '.wsf', '.wsh', '.lnk', '.hta', '.bat', '.cmd', '.ps1', '.psm1', '.url', '.reg'}
DOCUMENT = {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt', '.jpg', '.png'}


def finding(rule, detail='', severity='review'):
    return {'rule': rule, 'severity': severity, 'detail': str(detail)[:1000]}


def filename_findings(name):
    base = ntpath.basename(name).strip()
    suffix = ntpath.splitext(base)[1].lower()
    result = []
    if suffix in EXECUTABLE:
        result.append(finding('executable_content', base))
        if ntpath.splitext(ntpath.splitext(base)[0])[1].lower() in DOCUMENT:
            result.append(finding('double_extension', base))
    if any(c in name for c in '‪‫‭‮⁦⁧⁨⁩'):
        result.append(finding('bidirectional_filename', name))
    if suffix in {'.docm', '.xlsm', '.pptm', '.xlam', '.dotm'}:
        result.append(finding('macro_document', base))
    return result


class ActiveHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.findings = []
        self.password = False
        self.submission = False

    def handle_starttag(self, tag, attrs):
        if len(self.findings) >= 80:
            return
        attrs = dict(attrs)
        if tag in {'script', 'iframe', 'object', 'embed'}:
            self.findings.append(finding('active_html', tag))
        if any(k.startswith('on') for k in attrs):
            self.findings.append(finding('html_event_handler', tag))
        if tag == 'input' and (attrs.get('type') or '').lower() == 'password':
            self.password = True
        if tag == 'form':
            self.submission = True
            self.findings.append(finding('html_form', attrs.get('action') or '(current document)'))
        for key in ('href', 'src', 'action', 'formaction'):
            target = attrs.get(key)
            if not target:
                continue
            try:
                url = urlsplit(target.strip())
                if url.scheme.lower() in {'javascript', 'vbscript', 'file', 'data', 'ms-msdt', 'search-ms'}:
                    self.findings.append(finding('active_uri', target))
                if url.username is not None or url.password is not None:
                    self.findings.append(finding('url_userinfo', target))
            except ValueError:
                self.findings.append(finding('malformed_link', target))


def inspect_payload(raw, name, *, content_type='', depth=0, budget=None):
    budget = budget if budget is not None else {'bytes': 8 * 1024 * 1024, 'members': 200}
    result = filename_findings(name)
    suffix = ntpath.splitext(name)[1].lower()
    pe_offset = int.from_bytes(raw[60:64], 'little') if len(raw) >= 64 and raw[:2] == b'MZ' else -1
    executable = (pe_offset >= 64 and raw[pe_offset:pe_offset + 4] == bytes([80, 69, 0, 0])) or (
        raw.startswith(bytes([127, 69, 76, 70])) and len(raw) >= 52 and raw[4] in {1, 2})
    if executable and suffix not in {'.exe', '.dll', '.com', '.scr', '.so'}:
        result.append(finding('disguised_executable', name, 'dangerous'))
    if content_type == 'text/html' or suffix in {'.html', '.htm', '.shtml', '.svg'}:
        parser = ActiveHTML()
        parser.feed(raw[:2 * 1024 * 1024].decode('utf-8', 'replace'))
        result.extend(parser.findings)
        if parser.password and parser.submission:
            result.append(finding('credential_form_attachment', name, 'dangerous'))
        if len(raw) > 2 * 1024 * 1024:
            result.append(finding('html_inspection_limit', name))
    if raw.startswith(b'PK\x03\x04') or suffix == '.zip':
        if depth >= 3:
            return (result + [finding('archive_depth_limit', name)])[:200]
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                for member in archive.infolist():
                    if member.is_dir():
                        continue
                    budget['members'] -= 1
                    if budget['members'] < 0:
                        result.append(finding('archive_member_limit', name))
                        break
                    item = member.filename.replace('\\', '/')
                    if item.startswith('/') or '..' in PurePosixPath(item).parts or ntpath.splitdrive(item)[0]:
                        result.append(finding('archive_path_escape', item, 'dangerous'))
                    result.extend(filename_findings(item))
                    if item.lower().endswith('vbaproject.bin'):
                        result.append(finding('embedded_macro', item))
                    if member.flag_bits & 1:
                        result.append(finding('encrypted_archive_member', item))
                        continue
                    if member.file_size > budget['bytes'] or member.file_size > max(member.compress_size, 1) * 200:
                        result.append(finding('archive_expansion_limit', item))
                        continue
                    with archive.open(member) as stream:
                        payload = stream.read(min(budget['bytes'], member.file_size) + 1)
                    budget['bytes'] -= len(payload)
                    if len(payload) > member.file_size or budget['bytes'] < 0:
                        result.append(finding('archive_expansion_limit', item))
                        break
                    result.extend(inspect_payload(payload, item, depth=depth + 1, budget=budget))
                    if len(result) >= 180:
                        result.append(finding('archive_findings_limit', name))
                        break
        except (OSError, ValueError, RuntimeError, NotImplementedError, zipfile.BadZipFile):
            result.append(finding('unreadable_archive', name))
    return result[:200]
