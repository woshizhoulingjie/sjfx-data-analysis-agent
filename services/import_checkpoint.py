"""Durable model-call checkpoints for preemptible import summaries."""

import hashlib
import json


class CheckpointedModel:
    def __init__(self, model, storage, checkpoint_key):
        self._model = model
        self._storage = storage
        self._checkpoint_key = str(checkpoint_key)
        with storage._connect() as conn:
            columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(import_model_checkpoints)").fetchall()}
            if columns and "checkpoint_key" not in columns:
                conn.execute("ALTER TABLE import_model_checkpoints RENAME TO import_model_checkpoints_legacy")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS import_model_checkpoints ("
                "checkpoint_key TEXT NOT NULL, "
                "request_hash TEXT NOT NULL, payload TEXT NOT NULL, "
                "PRIMARY KEY(checkpoint_key,request_hash))"
            )

    def __getattr__(self, name):
        return getattr(self._model, name)

    def _call(self, method, args, kwargs):
        request = json.dumps(
            [method, getattr(self._model, "model", None), args, kwargs],
            ensure_ascii=False, sort_keys=True,
        )
        key = hashlib.sha256(request.encode("utf-8")).hexdigest()
        with self._storage._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM import_model_checkpoints WHERE checkpoint_key=? AND request_hash=?",
                (self._checkpoint_key, key),
            ).fetchone()
        if row:
            return json.loads(row["payload"])
        result = getattr(self._model, method)(*args, **kwargs)
        # A completed response is committed before the next chunk starts. A
        # preempted in-flight call is retried, but completed calls are reused.
        payload = json.dumps(result, ensure_ascii=False)
        with self._storage._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO import_model_checkpoints VALUES (?,?,?)",
                (self._checkpoint_key, key, payload),
            )
        return result

    def chat_json(self, *args, **kwargs):
        return self._call("chat_json", args, kwargs)

    def chat(self, *args, **kwargs):
        return self._call("chat", args, kwargs)
