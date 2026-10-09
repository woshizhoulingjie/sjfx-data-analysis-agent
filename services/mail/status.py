"""Shared, inexpensive list indicators; original bytes and extracted text differ."""


def message_status_columns(alias='m'):
    if alias not in {'m'}:
        raise ValueError('Unsupported SQL alias')
    return (
        "EXISTS(SELECT 1 FROM mail_raw_store rs WHERE rs.raw_sha256=m.raw_sha256) AS original_preserved,"
        "(m.body_preferred_part IS NULL) AS body_empty,"
        "(m.sent_at_utc IS NULL) AS date_unknown,"
        "(SELECT COUNT(*) FROM mail_parts qp LEFT JOIN mail_part_processing qx "
        "ON qx.message_key=qp.message_key AND qx.mime_path=qp.mime_path AND qx.stage='basic_text' "
        "WHERE qp.message_key=m.message_key AND qp.is_explicit_attachment=1 AND "
        "(COALESCE(qx.status,'not_started')!='ready' OR COALESCE(qx.coverage_ratio,1)<1 OR EXISTS("
        "SELECT 1 FROM mail_part_processing qo WHERE qo.message_key=qp.message_key AND qo.mime_path=qp.mime_path "
        "AND qo.stage='ocr' AND qo.status!='ready'))) AS attachment_incomplete"
    )
