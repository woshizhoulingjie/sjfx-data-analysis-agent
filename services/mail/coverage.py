"""Package-scoped stage coverage; successful text does not hide OCR gaps."""
import json
from .text_provenance import decode_processing_warnings


_LABELS = {'not_started':'待处理','pending':'待处理','running':'运行中','ready':'成功','partial':'部分成功',
    'waiting_retry':'等待重试','terminal_failed':'终止失败','failed':'失败','skipped':'跳过','restricted':'受限',
    'unsupported':'不支持','ocr_pending':'等待 OCR','ocr_no_text':'OCR 未得到文本'}


def _decoded(value,default):
    try:return json.loads(value) if value else default
    except (ValueError,TypeError):return default


def coverage_details(repository,package_id,owner_id,limit=200):
    if not repository.get_package(package_id,owner_id):raise ValueError('邮件资料包不存在')
    limit=max(1,min(1000,int(limit)))
    with repository._connection() as c:
        rows=[dict(r) for r in c.execute("SELECT DISTINCT p.message_key,p.mime_path,p.filename_raw,p.content_type,"
            "COALESCE(x.status,'not_started') AS basic_status,x.warnings_json,x.error_json,x.retry_count,x.next_retry_at,x.extracted_chars,x.coverage_ratio,"
            "COALESCE(y.status,'not_started') AS ocr_status,y.warnings_json AS ocr_warnings_json,y.error_json AS ocr_error_json,y.retry_count AS ocr_retry_count,y.next_retry_at AS ocr_next_retry_at,"
            "y.coverage_ratio AS ocr_coverage_ratio,y.status IS NOT NULL AS ocr_started,"
            "(COALESCE(x.extracted_chars,0)>0 OR EXISTS (SELECT 1 FROM mail_text_segments ts WHERE ts.package_id=o.package_id AND ts.message_key=p.message_key AND ts.mime_path=p.mime_path AND ts.source_kind='attachment' AND LENGTH(ts.text)>0)) AS has_text "
            "FROM mail_parts p JOIN mail_visible_occurrences o ON o.message_key=p.message_key "
            "LEFT JOIN mail_part_processing x ON x.message_key=p.message_key AND x.mime_path=p.mime_path AND x.stage='basic_text' "
            "LEFT JOIN mail_part_processing y ON y.message_key=p.message_key AND y.mime_path=p.mime_path AND y.stage='ocr' "
            "WHERE o.package_id=? AND p.is_explicit_attachment=1 ORDER BY p.message_key,p.mime_path",(package_id,))]
        facts=c.execute("SELECT COUNT(DISTINCT f.message_key) FROM mail_fact_processing f WHERE f.package_id=? AND f.status='ready' AND EXISTS (SELECT 1 FROM mail_visible_occurrences o WHERE o.package_id=f.package_id AND o.message_key=f.message_key)",(package_id,)).fetchone()[0]
        fact_states={r[0]:r[1] for r in c.execute('SELECT status,COUNT(*) FROM mail_fact_processing WHERE package_id=? GROUP BY status',(package_id,))}
        total=c.execute('SELECT COUNT(DISTINCT message_key) FROM mail_visible_occurrences WHERE package_id=?',(package_id,)).fetchone()[0]
    basic_counts={};ocr_counts={};counts={};gaps=[]
    for row in rows:
        basic=row['basic_status'];ocr=row['ocr_status'];basic_counts[basic]=basic_counts.get(basic,0)+1
        ocr_required=bool(row['ocr_started'] or basic in {'ocr_pending','ocr_no_text'})
        if ocr_required:ocr_counts[ocr]=ocr_counts.get(ocr,0)+1
        gap_stages=[]
        if basic!='ready' or (row['coverage_ratio'] is not None and row['coverage_ratio']<1):gap_stages.append('basic_text')
        if ocr_required and ocr!='skipped' and (ocr!='ready' or (row['ocr_coverage_ratio'] is not None and row['ocr_coverage_ratio']<1)):gap_stages.append('ocr')
        if not gap_stages:continue
        effective=basic if 'basic_text' in gap_stages else ocr
        if effective=='ready':effective='partial'
        row.update(status=effective,status_label=_LABELS.get(effective,effective),gap_stages=gap_stages,ocr_required=ocr_required,
            warnings=decode_processing_warnings(row['warnings_json'])[0],error=_decoded(row['error_json'],None),
            ocr_warnings=decode_processing_warnings(row['ocr_warnings_json'])[0],ocr_error=_decoded(row['ocr_error_json'],None))
        counts[effective]=counts.get(effective,0)+1;gaps.append(row)
    return {'items':gaps[:limit],'total':len(gaps),'truncated':len(gaps)>limit,'by_status':counts,
        'denominator':'unique_message_mime_attachment','attachments':{'total':len(rows),'by_status':basic_counts},
        'attachments_without_text':sum(not row['has_text'] for row in rows),
        'stages':{'basic_text':{'total':len(rows),'by_status':basic_counts},'ocr':{'total':sum(ocr_counts.values()),'by_status':ocr_counts}},
        'facts':{'messages':total,'ready':facts,'pending':max(0,total-facts),'processing_records_by_status':fact_states}}
