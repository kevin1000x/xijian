"""Discovery never runs an agent; probes and routing are separately admitted."""
import subprocess
import sys
import time

import pytest

from tools import workbench_discovery as discovery


@pytest.fixture
def empty_machine(monkeypatch):
    monkeypatch.setattr(discovery, '_known_candidates', lambda: [])
    monkeypatch.setenv('PATH', '')


def test_discovery_is_metadata_only_and_keeps_multiple_versions(tmp_path, monkeypatch, empty_machine):
    one = tmp_path / 'one' / 'codex.exe'
    two = tmp_path / 'two' / 'codex.exe'
    for path in (one, two):
        path.parent.mkdir()
        path.write_bytes(b'not launched')
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('discovery launched process'))
    records = discovery.discover_agents([
        {'kind': 'codex', 'executable': str(one)},
        {'kind': 'codex', 'executable': str(two)},
        {'kind': 'codex', 'executable': str(one)},
    ])
    codex = [record for record in records if record['kind'] == 'codex']
    assert len(codex) == 2 and len({r['id'] for r in codex}) == 2
    for record in codex:
        assert record['installed'] and record['version'] is None
        assert record['interface_status'] == 'unchecked'
        assert record['auth_status'] == 'unknown'
        assert record['task_validation'] == 'not_tested'
        assert record['authorized'] is False
    assert {r['kind'] for r in records} == set(discovery.AGENTS)


def test_custom_acp_is_registered_without_running_unknown_binary(tmp_path, monkeypatch, empty_machine):
    exe = tmp_path / 'unknown.exe'
    exe.write_bytes(b'never run')
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('unknown entry executed'))
    row = next(r for r in discovery.discover_agents([{'kind':'custom_acp','executable':str(exe)}]) if r['kind']=='custom_acp')
    assert row['installed']
    assert discovery.probe_agent(row)['interface_status'] == 'entry_unsupported'


def test_manual_desktop_entry_never_runs_as_cli(tmp_path, monkeypatch, empty_machine):
    exe=tmp_path/'OpenCode.exe'; exe.write_bytes(b'Electron desktop')
    resources=tmp_path/'resources'; resources.mkdir(); (resources/'app.asar').write_bytes(b'metadata only')
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('desktop was launched'))
    row=next(r for r in discovery.discover_agents([{'kind':'opencode','executable':str(exe)}]) if r['kind']=='opencode')
    assert discovery.probe_agent(row)['interface_status']=='desktop_only'


def test_opencode_desktop_known_location_is_found(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path))
    path=tmp_path/'Programs'/'@opencode-aidesktop'/'OpenCode.exe'
    path.parent.mkdir(parents=True); path.write_bytes(b'not executed')
    assert ('opencode',path,'desktop') in discovery._known_candidates()


@pytest.mark.parametrize('entries', [42, 'codex', [{}], [{'kind': 'alien', 'executable': 'x'}],
    [{'kind': 'codex', 'executable': 'relative.exe'}], [{'kind': 'codex', 'executable': 'C:\\bad\x00.exe'}]])
def test_bad_manual_entries_rejected(entries, empty_machine):
    with pytest.raises(ValueError):
        discovery.discover_agents(entries)


def test_missing_manual_path_does_not_claim_installed(tmp_path, empty_machine):
    record = discovery.discover_agents([{'kind': 'codex', 'executable': str(tmp_path / 'codex.exe')}])[0]
    assert not record['installed'] and record['status'] == 'not_installed'


def test_shell_shims_are_not_executed(tmp_path, monkeypatch, empty_machine):
    shim = tmp_path / 'codex.cmd'
    shim.write_text('echo unexpected')
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('shell shim launched'))
    record = discovery.discover_agents([{'kind': 'codex', 'executable': str(shim)}])[0]
    result = discovery.probe_agent(record)
    assert result['installed'] and result['interface_status'] == 'entry_unsupported'


def test_probe_version_is_not_auth_or_task_verification(tmp_path, monkeypatch, empty_machine):
    exe = tmp_path / 'codex.exe'
    exe.write_bytes(b'test double')
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return (0, 'codex-cli 0.115.0' if command[-1] == '--version' else ' '.join(discovery.CODEX_FLAGS), None)
    monkeypatch.setattr(discovery, '_run_probe', run)
    record = discovery.discover_agents([{'kind': 'codex', 'executable': str(exe)}])[0]
    result = discovery.probe_agent(record)
    assert result['version'] == '0.115.0' and result['interface_status'] == 'compatible'
    assert result['auth_status'] == 'unknown' and result['task_validation'] == 'not_tested'
    assert result['authorized'] is False
    assert calls == [[str(exe), '--version'], [str(exe), 'exec', '--help']]
    assert record['version'] is None


@pytest.mark.parametrize('error', ['timeout', 'output_limit', 'launch_failed'])
def test_probe_fails_closed_and_does_not_return_raw_output(tmp_path, monkeypatch, empty_machine, error):
    exe = tmp_path / 'opencode.exe'
    exe.write_bytes(b'test double')
    monkeypatch.setattr(discovery, '_run_probe', lambda *a, **k: (None, 'sensitive arbitrary text', error))
    record = next(r for r in discovery.discover_agents([{'kind': 'opencode', 'executable': str(exe)}]) if r['kind'] == 'opencode')
    result = discovery.probe_agent(record)
    assert result['interface_status'] == error
    assert 'sensitive' not in str(result)


def test_custom_acp_is_never_implicitly_probed(monkeypatch):
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('custom entry launched'))
    result = discovery.probe_agent({'kind': 'custom_acp', 'executable': '/unknown', 'installed': True})
    assert result['interface_status'] == 'entry_unsupported'


def test_probe_subprocess_has_bounded_time_and_output():
    start = time.monotonic()
    _, _, error = discovery._run_probe([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=0.2)
    assert error == 'timeout' and time.monotonic() - start < 3
    _, output, error = discovery._run_probe([sys.executable, '-c', 'print("x" * 1000000)'], output_limit=100)
    assert error == 'output_limit' and len(output.encode()) <= 100


def test_probe_excludes_project_cwd_and_preload_credentials(monkeypatch):
    monkeypatch.setenv('NODE_OPTIONS', '--require bad.js')
    monkeypatch.setenv('OPENAI_API_KEY', 'do-not-pass')
    code, output, error = discovery._run_probe([sys.executable, '-c',
        'import os; print(os.path.basename(os.getcwd())); print(os.getenv("NODE_OPTIONS")); print(os.getenv("OPENAI_API_KEY"))'])
    assert code == 0 and error is None
    assert output.startswith('xijian-agent-probe-') and output.count('None') == 2


def test_probe_timeout_closes_descendant_pipe_and_cwd():
    start = time.monotonic()
    code, output, error = discovery._run_probe([sys.executable, '-c',
        'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c","import time;time.sleep(10)"]); time.sleep(10)'], timeout=0.2)
    assert error == 'timeout' and time.monotonic() - start < 3


def test_old_codex_without_boundary_flags_is_incompatible(tmp_path, monkeypatch, empty_machine):
    exe = tmp_path / 'codex.exe'
    exe.write_bytes(b'test double')
    monkeypatch.setattr(discovery, '_run_probe', lambda command: (0,
        'codex-cli 0.115.0' if command[-1] == '--version' else '--json --output-last-message', None))
    record = discovery.discover_agents([{'kind': 'codex', 'executable': str(exe)}])[0]
    assert discovery.probe_agent(record)['interface_status'] == 'incompatible'


def eligible(identity):
    return {'id': identity, 'installed': True, 'interface_status': 'compatible',
            'auth_status': 'authenticated', 'authorized': True}


def test_selection_requires_separate_interface_auth_and_authorization():
    for key, value in [('installed', False), ('interface_status', 'unchecked'),
                       ('auth_status', 'unknown'), ('authorized', False)]:
        record = eligible('a')
        record[key] = value
        assert discovery.select_agent([record], {'approved_ids': ['a']})['selected'] is None
    assert discovery.select_agent([eligible('a')], {'approved_ids': []})['selected'] is None


def test_fixed_selection_never_silently_falls_back():
    result = discovery.select_agent([eligible('b')], {'fixed_id': 'a', 'approved_ids': ['a', 'b'], 'allow_fallback': True})
    assert result['selected'] is None and result['reason'] == 'fixed_unavailable'


def test_order_and_fallback_are_explicit_and_pre_submission_only():
    records = [eligible('b')]
    prefs = {'approved_ids': ['a', 'b'], 'allow_fallback': False}
    assert discovery.select_agent(records, prefs)['selected'] is None
    prefs['allow_fallback'] = True
    assert discovery.select_agent(records, prefs)['selected']['id'] == 'b'
    prefs['submission_started'] = True
    assert discovery.select_agent(records, prefs)['selected'] is None
    assert discovery.select_agent([eligible('a'), eligible('b')], {'approved_ids': ['b', 'a']})['selected']['id'] == 'b'


def test_priority_is_not_authorization_and_fixed_id_wins():
    records = [eligible('a'), eligible('b')]
    prefs = {'approved_ids': ['a'], 'priority': ['b', 'a']}
    assert discovery.select_agent(records, prefs)['selected'] is None
    prefs['allow_fallback'] = True
    assert discovery.select_agent(records, prefs)['selected']['id'] == 'a'
    prefs['approved_ids'] = ['a', 'b']
    assert discovery.select_agent(records, prefs)['selected']['id'] == 'b'
    prefs['fixed_id'] = 'a'
    assert discovery.select_agent(records, prefs)['selected']['id'] == 'a'


def test_known_windows_codex_versions_discovered_without_desktop_path(tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', '')
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setenv('APPDATA', str(tmp_path / 'Roaming'))
    monkeypatch.setenv('USERPROFILE', str(tmp_path / 'Profile'))
    monkeypatch.setenv('ProgramFiles', str(tmp_path / 'Programs'))
    for version in ('version-one', 'version-two'):
        executable = tmp_path / 'OpenAI' / 'Codex' / 'bin' / version / 'codex.exe'
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b'not executed')
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('discovery launched process'))
    codex = [r for r in discovery.discover_agents() if r['kind'] == 'codex']
    assert len(codex) == 2 and all(r['installed'] for r in codex)


def test_popular_desktops_and_cli_aliases_are_discovered_without_launch(tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', str(tmp_path / 'bin'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setenv('APPDATA', str(tmp_path / 'Roaming'))
    monkeypatch.setenv('USERPROFILE', str(tmp_path / 'Profile'))
    monkeypatch.setenv('ProgramFiles', str(tmp_path / 'Programs'))
    samples = {'cursor': 'Programs/cursor/Cursor.exe',
               'antigravity': 'Programs/Antigravity/Antigravity.exe',
               'windsurf': 'Programs/Windsurf/Windsurf.exe',
               'claude': 'AnthropicClaude/claude.exe',
               'qoder': 'bin/qoderclicn.cmd', 'copilot': 'bin/copilot.exe'}
    for name in samples.values():
        path=tmp_path/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(b'never run')
    monkeypatch.setattr(subprocess,'Popen',lambda *a,**k:pytest.fail('discovery launched process'))
    rows=discovery.discover_agents()
    for kind,name in samples.items():
        row=next(r for r in rows if r['kind']==kind and r['executable']==str((tmp_path/name).resolve()))
        assert row['installed'] and not row['authorized']
        if kind in ('cursor','antigravity','windsurf','claude'):
            assert discovery.probe_agent(row)['interface_status']=='desktop_only'
    assert any(r['kind']=='qoder' and r['source']=='path' for r in rows)


def test_manually_selected_vscode_desktop_does_not_run(tmp_path, monkeypatch, empty_machine):
    path=tmp_path/'Cursor.exe'; path.write_bytes(b'desktop')
    package=tmp_path/'resources'/'app';package.mkdir(parents=True)
    (package/'product.json').write_text('{}')
    monkeypatch.setattr(subprocess,'Popen',lambda *a,**k:pytest.fail('desktop launched'))
    row=next(r for r in discovery.discover_agents([{'kind':'cursor','executable':str(path)}]) if r['kind']=='cursor')
    assert discovery.probe_agent(row)['interface_status']=='desktop_only'


def test_cli_help_probe_uses_empty_dedicated_homes():
    code,output,error=discovery._run_probe([sys.executable,'-c',
        'import os,pathlib; root=pathlib.Path.cwd(); print(all(pathlib.Path(os.environ[k]).parent==root for k in ("HOME","USERPROFILE","CODEX_HOME","CLAUDE_CONFIG_DIR","XDG_CONFIG_HOME")))'])
    assert (code,output.strip(),error)==(0,'True',None)
