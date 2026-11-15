import json
import pytest
from tools.workbench_server import Store, InputError
from test_workbench_server import service, request
from test_workbench_external import catalog, enable


def test_catalog_read_and_csrf_do_not_start_detection(service, monkeypatch):
    monkeypatch.setattr('tools.workbench_discovery._run_probe', lambda *a, **k: pytest.fail('probe started'))
    status, _, raw = request(service, 'GET', '/api/external/agents')
    assert status == 200 and json.loads(raw)['entries'] == []
    assert request(service, 'POST', '/api/external/discover', '{}', {'Content-Type':'application/json'})[0] == 403
    _, _, raw = request(service, 'GET', '/api/bootstrap')
    headers = {'Content-Type':'application/json','X-Workbench-Token':json.loads(raw)['csrf_token']}
    status, _, raw = request(service, 'POST', '/api/external/discover', '{}', headers)
    assert status == 200 and all(not r['authorized'] for r in json.loads(raw)['entries'])


def test_server_selects_codex_without_qoder_and_rechecks_login(tmp_path, catalog):
    manager, _, _ = catalog
    store = Store(tmp_path/'store')
    store.external_agents = manager
    enable(manager)
    store._select_external()
    first = store.agent.external
    assert first.source == 'codex'
    store.external_action('probe', {'entry_id':'entry-a'})
    assert store.agent.external is not first
    assert store.external_configuration['entry_id'] == 'entry-a'
    store.external_action('revoke', {'entry_id':'entry-a'})
    assert store.agent.external is None


def test_active_generation_prevents_probe_or_switch(tmp_path, catalog):
    manager, _, _ = catalog
    store = Store(tmp_path/'store')
    store.external_agents = manager
    enable(manager)
    store._select_external()
    current = store.agent.external
    current.operation = lambda: 'started-operation'
    for action in ('probe','revoke','preferences','enable'):
        with pytest.raises(InputError): store.external_action(action, {'entry_id':'entry-a'})
    assert store.agent.external is current


def test_unsupported_acp_cannot_be_enabled(tmp_path, catalog):
    from tools.workbench_external import ExternalAgentError
    manager, _, _ = catalog
    manager.discover({})
    row = dict(manager.settings['entries'][0],kind='custom_acp')
    manager._discover = lambda manual: [dict(row)]
    with pytest.raises(ExternalAgentError): enable(manager)
