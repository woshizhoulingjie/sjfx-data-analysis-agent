"""Bounded large-package translation listings without reading document bodies.

The caller authorizes ``package`` before entering this function. The manifest
and translation state remain separate stores; no tables or source data change.
"""
from contextlib import closing
from pathlib import Path


def list_large_translation_page(package, package_store, storage, *, offset=0,
                                limit=100, query="", statuses=None, language="all"):
    package_id = str(package["id"])
    offset = max(0, int(offset or 0))
    limit = max(1, min(500, int(limit or 100)))
    query = str(query or "").strip().casefold()
    wanted = {str(value).strip() for value in (statuses or []) if str(value).strip()}
    wanted = {"not_started" if value == "pending" else value for value in wanted}
    language = str(language or "all").strip().lower()
    # Persisted translations are the small subset of explicitly submitted work.
    # Their sidecar projections contain status/title/progress, never full text.
    translations = {}
    for row in storage.iter_translations(package_id, hydrate=False):
        payload = row["payload"] or {}
        translations[row["path"]] = {
            key: payload[key] for key in (
                "schema_version", "source_language", "target_language", "provider_id",
                "status", "progress", "errors", "updated_at", "original_title",
                "translated_title", "source_level", "full_translation",
            ) if key in payload
        }
    with storage._connect() as connection:
        documents = {row[0] for row in connection.execute(
            "SELECT node_path FROM unified_documents WHERE scan_id=?", (package_id,)
        )}
    items, status_counts, language_counts = [], {}, {}
    matched_total = 0
    # Read only list metadata: quick/deep bodies and catalog decorations are
    # irrelevant to this view and can be much larger than the actual page.
    with closing(package_store._connect()) as connection:
        cursor = connection.execute(
            "SELECT path,name,quick_status,json_extract(local_summary,'$.title') AS title,"
            "COALESCE(NULLIF(json_extract(local_summary,'$.language.code'),''),"
            "NULLIF(json_extract(local_summary,'$.language.language'),''),"
            "CASE WHEN json_type(local_summary,'$.language')='text' "
            "THEN json_extract(local_summary,'$.language') END,'unknown') AS language,"
            "(COALESCE(json_extract(local_summary,'$.sample'),'')<>'' OR "
            "COALESCE(json_extract(local_summary,'$.text_preview'),'')<>'') AS has_preview "
            "FROM (SELECT path,name,quick_status,CASE WHEN json_valid(local_summary) "
            "THEN local_summary ELSE '{}' END AS local_summary "
            "FROM files WHERE package_id=?) ORDER BY path", (package_id,)
        )
        try:
            while True:
                rows = cursor.fetchmany(500)
                if not rows:
                    break
                for row in rows:
                    path = str(row["path"])
                    state = translations.get(path, {})
                    status = str(state.get("status") or "not_started")
                    source_language = str(state.get("source_language") or row["language"] or "unknown")
                    language_key = source_language.lower()
                    status_counts[status] = status_counts.get(status, 0) + 1
                    language_counts[language_key] = language_counts.get(language_key, 0) + 1
                    if wanted and status not in wanted:
                        continue
                    if language not in {"", "all"}:
                        if language == "foreign":
                            if language_key in {"zh", "zh-cn", "zh-hans"}:
                                continue
                        elif language_key != language:
                            continue
                    original_title = str(state.get("original_title") or row["title"] or row["name"] or Path(path).name)
                    if query and query not in " ".join((path, str(row["name"] or ""), original_title,
                                                        str(state.get("translated_title") or ""))).casefold():
                        continue
                    if offset <= matched_total < offset + limit:
                        items.append({
                            **state, "path": path, "status": status,
                            "source_language": source_language,
                            "source_availability": "document" if path in documents else (
                                "preview" if row["has_preview"] else "metadata"),
                            "preview_status": row["quick_status"],
                            "titles": {"original": original_title, "translated": state.get("translated_title")},
                        })
                    matched_total += 1
        finally:
            cursor.close()
    next_offset = offset + len(items)
    return {
        "items": items, "offset": offset, "limit": limit, "total": matched_total,
        "next_offset": next_offset if next_offset < matched_total else None,
        "has_more": next_offset < matched_total,
        "status": next(iter(wanted)) if len(wanted) == 1 else None,
        "statuses": sorted(wanted), "language": language, "query": query,
        "unfiltered_total": sum(status_counts.values()),
        "status_counts": status_counts, "language_counts": language_counts,
        "package_id": package_id,
    }
