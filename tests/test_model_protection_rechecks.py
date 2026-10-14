"""A queued model request cannot outlive its source authorization."""
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import test_package_cache_provenance as provenance
from services import model_dispatch
from sjfx_protection import business
from sjfx_protection.store import ProtectionError


class SyntheticModel:
    base_url = 'http://127.0.0.1/synthetic-no-network'
    timeout = 1
    def __init__(self, callback=lambda: None):
        self.calls = 0
        self.callback = callback

    @model_dispatch.serialized_generation
    def generate(self, **kwargs):
        self.calls += 1
        self.callback()
        return 'synthetic result'


class ModelProtectionTests(unittest.TestCase):
    def setUp(self):
        provenance.CacheProvenanceTests.setUp(self)

    def tearDown(self):
        provenance.CacheProvenanceTests.tearDown(self)

    @contextmanager
    def admitted(self, *args):
        yield

    def test_valid_immutable_sources_before_and_after_generation(self):
        model = SyntheticModel()
        with patch.object(model_dispatch, 'generation_slot', self.admitted):
            self.assertEqual(model.generate(), 'synthetic result')
        self.assertEqual(model.calls, 1)
        self.assertEqual(len(self.package.calls), 2)

    def test_account_or_source_revoked_while_waiting_prevents_call(self):
        @contextmanager
        def waiting(*args):
            self.package.blocked.add('A')
            yield
        model = SyntheticModel()
        with patch.object(model_dispatch, 'generation_slot', waiting):
            with self.assertRaises(ProtectionError):
                model.generate()
        self.assertEqual(model.calls, 0)

    def test_output_not_returned_after_authorization_changed(self):
        model = SyntheticModel(lambda: self.package.blocked.add('A'))
        with patch.object(model_dispatch, 'generation_slot', self.admitted):
            with self.assertRaises(ProtectionError):
                model.generate()
        self.assertEqual(model.calls, 1)

    def test_off_existing_model_use_without_context(self):
        self.p.enabled = False
        business.execution.set(None)
        with patch.object(model_dispatch, 'generation_slot', self.admitted):
            self.assertEqual(SyntheticModel().generate(), 'synthetic result')
        self.assertEqual(self.store.opens, 0)

    def test_non_package_general_chat_remains_available(self):
        business.execution.set({'owner': 'alice'})
        with patch.object(model_dispatch, 'generation_slot', self.admitted):
            self.assertEqual(SyntheticModel().generate(), 'synthetic result')
        self.assertEqual(self.package.calls, [])


if __name__ == '__main__':
    unittest.main()
