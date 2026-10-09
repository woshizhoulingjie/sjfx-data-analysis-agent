"""Extraction provenance stored compatibly in typed processing metadata.

No schema migration is needed. Existing human warnings remain strings; the
repository removes the typed metadata entry before exposing warning lists.
"""
from __future__ import annotations

import hashlib
import json

VERSION = 'mail-text-provenance-v1'
OCR_NOTICE = '扫描件文字由 OCR 识别，可能漏字或误识日期、数字与条件；识别完成和页覆盖不代表逐字准确，请回看原图。'


def decode_processing_warnings(value):
    try:
        rows=json.loads(value) if isinstance(value,str) else value or []
    except (TypeError,ValueError):
        rows=[]
    if not isinstance(rows,list):rows=[]
    metadata=next((row for row in reversed(rows) if isinstance(row,dict) and row.get('kind')==VERSION),{})
    return [row for row in rows if not (isinstance(row,dict) and row.get('kind')==VERSION)],metadata


def legacy_origin(parser):
    name=str(parser or '').casefold()
    if 'ocr' in name:return 'ocr'
    if name in {'pypdf','python-docx','openpyxl','xlrd','mail-text'}:return 'native'
    return 'unknown'


def location(unit):
    return unit.get('page_number'),unit.get('sheet_name'),unit.get('cell_range')


def processing_metadata(result,stage):
    default='ocr' if stage=='ocr' else legacy_origin(result.get('parser'))
    units=[]
    for unit in result.get('units') or [{'text':result.get('text') or ''}]:
        text=str(unit.get('text') or '')
        units.append({**{k:unit[k] for k in ('page_number','sheet_name','cell_range','ocr_language','language_fallback',
                    'ocr_quality','render_dpi') if k in unit},
            'text_origin':unit.get('text_origin') or default,
            'extraction_method':unit.get('extraction_method') or result.get('parser'),
            'text_sha256':hashlib.sha256(text.encode('utf-8')).hexdigest()})
    origins={u['text_origin'] for u in units}
    return {'kind':VERSION,'text_origin':next(iter(origins)) if len(origins)==1 else 'mixed',
            'units':units,'coverage_basis':(result.get('meta') or {}).get('coverage_basis'),
            'recognition_accuracy':'unverified' if 'ocr' in origins else 'not_measured'}


def provenance_for_unit(metadata,unit,parser=None):
    matched=[u for u in metadata.get('units') or [] if location(u)==location(unit)]
    row=matched[0] if len(matched)==1 else {}
    origin=row.get('text_origin') or legacy_origin(parser)
    value={'text_origin':origin,'extraction_method':row.get('extraction_method') or parser}
    if origin=='ocr':
        quality=row.get('ocr_quality') or {'confidence_available':False,'recognition_accuracy':'unverified'}
        # Lines and image coordinates are available in processing metadata.
        # Model context gets compact quality facts, never a fidelity guarantee.
        value['ocr_quality']={k:v for k,v in quality.items() if k!='lines'}
        value['source_text_notice']=OCR_NOTICE
    return value
