"""Read-only, metadata-first account material inventory for administrators.

This does not change the workbench's owner/view-as semantics. An account in the
console is always inventoried by its stable stored owner, including the admin's
own account. No scan payload, analysis, model, queue, or source file is loaded.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time


class AdminMaterialCatalog:
    KINDS = ("ordinary", "large", "conversations", "translations")

    def __init__(self, state_db_path, large_db_path):
        self.state_db_path = str(state_db_path)
        self.large_db_path = str(large_db_path)

    @staticmethod
    def _uri(path):
        return Path(path).resolve().as_uri() + "?mode=ro"

    @contextmanager
    def _connect(self):
        # mode=ro cannot create a missing database or write business state.
        conn = sqlite3.connect(self._uri(self.state_db_path), uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("ATTACH DATABASE ? AS large", (self._uri(self.large_db_path),))
            conn.execute("PRAGMA query_only=ON")
            deadline = time.monotonic() + 5
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 20000)
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _empty_stats():
        return dict(ordinary_packages=0, large_packages=0, conversations=0,
                    translations=0, completed_translations=0)

    def summaries(self, owners):
        result = {str(owner): self._empty_stats() for owner in owners}
        if not result:
            return result
        with self._connect() as conn:
            queries = (
                ("ordinary_packages", "SELECT owner_id,COUNT(*) AS n FROM scans GROUP BY owner_id"),
                ("large_packages", "SELECT owner_id,COUNT(*) AS n FROM large.packages GROUP BY owner_id"),
                ("conversations", "SELECT owner_id,COUNT(*) AS n FROM conversations GROUP BY owner_id"),
            )
            for key, query in queries:
                for row in conn.execute(query):
                    if row["owner_id"] in result:
                        result[row["owner_id"]][key] = row["n"]
            # Translation rows belong to their source package, not the viewer.
            rows = conn.execute("""
                SELECT COALESCE(s.owner_id,p.owner_id) AS owner_id,
                       COUNT(*) AS n,
                       SUM(CASE WHEN t.status='completed' THEN 1 ELSE 0 END) AS completed
                FROM document_translations t LEFT JOIN scans s ON s.id=t.scan_id
                LEFT JOIN large.packages p ON p.id=t.scan_id
                WHERE s.id IS NOT NULL OR p.id IS NOT NULL
                GROUP BY COALESCE(s.owner_id,p.owner_id)
            """)
            for row in rows:
                if row["owner_id"] in result:
                    result[row["owner_id"]]["translations"] = row["n"]
                    result[row["owner_id"]]["completed_translations"] = row["completed"]
        return result

    @staticmethod
    def _page(limit, offset):
        return max(1, min(100, int(limit))), max(0, int(offset))

    def list_materials(self, owner_id, kind="ordinary", limit=25, offset=0):
        if kind not in self.KINDS:
            raise ValueError("未知资料类型")
        limit, offset = self._page(limit, offset)
        if kind == "ordinary":
            source = "scans s LEFT JOIN import_tasks i ON i.scan_id=s.id"
            where = "s.owner_id=?"
            columns = ("s.id,s.id AS source_id,s.root_path AS root,"
                       "COALESCE(i.status,'saved') AS status,s.created_at,"
                       "s.created_at AS updated_at")
            order = "s.created_at DESC,s.id DESC"
        elif kind == "large":
            source, where = "large.packages p", "p.owner_id=?"
            columns = ("p.id,p.id AS source_id,p.root_path AS root,p.status,p.phase,"
                       "p.created_at,p.updated_at")
            order = "p.updated_at DESC,p.id DESC"
        elif kind == "conversations":
            source = ("conversations c LEFT JOIN scans s ON s.id=c.scan_id "
                      "LEFT JOIN large.packages p ON p.id=c.scan_id")
            where = "c.owner_id=?"
            columns = ("c.id,c.scan_id AS source_id,c.title AS name,c.status,"
                       "COALESCE(s.root_path,p.root_path,'') AS root,c.created_at,c.updated_at")
            order = "c.updated_at DESC,c.id DESC"
        else:
            source = ("document_translations t LEFT JOIN scans s ON s.id=t.scan_id "
                      "LEFT JOIN large.packages p ON p.id=t.scan_id")
            where = "COALESCE(s.owner_id,p.owner_id)=?"
            columns = ("t.node_path AS id,t.node_path AS name,t.scan_id AS source_id,t.status,"
                       "COALESCE(s.root_path,p.root_path,'') AS root,"
                       "t.updated_at AS created_at,t.updated_at")
            order = "t.updated_at DESC,t.scan_id DESC,t.node_path DESC"
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM " + source + " WHERE " + where,
                                 (str(owner_id),)).fetchone()[0]
            rows = conn.execute("SELECT " + columns + " FROM " + source + " WHERE " + where
                                + " ORDER BY " + order + " LIMIT ? OFFSET ?",
                                (str(owner_id), limit, offset)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["name"] = item.get("name") or str(item.get("root") or "").replace("\\", "/").rstrip("/").split("/")[-1] or item["id"]
            items.append(item)
        return dict(kind=kind, items=items, total=total, limit=limit, offset=offset,
                    has_more=offset + len(items) < total)

    def conversation(self, owner_id, session_id, limit=50, offset=0):
        limit, offset = self._page(limit, offset)
        with self._connect() as conn:
            row = conn.execute("SELECT id,scan_id,title,status,payload FROM conversations "
                               "WHERE id=? AND owner_id=?",
                               (str(session_id), str(owner_id))).fetchone()
            if not row:
                return None
            total = conn.execute("SELECT COUNT(*) FROM conversation_messages WHERE session_id=?",
                                 (str(session_id),)).fetchone()[0]
            if total:
                messages = conn.execute("SELECT payload FROM conversation_messages WHERE session_id=? "
                                        "ORDER BY sequence DESC,created_at DESC,message_id DESC LIMIT ? OFFSET ?",
                                        (str(session_id), limit, offset)).fetchall()
                messages = [json.loads(item["payload"]) for item in reversed(messages)]
            else:
                # Older stored conversations may still keep messages inline.
                legacy = json.loads(row["payload"]).get("messages") or []
                total = len(legacy)
                end = max(0, total - offset)
                messages = legacy[max(0, end - limit):end]
        return dict(session_id=row["id"], source_id=row["scan_id"], title=row["title"],
                    status=row["status"], messages=messages, total=total, limit=limit,
                    offset=offset, has_more=offset + len(messages) < total)
