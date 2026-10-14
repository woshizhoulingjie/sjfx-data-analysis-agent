"""Synthetic integrity/provenance tests: no application/LLM/production DB."""
import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from sjfx_protection import business, cache
from sjfx_protection.store import ProtectionError


class FakeStore:
    def __init__(self, path):
        self.path = path
        self.opens = 0

    @contextmanager
    def db(self, write=False):
        self.opens += 1
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()


class FakePackage:
    def __init__(self):
        self.current = {}
        self.calls = []
        self.blocked = set()

    def require_operation(self, owner, registration_id, context, versions, **kwargs):
        self.calls.append((owner, registration_id, copy.deepcopy(context), kwargs))
        expected = self.current.get(registration_id)
        if not expected or registration_id in self.blocked:
            raise ProtectionError("revoked", 423, "package_revoked")
        if owner != expected["owner"] or any(context.get(key) != expected.get(key) for key in
                ("owner", "manifest_revision", "policy_revision", "gap_epoch", "device_id", "versions")):
            raise ProtectionError("stale", 423, "context_stale")
        if any(item not in expected["versions"] for item in versions):
            raise ProtectionError("wrong input", 423, "target_stale")
        return {"context": copy.deepcopy(context)}


def context(registration="A", version="v1", owner="alice"):
    return {"owner": owner, "registration_id": registration, "manifest_revision": "manifest-" + version,
            "policy_revision": "policy-1", "gap_epoch": 0, "device_id": "local-synthetic-fixture", "operation": "analyze",
            "versions": [{"file_id": "file-" + registration, "version_id": version, "sha256": "hash-" + version}]}


class CacheProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = business._configured
        self.ctx = context()
        self.package = FakePackage()
        self.package.current["A"] = copy.deepcopy(self.ctx)
        self.store = FakeStore(Path(self.tmp.name) / "protection.db")
        self.p = SimpleNamespace(enabled=True, package=self.package, store=self.store)
        business.configure(self.p)
        self.token = business.execution.set(self.ctx)
        self.key = cache.resource_key(Path(self.tmp.name) / "agent.db", "summary", "scan-A", "node-1")
        self.payload = {"title": "合同", "body": "完整内容", "nested": [1, False, None]}

    def tearDown(self):
        business.execution.reset(self.token)
        business.configure(self.old)
        self.tmp.cleanup()

    def error(self, code, func, *args, **kwargs):
        with self.assertRaises(ProtectionError) as caught:
            func(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_off_never_opens_or_serializes(self):
        self.p.enabled = False
        poison = object()
        self.assertIs(cache.record_payload(poison, poison, sources=poison), poison)
        self.assertIs(cache.require_payload(poison, poison), poison)
        self.assertIsNone(cache.capture_sources(poison))
        self.assertIsNone(cache.union_sources(poison))
        self.assertEqual(self.store.opens, 0)
        self.assertFalse(self.store.path.exists())

    def test_canonical_mapping_order_and_all_fields(self):
        self.assertEqual(cache.canonical_hash({"a": 1, "b": 2}), cache.canonical_hash({"b": 2, "a": 1}))
        for value in ("1", 1, 1.0, True, b"1", [1], {"value": 1}):
            self.assertEqual(cache.canonical_hash(value), cache.canonical_hash(value))
        self.assertEqual(len({cache.canonical_hash(value) for value in ("1", 1, 1.0, True, b"1", [1], {"value": 1})}), 7)
        self.assertNotEqual(cache.canonical_hash({"body": "x", "title": "one"}),
                            cache.canonical_hash({"body": "x", "title": "two"}))

    def test_namespace_and_subkey_isolation(self):
        self.assertNotEqual(self.key, cache.resource_key(Path(self.tmp.name) / "other.db", "summary", "scan-A", "node-1"))
        self.assertNotEqual(self.key, cache.resource_key(Path(self.tmp.name) / "agent.db", "summary", "scan-A", "node-2"))

    def test_new_payload_roundtrip_and_return_sources(self):
        self.assertIs(cache.record_payload(self.key, self.payload), self.payload)
        self.assertIs(cache.require_payload(self.key, self.payload), self.payload)
        sources = cache.require_payload(self.key, self.payload, return_sources=True)
        self.assertEqual(sources[0]["context"], self.ctx)
        self.assertTrue(all(call[3]["record"] is False for call in self.package.calls))

    def test_unrecorded_historical_payload_never_blessed(self):
        self.error("cache_provenance_missing", cache.require_payload, self.key, self.payload)
        with self.store.db() as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name=?", (cache.TABLE,)).fetchone())

    def test_output_tamper_rejected(self):
        cache.record_payload(self.key, self.payload)
        changed = dict(self.payload, title="篡改")
        self.error("cache_output_mismatch", cache.require_payload, self.key, changed)

    def test_old_v1_not_relabelled_by_current_v2_gate(self):
        cache.record_payload(self.key, self.payload)
        newer = context(version="v2")
        self.package.current["A"] = copy.deepcopy(newer)
        business.execution.set(newer)
        self.error("context_stale", cache.require_payload, self.key, self.payload)

    def test_policy_change_rejected(self):
        cache.record_payload(self.key, self.payload)
        newer = copy.deepcopy(self.ctx)
        newer["policy_revision"] = "policy-2"
        self.package.current["A"] = newer
        business.execution.set(copy.deepcopy(newer))
        self.error("context_stale", cache.require_payload, self.key, self.payload)

    def test_monitor_epoch_change_rejected(self):
        cache.record_payload(self.key, self.payload)
        self.package.current["A"]["gap_epoch"] = 2
        newer = copy.deepcopy(self.package.current["A"])
        business.execution.set(newer)
        self.error("context_stale", cache.require_payload, self.key, self.payload)

    def test_revoked_package_rejected(self):
        cache.record_payload(self.key, self.payload)
        self.package.blocked.add("A")
        self.error("package_revoked", cache.require_payload, self.key, self.payload)

    def test_owner_and_current_scope_checked(self):
        cache.record_payload(self.key, self.payload)
        self.error("cache_provenance_missing", cache.require_payload, self.key, self.payload, owner="bob")
        other = context("B")
        self.package.current["B"] = copy.deepcopy(other)
        business.execution.set(other)
        self.error("cache_scope_mismatch", cache.require_payload, self.key, self.payload)

    def test_saved_source_is_immutable(self):
        sources = cache.capture_sources()
        original = copy.deepcopy(sources)
        self.ctx["versions"][0]["sha256"] = "mutated-context"
        self.assertEqual(sources, original)
        business.execution.set(copy.deepcopy(self.package.current["A"]))
        cache.record_payload(self.key, self.payload, sources)
        sources[0]["context"]["versions"][0]["sha256"] = "mutated-argument"
        self.assertEqual(cache.require_payload(self.key, self.payload, return_sources=True), original)

    def test_union_checks_every_package(self):
        a = cache.capture_sources()
        bctx = context("B")
        self.package.current["B"] = copy.deepcopy(bctx)
        b = cache.capture_sources(bctx)
        sources = cache.union_sources(a, b, a)
        self.assertEqual(len(sources), 2)
        both = {"owner": "alice", "package_contexts": [self.ctx, bctx]}
        business.execution.set(both)
        cache.record_payload(self.key, self.payload, sources)
        cache.require_payload(self.key, self.payload)
        self.package.blocked.add("B")
        self.error("package_revoked", cache.require_payload, self.key, self.payload)

    def test_missing_or_forged_producer_sources_rejected(self):
        self.error("cache_provenance_missing", cache.record_payload, self.key, self.payload, [])
        sources = cache.capture_sources()
        sources[0]["registration_id"] = "B"
        self.error("cache_provenance_missing", cache.record_payload, self.key, self.payload, sources)
        business.execution.set({})
        self.error("cache_provenance_missing", cache.record_payload, self.key, self.payload)

    def test_preserves_member_provenance_on_union(self):
        sources = cache.capture_sources()
        sources[0].update(parent_version_id="container-v1", member_name="mail.eml", derived_version_id="member-v1")
        cache.record_payload(self.key, self.payload, sources)
        got = cache.require_payload(self.key, self.payload, return_sources=True)
        self.assertEqual(got[0]["member_name"], "mail.eml")
        self.assertEqual(got[0]["parent_version_id"], "container-v1")

    def test_sqlite_row_hashes_as_mapping(self):
        with self.store.db() as db:
            row = db.execute("SELECT ? AS body, ? AS title", ("x", "y")).fetchone()
            self.assertEqual(cache.canonical_hash(row), cache.canonical_hash(dict(row)))

    def test_partial_cross_database_write_fails_closed(self):
        cache.record_payload(self.key, self.payload)
        self.error("cache_output_mismatch", cache.require_payload, self.key, {"body": "new output"})

    def test_corrupt_sources_and_contract_rejected(self):
        cache.record_payload(self.key, self.payload)
        with self.store.db(True) as db:
            db.execute("UPDATE " + cache.TABLE + " SET sources_json='not JSON'")
        self.error("cache_provenance_missing", cache.require_payload, self.key, self.payload)
        cache.record_payload(self.key, self.payload)
        with self.store.db(True) as db:
            db.execute("UPDATE " + cache.TABLE + " SET contract_version='unknown'")
        self.error("cache_contract_mismatch", cache.require_payload, self.key, self.payload)

    def test_invalid_nonfinite_hash_fails(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                cache.canonical_hash(value)

    def test_batch_normalizes_manifest_once_and_one_db_transaction(self):
        sources = cache.capture_sources()
        before = self.store.opens
        rows = [(self.key + str(index), {'body': str(index)}, 'alice') for index in range(20)]
        cache.record_payloads(rows, sources)
        self.assertEqual(self.store.opens - before, 1)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM ' + cache.TABLE).fetchone()[0], 20)
            kinds = {row[0]: row[1] for row in db.execute('SELECT kind,COUNT(*) FROM protection_cache_source_objects GROUP BY kind')}
        self.assertEqual(kinds, {'versions': 1, 'context': 1, 'sources': 1})
        self.assertEqual(cache.require_payload(self.key + '7', {'body': '7'}), {'body': '7'})

    def test_normalized_source_object_tamper_refused(self):
        cache.record_payload(self.key, self.payload)
        with self.store.db(True) as db:
            db.execute("UPDATE protection_cache_source_objects SET payload='[]' WHERE kind='versions'")
        self.error('cache_provenance_missing', cache.require_payload, self.key, self.payload)

    def test_normalized_source_object_missing_refused(self):
        cache.record_payload(self.key, self.payload)
        with self.store.db(True) as db:
            db.execute("DELETE FROM protection_cache_source_objects WHERE kind='context'")
        self.error('cache_provenance_missing', cache.require_payload, self.key, self.payload)

    def test_legacy_inline_record_read_without_rewrite(self):
        sources = cache.capture_sources()
        cache.record_payload(self.key, self.payload, sources)
        encoded = json.dumps(sources, ensure_ascii=False)
        with self.store.db(True) as db:
            db.execute('UPDATE ' + cache.TABLE + ' SET sources_json=?', (encoded,))
        cache.require_payload(self.key, self.payload)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT sources_json FROM ' + cache.TABLE).fetchone()[0], encoded)


if __name__ == "__main__":
    unittest.main()
