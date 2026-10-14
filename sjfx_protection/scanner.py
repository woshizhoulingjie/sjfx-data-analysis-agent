"""Bounded, passive content inspection. No links are followed or macros executed."""
from __future__ import annotations

import email
import email.policy
import hashlib
import json
import math
import os
import re
import subprocess
import struct
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


STREAMING_REVISION = 'sjfx-streaming-passive-20261014-v2'


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def handle_identity(stream, info):
    if os.name!='nt':return '%s:%s'%(info.st_dev,info.st_ino)
    import ctypes
    import msvcrt
    from ctypes import wintypes
    class Information(ctypes.Structure):
        _fields_=[('attributes',wintypes.DWORD),('created',wintypes.FILETIME),
                  ('accessed',wintypes.FILETIME),('written',wintypes.FILETIME),
                  ('volume',wintypes.DWORD),('size_high',wintypes.DWORD),
                  ('size_low',wintypes.DWORD),('links',wintypes.DWORD),
                  ('index_high',wintypes.DWORD),('index_low',wintypes.DWORD)]
    function=ctypes.WinDLL('kernel32',use_last_error=True).GetFileInformationByHandle
    function.argtypes=[wintypes.HANDLE,ctypes.POINTER(Information)]
    function.restype=wintypes.BOOL
    result=Information()
    if not function(msvcrt.get_osfhandle(stream.fileno()),ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    return '%08x:%08x%08x'%(result.volume,result.index_high,result.index_low)


def stable_hash(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        native_identity=handle_identity(stream,before)
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
            "identity": native_identity}


def _run(args, timeout=45):
    try:
        with __import__('tempfile').TemporaryFile() as out:
            result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                    timeout=timeout, check=False)
            out.seek(0)
            raw=out.read(128001)
            if len(raw)>128000:return -1,'扫描器响应超过完整检查记录容量'
            text = raw.decode("utf-8", "replace")
        return result.returncode, text
    except subprocess.TimeoutExpired:
        return -1, "扫描超时"
    except OSError:
        return -1, "扫描器无法运行"


def clam_completion(status,output):
    from sjfx_local_protection.antivirus import parse_receipt
    state=parse_receipt(status,output)['status']
    return 'partial' if state=='incomplete' else state


def _scan_score(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _rspamd_symbols(result):
    if (not isinstance(result, dict) or result.get('is_skipped') is True
            or not _scan_score(result.get('score')) or not isinstance(result.get('symbols'), dict)):
        raise ValueError('Rspamd 未返回完整扫描回执')
    symbols = result['symbols']
    if any(not isinstance(value, dict) or not _scan_score(value.get('score'))
           for value in symbols.values()):
        raise ValueError('Rspamd 规则回执无效')
    return symbols


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


def _small_inspect_file(path, *, name=None, max_bytes=20 * 1024 * 1024, virus_max_bytes=2*1024**3,
                        clam=None, yara=None, yara_rules=None, rspamd=None, timeout=45, static_policy=None):
    from sjfx_local_protection.risk import inspect_bytes
    from sjfx_local_protection.antivirus import scan, receipt_complete
    path=Path(path)
    raw=bounded_read(path,max_bytes)
    modern=inspect_bytes(raw,str(name or path.name),{**(static_policy or {}),'required_av':False,'max_bytes':max_bytes})
    findings=list(modern['findings'])
    engines={'static':'completed' if modern['complete'] else 'incomplete',
             'local_static':modern['engines']['static']}
    av=scan(data=raw,name=str(name or path.name),policy={'clamav_path':clam,
            'av_max_bytes':virus_max_bytes,'av_timeout':timeout})
    engines['clamav']='completed' if receipt_complete(av) else 'partial' if av['status'] in {'completed','incomplete'} else av['status']
    engines['clamav_receipt']=av
    if av.get('detected'):
        findings.append({'rule':'clamav','severity':'dangerous','detail':av.get('detections',av.get('detail',''))})
    if yara and yara_rules:
        status,output=_run([yara,'scan',str(yara_rules),str(path)],timeout)
        engines['yara_x']='completed' if status==0 else 'error'
        if status==0 and output.strip():findings.append({'rule':'yara_x','severity':'review','detail':output[:8000]})
    elif yara or yara_rules:
        engines['yara_x']='error'
    if rspamd and path.suffix.lower()=='.eml':
        parsed=urlsplit(rspamd)
        if parsed.hostname not in {'127.0.0.1','localhost','::1'} or parsed.scheme not in {'http','https'}:
            raise ValueError('Rspamd 只允许本机扫描地址')
        try:
            with build_opener(_NoRedirect()).open(Request(rspamd.rstrip('/')+'/checkv2',data=raw,headers={'Content-Type':'message/rfc822'}),timeout=30) as response:
                payload=response.read(1024*1024+1)
            if len(payload)>1024*1024:raise ValueError('Rspamd response budget exhausted')
            response=json.loads(payload)
            symbols = _rspamd_symbols(response)
            engines['rspamd']='completed'
            for label,value in symbols.items():
                if value.get('score',0)>0:findings.append({'rule':'rspamd:'+label,'severity':'review','detail':value.get('options',[]),'score':value.get('score')})
        except Exception:engines['rspamd']='error'
    return {'risk':modern['verdict'],'findings':findings,'engines':engines,'members':modern['members'],
            'mail':modern.get('mail',[]),'sha256':modern['sha256'],'static_revision':modern['revision']}


def inspect_file(path, *, max_bytes=20*1024*1024, virus_max_bytes=2*1024**3,
                 timeout=300, required_engines=('static','clamav'),
                 clam=None, yara=None, yara_rules=None, rspamd=None, name=None, static_policy=None):
    """Separate hashing, static and antivirus capacities with honest coverage.

    Large UTF-8 data text uses a streaming passive checker and full-file antivirus.
    Large complex containers need an adequate engine; no human review erases limits.
    """
    path=Path(path);size=path.stat().st_size
    if size<=max_bytes:
        result=_small_inspect_file(path,name=name,max_bytes=max_bytes,virus_max_bytes=virus_max_bytes,clam=clam,yara=yara,yara_rules=yara_rules,rspamd=rspamd,timeout=timeout,static_policy=static_policy)
    else:
        result={'risk':'unchecked','findings':[],'engines':{}}
        from sjfx_local_protection.risk import _Inspection, DEFAULTS
        names=_Inspection(DEFAULTS);names.filename(str(name or path.name))
        result['findings'].extend(names.findings)
        if path.suffix.lower() in {'.txt','.csv','.tsv','.jsonl','.log'}:
            import codecs
            import time
            deadline=time.monotonic()+timeout
            decoder=codecs.getincrementaldecoder('utf-8')('strict');tail='';formula=False;active=False
            hasher=hashlib.sha256()
            try:
                with path.open('rb') as incoming:
                    for block in iter(lambda:incoming.read(1024*1024),b''):
                        if time.monotonic()>deadline:raise ValueError('static timeout')
                        hasher.update(block)
                        text=decoder.decode(block)
                        if '\x00' in text:raise ValueError('binary text')
                        active=active or bool(re.search(r'<(?:!doctype\s+html|html|script|iframe|form|svg|object)\b',tail+text,re.I))
                        if path.suffix.lower() in {'.csv','.tsv'}:
                            formula=formula or bool(re.search(r'(?:^|[\n,\t])[ \r\"]*[=@+\-]',tail+text))
                        tail=text[-8192:]
                    decoder.decode(b'',final=True)
                result['engines']['static']='completed_streaming_text'
                result['sha256']=hasher.hexdigest()
                if active:
                    result['engines']['static']='incomplete_active_text'
                    result['findings'].append({'rule':'large_active_text_requires_parser','severity':'incomplete','detail':'大文本包含活动文档标记，必须使用足够覆盖的解析通道'})
                if formula:result['findings'].append({'rule':'spreadsheet_formula','severity':'review','detail':'导入或导出到电子表格时需禁用活动公式'})
            except (UnicodeError,ValueError,OSError):result['engines']['static']='incomplete_text_format'
        else:
            result['engines']['static']='skipped_size_limit'
            result['findings'].append({'rule':'large_format_unsupported','severity':'incomplete','detail':'大文件格式缺少足够覆盖的静态检查通道'})
        from sjfx_local_protection.antivirus import scan, receipt_complete
        if size>virus_max_bytes:result['engines']['clamav']='skipped_size_limit'
        else:
            av=scan(path=path,name=str(name or path.name),policy={'clamav_path':clam,
                'av_max_bytes':virus_max_bytes,'av_timeout':timeout})
            result['engines']['clamav']='completed' if receipt_complete(av) else 'partial' if av['status'] in {'completed','incomplete'} else av['status']
            result['engines']['clamav_receipt']=av
            if result.get('sha256') and av.get('input_sha256') and result['sha256']!=av['input_sha256']:
                result['engines']['clamav']='partial'
                result['findings'].append({'rule':'inspection_version_mismatch','severity':'incomplete','detail':'静态检查和病毒检查读取了不同版本'})
            if av.get('detected'):result['findings'].append({'rule':'clamav','severity':'dangerous','detail':av.get('detections',av.get('detail',''))})
        if yara and yara_rules:
            status,output=_run([yara,'scan',str(yara_rules),str(path)],timeout)
            result['engines']['yara_x']='completed' if status==0 else 'error'
            if status==0 and output.strip():result['findings'].append({'rule':'yara_x','severity':'review','detail':output[:8000]})
        elif yara or yara_rules:
            result['engines']['yara_x']='error'
    findings=result['findings'];engines=result['engines']
    incomplete_rules={'html_inspection_limit','archive_depth_limit','archive_member_limit','encrypted_archive_member',
                      'archive_expansion_limit','archive_findings_limit','unreadable_archive','msg_requires_mail_engine',
                      'unsupported_format','invalid_format','legacy_office_requires_engine'}
    for finding in findings:
        if finding.get('rule') in incomplete_rules and finding.get('severity')!='dangerous':finding['severity']='incomplete'
    completed=lambda value:isinstance(value,str) and value in {'completed','completed_streaming_text'}
    missing=[engine for engine in required_engines if not completed(engines.get(engine))]
    missing.extend(engine for engine,state in engines.items() if isinstance(state,str) and state in {'error','partial','partial_part_limit','partial_charset'} and engine not in missing)
    incomplete=[f['rule'] for f in findings if f.get('severity')=='incomplete']
    actual=actual_format(path) if size>max_bytes else {'format':','.join(engines['local_static']['formats']), 'complete':engines['static']=='completed','reason':'static_incomplete'}
    if actual.get('complete') is not True:incomplete.append(actual['reason'])
    complete=not missing and not incomplete
    if any(f.get('severity')=='dangerous' for f in findings):risk='dangerous'
    elif not complete:risk='incomplete'
    elif any(f.get('severity')=='review' for f in findings):risk='review'
    else:risk='no_detection'
    result.update(risk=risk,coverage={'complete':complete,'required_engines':list(required_engines),
        'missing_engines':missing,'incomplete_reasons':incomplete,'actual_format':actual.get('format'),
        'mail':result.get('mail',[]),'size':size,'static_capacity':max_bytes,'virus_capacity':virus_max_bytes,
        'rules_revision':result.get('static_revision',STREAMING_REVISION),'path':'streaming_large_text' if size>max_bytes else 'bounded_passive'})
    return result

def actual_format(path):
    path=Path(path);suffix=path.suffix.lower()
    with path.open('rb') as incoming:head=incoming.read(4096)
    common={'.txt','.csv','.tsv','.json','.jsonl','.xml','.log','.html','.htm','.shtml','.svg','.eml','.md','.rtf'}
    if suffix in common:
        if b'\x00' in head:return {'format':'binary','complete':False,'reason':'invalid_format'}
        if head.startswith((b'MZ',b'\x7fELF',b'PK\x03\x04')):return {'format':'disguised_binary','complete':False,'reason':'invalid_format'}
        return {'format':'text','complete':True}
    signatures={'.pdf':b'%PDF-', '.png':b'\x89PNG\r\n\x1a\n','.jpg':b'\xff\xd8\xff','.jpeg':b'\xff\xd8\xff',
                '.gif':b'GIF8','.zip':b'PK', '.docx':b'PK','.xlsx':b'PK','.pptx':b'PK','.docm':b'PK','.xlsm':b'PK','.pptm':b'PK'}
    if suffix in signatures:
        valid=head.startswith(signatures[suffix])
        if valid and suffix=='.pdf':
            with path.open('rb') as incoming:
                incoming.seek(max(0,path.stat().st_size-2048));valid=b'%%EOF' in incoming.read()
        if valid and suffix in {'.jpg','.jpeg','.gif'}:
            with path.open('rb') as incoming:
                incoming.seek(max(0,path.stat().st_size-2));tail=incoming.read()
            valid=tail.endswith(b'\xff\xd9') if suffix in {'.jpg','.jpeg'} else tail.endswith(b';')
        if valid and suffix=='.png':
            import zlib
            with path.open('rb') as incoming:
                incoming.read(8);seen_end=False;chunks=0
                while chunks<100000:
                    record=incoming.read(8)
                    if len(record)!=8:break
                    length=struct.unpack('!I',record[:4])[0];kind=record[4:];chunks+=1
                    if length>20*1024*1024:break
                    payload=incoming.read(length);crc=incoming.read(4)
                    if len(payload)!=length or len(crc)!=4 or zlib.crc32(kind+payload)&0xffffffff!=struct.unpack('!I',crc)[0]:break
                    if kind==b'IHDR' and (length!=13 or struct.unpack('!II',payload[:8])[0]*struct.unpack('!II',payload[:8])[1]>100000000):break
                    if kind==b'IEND':seen_end=length==0;break
                valid=seen_end
        if valid and suffix in {'.docx','.xlsx','.pptx','.docm','.xlsm','.pptm'}:
            import zipfile
            mandatory='word/document.xml' if suffix in {'.docx','.docm'} else 'xl/workbook.xml' if suffix in {'.xlsx','.xlsm'} else 'ppt/presentation.xml'
            try:
                with zipfile.ZipFile(path) as container:valid={'[Content_Types].xml',mandatory}.issubset(set(container.namelist()))
            except (OSError,ValueError,zipfile.BadZipFile):valid=False
        return {'format':suffix[1:],'complete':valid,'reason':'invalid_format'}
    if suffix in {'.doc','.xls','.ppt','.msg','.ole'}:return {'format':'ole','complete':False,'reason':'legacy_office_requires_engine'}
    if suffix in {'.exe','.dll','.com','.scr','.js','.vbs','.ps1','.bat','.cmd','.hta','.lnk','.so'}:
        return {'format':'executable_or_script','complete':True}
    return {'format':'unknown','complete':False,'reason':'unsupported_format'}


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
