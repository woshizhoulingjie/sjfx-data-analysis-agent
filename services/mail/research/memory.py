"""Repository-backed session helpers; no cross-package conversational state."""

from __future__ import annotations


def current_scope(repository, session_id, owner_id):
    session = repository.get_session(session_id, owner_id)
    return (session or {}).get("scope") or {}
