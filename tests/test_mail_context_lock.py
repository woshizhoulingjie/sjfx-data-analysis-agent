"""Context cancellation must re-enter the real Storage outside its Lock."""
from pathlib import Path
from unittest.mock import patch
from config import Config

from services.storage import Storage
from services.mail.repository import MailRepository
from services.mail.models import MailMember
from services.mail.parsers.eml import parse_eml
from services.mail.research.context import read_context
from services.mail.research.tools import MailResearchTools


def fixture(tmp_path):
    storage = Storage(tmp_path / "context-lock.sqlite3")
    repo = MailRepository(storage); repo.migrate()
    package, _ = repo.create_package(owner_id="owner", display_name="lock regression",
        source_kind="directory", source_path=str(tmp_path), source_revision="fixture", config={})
    keys = []
    for number in range(3):
        raw = ("From: Alice <alice@example.com>\r\nTo: Bob <bob@example.com>\r\n"
               "Message-ID: <lock-{}@example.com>\r\nSubject: context\r\n"
               "Content-Type: text/plain; charset=utf-8\r\n\r\n"
               "Message {}: conditional approval remains pending.").format(number, number).encode()
        value = repo.upsert_ingested_message(package_id=package["package_id"],
            source={"locator_type":"directory", "root_path":str(tmp_path), "container_path":None,
                    "container_sha256":"fixture", "source_revision":"fixture"},
            member=MailMember(str(number)+".eml", number, len(raw), {"kind":"file", "path":"unused"}),
            raw=raw, parsed=parse_eml(raw))
        keys.append(value["message_key"])
    with repo._connection() as c:
        for number in range(2):
            c.execute("INSERT INTO mail_thread_edges(edge_id,package_id,source_message_key,target_message_key,"
                      "relation_type,source_header,status) VALUES (?,?,?,?,?,?,?)",
                      ("edge"+str(number),package["package_id"],keys[number],keys[number+1],"reply_to","In-Reply-To","exact"))
    job = storage.create_job(package["package_id"], task_type="mail_research", owner_id="owner")
    tools = MailResearchTools(repo, "owner", package["package_id"], context_limit=10000)
    return storage, repo, tools, keys, job


def callback(storage, repo, job, *, cancel_on=None):
    calls = []
    def check():
        # Fail immediately on regression instead of hanging the test runner.
        acquired = storage.lock.acquire(blocking=False)
        assert acquired, "context callback invoked while Storage lock is held"
        storage.lock.release()
        calls.append(len(calls)+1)
        assert storage.get_job(job)["id"] == job
        if cancel_on == len(calls): storage.cancel_job(job)
        return not repo.cancelled(job)
    return check, calls


def test_context_callback_can_read_same_storage_job_each_depth(tmp_path):
    storage, repo, tools, keys, job = fixture(tmp_path)
    check, calls = callback(storage, repo, job)
    value = read_context(tools, keys[0], check=check)
    assert len(calls) >= 3
    assert set(value["review"]["message_keys"]) == set(keys)
    assert value["items"] and "budget_or_cancelled" not in value["review"]["gaps"]
    assert all(item["evidence_id"] for item in value["items"])


def test_context_cancellation_during_bfs_returns_gap_without_lock_reentry(tmp_path):
    storage, repo, tools, keys, job = fixture(tmp_path)
    check, calls = callback(storage, repo, job, cancel_on=2)
    value = read_context(tools, keys[0], check=check)
    assert len(calls) >= 2 and repo.cancelled(job)
    assert "budget_or_cancelled" in value["review"]["gaps"]
    assert value["review"]["complete"] is False and not value["items"]


def test_context_scope_and_depth_caps_survive_unlocked_callbacks(tmp_path):
    storage, repo, tools, keys, job = fixture(tmp_path)
    check, _ = callback(storage, repo, job)
    tools.allowed_keys = frozenset(keys[:2])
    value = read_context(tools, keys[0], max_depth=1, max_messages=2, max_chunks=1, check=check)
    assert set(value["review"]["message_keys"]).issubset(set(keys[:2]))
    assert "depth_limit" in value["review"]["gaps"]
    assert "context_text_limit" in value["review"]["gaps"]
    assert len(value["items"]) == 1


def test_scoped_boundary_fallback_can_read_same_storage_job(tmp_path):
    storage, repo, tools, keys, job = fixture(tmp_path)
    tools.allowed_keys = frozenset(keys[:1])
    check, calls = callback(storage, repo, job)
    tools.continue_check = check
    with patch.object(Config, "MAIL_ENABLE_VECTOR_INDEX", False):
        value = tools._scoped_search("unseen_scope_query")
    # No direct hit forces the eligible-segment boundary scan and its callback.
    assert len(calls) >= 2
    assert value["items"] == []
    assert value["retrieval"]["boundary_scan_truncated"] is False
    assert value["retrieval"]["scope_applied_before_limit"] is True


def test_scoped_boundary_cancellation_preserves_explicit_coverage_gap(tmp_path):
    storage, repo, tools, keys, job = fixture(tmp_path)
    tools.allowed_keys = frozenset(keys[:1])
    check, calls = callback(storage, repo, job, cancel_on=2)
    tools.continue_check = check
    with patch.object(Config, "MAIL_ENABLE_VECTOR_INDEX", False):
        value = tools._scoped_search("unseen_scope_query")
    assert len(calls) == 2 and repo.cancelled(job)
    assert value["items"] == []
    # An interrupted empty scan cannot be interpreted as complete non-match.
    assert value["retrieval"]["boundary_scan_truncated"] is True
