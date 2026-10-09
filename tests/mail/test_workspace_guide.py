"""Guidance reports real services and never reads package data or source paths."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from config import Config
from services.mail.analysis_api import FILTERS
from services.mail.api import register_mail_routes
from services.mail.source import AdapterRegistry, NativeAdapter
from services.mail.workspace_guide import workspace_guide
from web_compat import SJFXFastAPI


def services(model=None, queue=None):
    return SimpleNamespace(model=model, job_factory=queue, owner_resolver=lambda: 'owner', repository=object())


@pytest.mark.parametrize('model,queue,available', [(None,None,False),(object(),None,False),(None,object(),False),(object(),object(),True)])
def test_analysis_availability_requires_actual_model_and_queue(model, queue, available):
    guide = workspace_guide(services(model, queue))
    assert {key: guide['runtime'][key] for key in ('model_available', 'queue_available')} == {'model_available': bool(model), 'queue_available': bool(queue)}
    assert guide['runtime']['budgets']['tokens'] == Config.MAIL_RESEARCH_MAX_TOKENS
    assert guide['runtime']['budgets']['limits']['tokens'] >= guide['runtime']['budgets']['tokens']
    assert all(mode['available'] is available for mode in guide['analysis_modes'])
    assert {mode['kind'] for mode in guide['analysis_modes']} == {'research','message','pair','screen'}
    assert all(mode[key] for mode in guide['analysis_modes'] for key in ('purpose','input','output','scope_notes'))


def test_imports_follow_installed_adapter_capabilities_and_scope_contract():
    with patch.object(NativeAdapter, 'available', return_value=False):
        expected = AdapterRegistry().support_matrix()
        guide = workspace_guide(services())
    assert [{key: row[key] for key in ('format','available','dependency')} for row in guide['imports']['formats']] == expected
    assert guide['imports']['path_location'] == 'server'
    assert [field['key'] for field in guide['scope']['fields']] == list(FILTERS)
    formats = {row['format']: row for row in guide['imports']['formats']}
    assert formats['mbox']['extensions'] == ['.mbox','.mbx']
    assert 'EML' in formats['zip']['description'] and 'EML' in formats['directory']['description']
    with patch.object(NativeAdapter, 'available', return_value=True):
        assert all(row['available'] for row in workspace_guide(services())['imports']['formats'])


def test_route_is_read_only_and_feature_guarded(monkeypatch):
    monkeypatch.setattr(Config, 'MAIL_FEATURE_ENABLED', True)
    app = SJFXFastAPI()
    register_mail_routes(app, services())
    with TestClient(app) as client:
        response = client.get('/api/mail/workspace-guide')
        assert response.status_code == 200, response.text
        assert response.json()['ok'] is True
        assert response.headers['cache-control'] == 'no-store, max-age=0'
        # A bare repository sentinel and no policy cannot read rows or paths.
        assert 'source_path' not in response.text and 'allowed_roots' not in response.text
        monkeypatch.setattr(Config, 'MAIL_FEATURE_ENABLED', False)
        assert client.get('/api/mail/workspace-guide').status_code == 404
