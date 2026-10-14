"""CPU document extraction for background QA indexing, never an HTTP request."""
from __future__ import annotations

import multiprocessing as mp
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from services.large_qa_resources import ResourcePressure, resource_code
from services.file_capabilities import IMAGE_EXTENSIONS, file_extension
from services.process_liveness import (
    initialize_process_family, own_process_tree, process_rss_bytes, terminate_process_tree,
)


def _pdf_records(pages, max_chars):
    records=[]; used=0; empty=0
    for page_no,text in pages:
        text=text or ''
        if not text.strip():
            empty+=1
            continue
        remaining=max_chars-used
        if remaining<=0:
            return records,False,'正文达到单文件字符预算，尚有页面未索引。'
        records.append({'text':text[:remaining],'location':{'page':page_no}})
        used+=min(len(text),remaining)
        if len(text)>remaining:
            return records,False,'正文超过字符预算；已有文本已索引。'
    if used and empty:
        return records,False,'部分页面无文本层，需要 OCR；已有文本已索引。'
    if used:
        return records,True,'已索引全部页面的文本层；图表和公式的视觉内容未作完整理解。'
    return [],False,'PDF 没有可用文本层。'


def _pdfium_records(path, max_chars):
    import pypdfium2 as pdfium
    document=pdfium.PdfDocument(str(path))
    def pages():
        for number in range(len(document)):
            page=document[number]
            text_page=None
            try:
                text_page=page.get_textpage()
                yield number+1,text_page.get_text_range()
            finally:
                if text_page is not None:
                    text_page.close()
                page.close()
    iterator=pages()
    try:
        return _pdf_records(iterator,max_chars)
    finally:
        iterator.close()
        document.close()


def _poppler_records(path, max_chars, page_count=None):
    executable=shutil.which('pdftotext')
    if not executable:
        raise ImportError('未安装 pdftotext')
    if page_count is None:
        info=shutil.which('pdfinfo')
        if not info:
            raise ImportError('无法确定 PDF 页数')
        result=subprocess.run([info,str(path)],capture_output=True,timeout=15)
        count=re.search(rb'^Pages:\s*(\d+)',result.stdout,re.M)
        if result.returncode or not count:
            raise ValueError('Poppler 无法读取 PDF 页数')
        page_count=int(count.group(1))
    def pages():
        for number in range(1,page_count+1):
            result=subprocess.run([executable,'-q','-enc','UTF-8','-f',str(number),
                                   '-l',str(number),str(path),'-'],
                                  capture_output=True,timeout=15)
            if result.returncode:
                raise ValueError('Poppler 文本层抽取失败，第 {} 页'.format(number))
            yield number,result.stdout.decode('utf-8','replace')
    return _pdf_records(pages(),max_chars)


def _pdf_text_fallback(path, max_chars, page_count=None):
    failures=[]
    for name,extract in (('PDFium',lambda:_pdfium_records(path,max_chars)),
                         ('Poppler',lambda:_poppler_records(path,max_chars,page_count))):
        try:
            records,complete,note=extract()
            if records:
                return records,complete,'{} 文本层回退；{}'.format(name,note)
            failures.append(name+'无文本层')
        except Exception as exc:
            if resource_code(exc):
                raise
            failures.append(name+':'+type(exc).__name__)
    # A font/read failure must not silently turn into an OCR/model job. Leave
    # explicit failure diagnostics when neither existing text reader succeeds.
    raise ValueError('PDF 文本层回退不可用（{}），未启动 OCR 或模型。'.format('；'.join(failures)))


def _extract(path, relative, max_chars):
    path=Path(path)
    extension = file_extension(path)
    if extension=='.pdf':
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
        page_count=None
        try:
            reader=PdfReader(str(path))
            page_count=len(reader.pages)
            records,complete,note=_pdf_records(
                ((number,page.extract_text() or '') for number,page in enumerate(reader.pages,1)),max_chars)
        except PdfReadError:
            return _pdf_text_fallback(path,max_chars,page_count)
        if records:
            return records,complete,note
        # An entirely scanned PDF uses the existing offline OCR parser on CPU.
    from services.unified_parser import UnifiedDocumentParser
    from config import Config
    parser=UnifiedDocumentParser(Config.DATA_DIR/'large_qa'/'parse_artifacts',Config.RAPIDOCR_MODEL_DIR,max_chars,fast_office_ocr=True)
    mode='accurate' if extension in IMAGE_EXTENSIONS | {'.pdf'} else 'fast'
    result=parser.parse(path,relative,mode=mode)
    evidence=result.get('evidence') or []
    records=[]
    for item in evidence:
        body=item.get('text') or item.get('original_text') or ''
        if not body:
            continue
        location=dict(item.get('location') or {})
        for key in ('page','section','paragraph_index','table','row','row_range','archive_member','char_start','char_end'):
            if item.get(key) is not None:
                location[key]=item[key]
        records.append({'text':body,'location':location})
    # Evidence extraction may itself be bounded. Index the complete cached text
    # too, so a late paragraph cannot disappear just because evidence was capped.
    if result.get('text'):
        records.append({'text':result['text'],'location':{'representation':'extracted_text'}})
    coverage=result.get('coverage') or {}
    complete=bool(coverage.get('complete')) and bool(records)
    note = '；'.join(str(x) for x in result.get('warnings') or [])[:600]
    if not records:
        note = '{}：{}'.format(result.get('parse_status') or 'partial', note or '未提取到可读正文')
    return records,complete,note


def _child(connection,path,relative,max_chars,root=None,doc=None,start_gate=None,parent_pid=None):
    os.environ['CUDA_VISIBLE_DEVICES']=''
    for name in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS'):
        os.environ[name]='1'
    try:
        os.nice(8)
    except (AttributeError,OSError):
        pass
    try:
        if start_gate is not None:
            initialize_process_family()
            if os.name != 'nt' and parent_pid is not None and os.getppid() != parent_pid:
                raise RuntimeError('后台解析所属进程已退出')
            if not start_gate.wait(timeout=30):
                raise RuntimeError('后台解析子进程未获得启动授权')
        if doc and doc.get('source_kind') == 'archive_member':
            from services.large_archive_members import member_snapshot
            with member_snapshot(root, doc) as member_path:
                result = _extract(str(member_path), relative, max_chars)
            connection.send(('ok', result))
        else:
            connection.send(('ok',_extract(path,relative,max_chars)))
    except BaseException as exc:
        code = resource_code(exc)
        if code:
            connection.send(('resource_pressure', {'code': code, 'message': str(exc)[:400]}))
        else:
            connection.send(('error',str(exc)[:400]))
    finally:
        connection.close()


def extract_document(root,doc,timeout=90,memory_mb=1536,max_chars=2_000_000):
    root=Path(root).resolve()
    logical = doc.get('source_kind') == 'archive_member'
    if logical:
        from services.large_archive_members import source_identity
        path, initial_identity = source_identity(root, doc)
    else:
        path=(root/doc['path']).resolve()
    if root not in path.parents or doc.get('sensitive'):
        raise ValueError('文件不在允许范围内或只允许读取元数据')
    before=path.stat()
    if not logical and (before.st_size!=doc['size'] or before.st_mtime_ns!=doc['mtime']):
        raise ValueError('文件已变化，等待清单更新后再提取')
    if int(doc['size'])>256*1024*1024:
        raise ValueError('文件超过 256 MiB 的后台解析预算，需要专门处理')
    ctx=mp.get_context('spawn')
    parent,child=ctx.Pipe()
    start_gate = ctx.Event()
    args = (child,str(path),doc['path'],max_chars,
            str(root) if logical else None, dict(doc) if logical else None, start_gate, os.getpid())
    process=ctx.Process(target=_child,args=args,daemon=True)
    started = False
    job_owner = None
    try:
        process.start(); started = True; child.close()
        # Injected test contexts can expose a mock PID without creating an OS
        # process. Real multiprocessing workers always have an integer PID.
        actual_pid = process.pid
        if isinstance(actual_pid, int):
            job_owner = own_process_tree(actual_pid)
            if os.name == 'nt' and job_owner is None:
                raise ResourcePressure('无法建立 Windows 后台解析子进程回收边界', 'process_ownership')
        start_gate.set()
        start=time.monotonic()
        while time.monotonic()-start<timeout:
            if parent.poll(.15):
                status,value=parent.recv()
                if status=='resource_pressure':
                    raise ResourcePressure(value['message'], value['code'])
                if status!='ok':
                    raise ValueError(value)
                after=path.stat()
                if logical and source_identity(root, doc)[1] != initial_identity:
                    raise ValueError('归档在提取期间变化，已丢弃结果')
                if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
                    raise ValueError('提取过程中源文件变化，已丢弃结果')
                return value
            if not process.is_alive():
                raise ValueError('解析进程退出')
            rss = process_rss_bytes(process.pid)
            if rss is not None and rss > memory_mb*1024*1024:
                raise ResourcePressure('解析达到内存预算', 'memory_exhausted')
        raise TimeoutError('解析达到单文件时间预算')
    finally:
        try:
            if started:
                terminate_process_tree(process, grace_seconds=2, job=job_owner, private_group=True)
        finally:
            try:
                parent.close()
            finally:
                try:
                    child.close()
                finally:
                    try:
                        process.close()
                    except (ValueError, OSError):
                        pass
