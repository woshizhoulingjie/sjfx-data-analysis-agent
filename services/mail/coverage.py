"""Mail-only coverage and immutable result projections."""
from config import Config


def coverage_details(repository,package_id,owner_id,limit=200):
    if not repository.get_package(package_id,owner_id):raise ValueError('邮件资料包不存在')
    with repository._connection() as c:
        rows=[dict(r) for r in c.execute("SELECT DISTINCT p.message_key,p.mime_path,p.filename_raw,p.content_type,COALESCE(x.status,'not_started') AS status,x.warnings_json,x.error_json FROM mail_parts p JOIN mail_occurrences o ON o.message_key=p.message_key LEFT JOIN mail_part_processing x ON x.message_key=p.message_key AND x.mime_path=p.mime_path AND x.stage='basic_text' WHERE o.package_id=? AND p.is_explicit_attachment=1 AND (x.status IS NULL OR x.status<>'ready') ORDER BY status,p.message_key,p.mime_path",(package_id,))]
        facts=c.execute("SELECT COUNT(DISTINCT message_key) FROM mail_fact_processing WHERE package_id=? AND status='ready'",(package_id,)).fetchone()[0]
        total=c.execute('SELECT COUNT(DISTINCT message_key) FROM mail_occurrences WHERE package_id=?',(package_id,)).fetchone()[0]
    counts={}
    for r in rows:counts[r['status']]=counts.get(r['status'],0)+1
    return {'items':rows[:limit],'total':len(rows),'truncated':len(rows)>limit,'by_status':counts,'facts':{'messages':total,'ready':facts,'pending':max(0,total-facts)}}
