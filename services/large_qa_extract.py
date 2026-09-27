"""CPU document extraction for background QA indexing, never an HTTP request."""
from __future__ import annotations

import multiprocessing as mp
import os
import time
from pathlib import Path

from services.large_qa_resources import ResourcePressure, resource_code


def _extract(path, relative, max_chars):
    path=Path(path)
    if path.suffix.lower()=='.pdf':
        from pypdf import PdfReader
        reader=PdfReader(str(path))
        records=[]; used=0; empty=0
        for page_no,page in enumerate(reader.pages,1):
            text=page.extract_text() or ''
            if not text.strip():
                empty+=1
            remaining=max_chars-used
            if remaining<=0:
                return records,False,'正文达到单文件字符预算，尚有页面未索引。'
            records.append({'text':text[:remaining],'location':{'page':page_no}})
            used+=len(text)
        if used and empty:
            return records,False,'部分页面无文本层，需要 OCR；已有文本已索引。'
        if used:
            return records,used<=max_chars,'已索引全部页面的文本层；图表和公式的视觉内容未作完整理解。' if used<=max_chars else '正文超过字符预算。'
        # An entirely scanned PDF uses the existing offline OCR parser on CPU.
    from services.unified_parser import UnifiedDocumentParser
    from config import Config
    parser=UnifiedDocumentParser(Config.DATA_DIR/'large_qa'/'parse_artifacts',Config.RAPIDOCR_MODEL_DIR,max_chars,fast_office_ocr=True)
    mode='accurate' if path.suffix.lower() in {'.pdf','.png','.jpg','.jpeg','.tiff','.tif'} else 'fast'
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
    return records,complete,'；'.join(str(x) for x in result.get('warnings') or [])[:600]


def _child(connection,path,relative,max_chars,root=None,doc=None):
    os.environ['CUDA_VISIBLE_DEVICES']=''
    for name in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS'):
        os.environ[name]='1'
    try:
        os.nice(8)
    except (AttributeError,OSError):
        pass
    try:
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
    args = (child,str(path),doc['path'],max_chars)
    if logical:
        args += (str(root), dict(doc))
    process=ctx.Process(target=_child,args=args,daemon=True)
    started = False
    try:
        process.start(); started = True; child.close()
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
            try:
                rss=int(Path('/proc/{}/statm'.format(process.pid)).read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')
                if rss>memory_mb*1024*1024:
                    raise ValueError('解析达到内存预算')
            except (FileNotFoundError,IndexError):
                pass
        raise TimeoutError('解析达到单文件时间预算')
    finally:
        parent.close()
        child.close()
        if started:
            if process.is_alive():
                process.terminate()
            process.join(2)
            if process.is_alive():
                process.kill(); process.join(2)
