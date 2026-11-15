import hashlib
import pytest

from tools.workbench_external import ExternalAgents, ExternalAgentError


@pytest.fixture
def catalog(tmp_path):
    executable = tmp_path / 'codex.exe'
    executable.write_bytes(b'fixture executable; never launched')
    record = dict(id='entry-a', kind='codex', name='Codex', executable=str(executable),
                  installed=True, version='fixture', interface_status='unchecked',
                  auth_status='unknown', task_validation='not_tested', authorized=False)
    class Adapter:
        source = 'codex'
        calls = 0
        def __init__(self, root, budget, profile):
            self.profile = profile
            self.budget = budget
        def probe(self):
            return dict(compatible=True, authentication='authenticated', boundary_verified=True)
        def readiness(self):
            return dict(ready=True, live=True, source='codex', missing=[])
        def operation(self): return None
        def call(self, bundle):
            Adapter.calls += 1
            raise RuntimeError('outcome unknown')
    manager = ExternalAgents(tmp_path / 'connections', object(),
        discover=lambda manual: [dict(record)], probe=lambda r: dict(r, interface_status='compatible'),
        factories={'codex': Adapter})
    return manager, Adapter, executable


def enable(manager):
    manager.discover({})
    manager.probe('entry-a')
    return manager.enable(dict(entry_id='entry-a', authorized=True,
        data_scope='public_bundle_only', account_scope='dedicated_external_home',
        model='chosen-model', allocation=1000, max_invocations=1, timeout_seconds=30))


def test_discovery_does_not_enable_or_invoke(catalog):
    manager, adapter, _ = catalog
    view = manager.discover({})
    assert view['entries'][0]['authorized'] is False
    assert view['entries'][0]['auth_status'] == 'unknown'
    assert manager.selection()['selected'] is None
    assert adapter.calls == 0


def test_probe_separates_protocol_auth_task_and_approval(catalog):
    manager, _, _ = catalog
    manager.discover({})
    row = manager.probe('entry-a')['entries'][0]
    assert row['interface_status'] == 'compatible'
    assert row['auth_status'] == 'authenticated'
    assert row['authorized'] is False
    assert row['task_validation'] == 'not_tested'


def test_enable_requires_explicit_scope_and_limits(catalog):
    manager, _, _ = catalog
    manager.discover({})
    with pytest.raises(ExternalAgentError): manager.enable({'entry_id': 'entry-a'})
    for key, value in [('authorized', False), ('data_scope', 'internal'), ('allocation', True),
                       ('allocation', 0), ('max_invocations', 0), ('model', '')]:
        payload = dict(entry_id='entry-a', authorized=True, data_scope='public_bundle_only',
            account_scope='dedicated_external_home', allocation=1000, max_invocations=1,
            model='m', timeout_seconds=30)
        payload[key] = value
        with pytest.raises(ExternalAgentError): manager.enable(payload)


def test_approval_pins_executable_and_survives_restart(catalog):
    manager, _, executable = catalog
    enable(manager)
    selected = manager.selection()
    assert selected['selected']['id'] == 'entry-a'
    connector = manager.connector()
    assert connector.profile['executable_sha256'] == hashlib.sha256(executable.read_bytes()).hexdigest()
    assert connector.budget is manager.budget
    again = ExternalAgents(manager.root, manager.budget, discover=manager._discover,
                           probe=manager._probe, factories=manager.factories)
    assert again.selection()['selected']['id'] == 'entry-a'
    assert again.connector().profile['approval_id'] == connector.profile['approval_id']
    executable.write_bytes(b'version changed')
    with pytest.raises(ExternalAgentError): again.connector()


def test_failed_dispatch_has_no_adapter_fallback(catalog):
    manager, adapter, _ = catalog
    enable(manager)
    connector = manager.connector()
    with pytest.raises(RuntimeError): connector.call({'public': 'only'})
    assert adapter.calls == 1


def test_fixed_selection_unavailable_does_not_fallback(catalog):
    manager, _, _ = catalog
    enable(manager)
    with pytest.raises(ExternalAgentError):
        manager.preferences({'fixed_id': 'unknown', 'priority': ['entry-a'], 'allow_fallback': True})
    manager.revoke('entry-a')
    assert manager.selection()['selected'] is None


def test_manual_settings_never_accept_arbitrary_command_or_credentials(catalog):
    manager, _, _ = catalog
    with pytest.raises(ExternalAgentError): manager.discover({'command': 'run anything'})
    with pytest.raises(ExternalAgentError): manager.preferences({'credential': 'secret'})


def test_reprobe_replaces_cached_auth_state(catalog):
    manager, _, _ = catalog
    enable(manager)
    before = manager.connector()
    manager.probe('entry-a')
    assert manager.connector() is not before


def test_cached_budget_or_allowance_exhaustion_blocks_selection(catalog):
    manager, _, _ = catalog
    enable(manager)
    manager.connector().readiness = lambda: {'ready':False,'missing':['invocation_allowance']}
    assert manager.selection()['selected'] is None


def test_status_failure_can_only_fallback_before_submission(catalog):
    manager, adapter, _ = catalog
    enable(manager)
    row = dict(manager.settings['entries'][0],id='entry-b')
    manager.settings['entries'].append(row)
    approval = dict(manager.settings['approvals']['entry-a'],entry_id='entry-b',approval_id='b')
    manager.settings['approvals']['entry-b'] = approval
    manager.preferences({'priority':['entry-a','entry-b'],'allow_fallback':False})
    manager.connector().readiness = lambda: {'ready':False}
    assert manager.resolve()[1] is None
    manager.preferences({'priority':['entry-a','entry-b'],'allow_fallback':True})
    selection, connector = manager.resolve()
    assert selection['selected']['id'] == 'entry-b'
    assert selection['reason'] == 'approved_fallback_selected'
    assert adapter.calls == 0
    manager.preferences({'fixed_id':'entry-a','priority':['entry-a','entry-b'],'allow_fallback':True})
    assert manager.resolve()[1] is None


def test_view_exposes_adapter_capabilities_and_catalog(catalog):
    manager,_,_=catalog
    view=manager.discover({})
    assert view['entries'][0]['generation_supported'] is True
    manager.factories={}
    assert manager.view()['entries'][0]['generation_supported'] is False
    assert {'codex','claude','cursor','opencode','antigravity'} <= {r['kind'] for r in view['catalog']}


def test_claude_has_a_registered_generation_adapter():
    manager=object.__new__(ExternalAgents);manager.factories=None
    assert manager._factory('claude').source == 'claude'


def test_enable_probes_the_selected_model_before_selection(catalog):
    manager,adapter,_=catalog
    seen=[]
    def probe(self):
        seen.append(self.profile['model'])
        return dict(compatible=True,authentication='not_required' if self.profile['model']=='chosen-model' else 'unknown')
    adapter.probe=probe
    view=enable(manager)
    assert seen==['','chosen-model']
    assert view['selection']['selected']['id']=='entry-a'
    assert manager.connector().profile['model']=='chosen-model'
