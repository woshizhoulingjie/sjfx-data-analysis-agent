"""Temp-DB checks for a drained naming lane promoting its own leaf lease."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from services import large_package_catalog_coordination as coordination


class Store:
    def __init__(self, path):
        self.db_path = str(path)
        self._lock = threading.RLock()

    def _connect(self):
        return sqlite3.connect(self.db_path, timeout=2)


class TopicWindowLeaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "catalog.db")
        self.package = "synthetic-package"
        with closing(self.store._connect()) as conn:
            conn.execute("CREATE TABLE packages(id TEXT PRIMARY KEY,status TEXT,control_state TEXT)")
            conn.execute("CREATE TABLE lp_catalog_jobs(package_id TEXT,status TEXT)")
            conn.execute("INSERT INTO packages VALUES(?,'waiting_for_selection','active')", (self.package,))
            coordination.init(conn)
            conn.commit()
        self.env = patch.dict(os.environ, {"SJFX_LP_TOPIC_DRAIN_SECONDS": "0", "SJFX_LP_TOPIC_WINDOW_SECONDS": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        # Process identity is outside these lease/drain tests. Keep it stable
        # while exercising the real context managers and temporary SQLite DB.
        process = SimpleNamespace(create_time=lambda: 1.0, is_running=lambda: True)
        metadata = patch.dict(sys.modules, {"psutil": SimpleNamespace(Process=lambda *args: process)})
        metadata.start()
        self.addCleanup(metadata.stop)

    def round(self):
        return {"deadline": time.time() + 2}

    def rows(self, table):
        with closing(self.store._connect()) as conn:
            return conn.execute("SELECT * FROM " + table).fetchall()

    def test_single_drained_lane_can_promote_its_exact_lease(self):
        with coordination.leaf_batch(self.store, self.package, "naming-single") as allowed:
            self.assertTrue(allowed)
            with coordination.topic_window(self.store, self.package, self.round()) as context:
                self.assertIsNotNone(context)
                self.assertEqual(len(self.rows("lp_catalog_mutations")), 1)
                context["outcome"] = "completed"
        self.assertEqual(self.rows("lp_catalog_mutations"), [])

    def test_batch_to_single_nested_lane_shares_and_promotes_one_lease(self):
        with coordination.leaf_batch(self.store, self.package, "naming-batch"):
            with coordination.leaf_batch(self.store, self.package, "naming-single"):
                self.assertEqual(len(self.rows("lp_catalog_mutations")), 1)
                with coordination.topic_window(self.store, self.package, self.round()) as context:
                    self.assertIsNotNone(context)
                    context["outcome"] = "completed"

    def test_foreign_thread_lease_still_blocks_own_promotion(self):
        entered, release = threading.Event(), threading.Event()
        def foreign():
            with coordination.leaf_batch(self.store, self.package, "foreign") as allowed:
                if allowed:
                    entered.set()
                    release.wait(2)
        thread = threading.Thread(target=foreign)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            with coordination.leaf_batch(self.store, self.package, "naming-single"):
                with coordination.topic_window(self.store, self.package, self.round()) as context:
                    self.assertIsNone(context)
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_same_database_other_store_identity_cannot_borrow_lease(self):
        other_store = Store(Path(self.store.db_path))
        with coordination.leaf_batch(self.store, self.package):
            with coordination.topic_window(other_store, self.package, self.round()) as context:
                self.assertIsNone(context)

    def test_other_package_thread_local_key_does_not_exempt_foreign_token(self):
        other = "other-synthetic-package"
        with closing(self.store._connect()) as conn:
            conn.execute("INSERT INTO packages VALUES(?,'waiting_for_selection','active')", (other,))
            conn.commit()
        with coordination.leaf_batch(self.store, other):
            token = coordination._local.leaves[(id(self.store), other)]
            with closing(self.store._connect()) as conn:
                conn.execute("INSERT INTO lp_catalog_mutations VALUES(?,?,?,?)",
                             ("foreign-current", self.package, token, time.time() + 1))
                conn.commit()
            with coordination.topic_window(self.store, self.package, self.round()) as context:
                self.assertIsNone(context)

    def test_running_naming_job_blocks_even_with_own_lease(self):
        with closing(self.store._connect()) as conn:
            conn.execute("INSERT INTO lp_catalog_jobs VALUES(?,'running')", (self.package,))
            conn.commit()
        with coordination.leaf_batch(self.store, self.package):
            with coordination.topic_window(self.store, self.package, self.round()) as context:
                self.assertIsNone(context)

    def test_active_topic_window_blocks_nested_window_and_new_leaf_claims(self):
        with coordination.topic_window(self.store, self.package, self.round()) as context:
            self.assertIsNotNone(context)
            with coordination.topic_window(self.store, self.package, self.round()) as nested:
                self.assertIsNone(nested)
            with coordination.leaf_batch(self.store, self.package) as allowed:
                self.assertFalse(allowed)
            context["outcome"] = "completed"

    def test_paused_and_cancelled_control_remain_blocked(self):
        for state in ("paused", "cancelled"):
            with self.subTest(state=state):
                with closing(self.store._connect()) as conn:
                    conn.execute("UPDATE packages SET status=?,control_state=? WHERE id=?", (state, state, self.package))
                    conn.execute("DELETE FROM lp_catalog_windows")
                    conn.commit()
                with coordination.leaf_batch(self.store, self.package):
                    with coordination.topic_window(self.store, self.package, self.round()) as context:
                        self.assertIsNone(context)

    def test_expired_round_does_not_promote_or_create_topic_window(self):
        with coordination.leaf_batch(self.store, self.package):
            with coordination.topic_window(self.store, self.package, {"deadline": time.time() - 1}) as context:
                self.assertIsNone(context)
        self.assertEqual(self.rows("lp_catalog_windows"), [])

    def test_exception_releases_window_then_outer_lease(self):
        with self.assertRaisesRegex(ValueError, "synthetic"):
            with coordination.leaf_batch(self.store, self.package):
                with coordination.topic_window(self.store, self.package, self.round()) as context:
                    self.assertIsNotNone(context)
                    raise ValueError("synthetic")
        self.assertEqual(self.rows("lp_catalog_mutations"), [])
        with closing(self.store._connect()) as conn:
            row = conn.execute("SELECT expires,active FROM lp_catalog_windows WHERE package_id=?", (self.package,)).fetchone()
        self.assertEqual(row, (0.0, 0))
        self.assertNotIn((id(self.store), self.package), coordination._local.leaves)


if __name__ == "__main__":
    unittest.main()
