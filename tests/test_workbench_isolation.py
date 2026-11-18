import hashlib
import socket

from runtime.supervised_execution import Limits


def test_windows_boundary_with_synthetic_controls(tmp_path):
    from runtime.workbench_isolation import execute
    marker = tmp_path / 'ordinary-synthetic-marker.txt'
    marker.write_text('synthetic control', encoding='ascii')
    assert marker.read_text(encoding='ascii') == 'synthetic control'
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(2)
        addr = listener.getsockname()
        with socket.create_connection(addr, timeout=2):
            control, _ = listener.accept()
            control.close()
        program = (
            "def analyze(df):\n"
            "    import socket\n"
            "    try:\n"
            "        with open(%r, 'rb') as stream: stream.read(1)\n"
            "        file_denied = False\n"
            "    except PermissionError:\n"
            "        file_denied = True\n"
            "    try:\n"
            "        with socket.create_connection(%r, timeout=1): pass\n"
            "        network_denied = False\n"
            "    except OSError:\n"
            "        network_denied = True\n"
            "    return {'rows':[{'total':int(df['COL_0001'].sum()), 'file_denied':file_denied,"
            " 'network_denied':network_denied}]}\n"
        ) % (str(marker), addr)
        candidate = tmp_path / 'program.py'
        original = program.encode('utf-8')
        candidate.write_bytes(original)
        outcome = execute(candidate, hashlib.sha256(original).hexdigest(),
                          [{'COL_0001': 11}, {'COL_0001': 23}], Limits(timeout_seconds=20))
        assert outcome.receipt['status'] == 'success', str(outcome.receipt)
        assert outcome.receipt['cleanup_complete']
        assert outcome.receipt['isolation']['token_verified']
        assert outcome.receipt['isolation']['policy']=='windows-appcontainer-zero-capabilities-single-process-v1'
        assert outcome.receipt['isolation']['lpac'] is False
        assert outcome.receipt['isolation']['capability_count'] == 0
        assert outcome.payload == {'rows':[{'total':34,'file_denied':True,'network_denied':True}]}
        assert candidate.read_bytes() == original
        listener.settimeout(0.1)
        try:
            extra, _ = listener.accept()
        except socket.timeout:
            pass
        else:
            extra.close()
            raise AssertionError('container reached synthetic listener')


def test_public_and_internal_entrypoints_refuse_an_unavailable_boundary(tmp_path, monkeypatch):
    import pandas as pd
    from runtime import workbench_isolation as broker
    from runtime.mvp_binding import issue_binding
    from tools import workbench_analysis as analysis

    def unavailable(_):
        raise OSError('synthetic unavailable boundary')

    def must_not_run(*args, **kwargs):
        raise AssertionError('unexpected ordinary-user execution')

    monkeypatch.setattr(broker, '_runtime', unavailable)
    monkeypatch.setattr(analysis, 'execute', must_not_run)
    frame = pd.DataFrame({'approved': [5, 8]})
    binding = issue_binding(frame, analysis.NAMESPACE, analysis.SCOPE)
    source = b'def analyze(df):\n    return {"rows":[{"n":int(df.iloc[:,0].sum())}]}\n'
    contract = {'columns':[{'key':'n','label':'Sum','nullable':False,'restore_from':None,'type':'integer'}],
                'max_rows':1}
    bundle = {'schema':analysis.public_schema(binding),'examples':[{'COL_0001':3}], 'output':contract}
    for folder, call in [
            ('public', lambda path: analysis.run_public_candidate(source, bundle, path, source='external_http')),
            ('bound', lambda path: analysis.run_candidate(frame, source, contract, binding, path,
                                                          source='external_http', bundle_sha256='0'*64))]:
        path = tmp_path / folder
        result = call(path)
        assert result['status'] == 'EXECUTION_FAILED' and result['reason'] == 'enforcement_unavailable'
        assert result['rows'] == [] and result['isolation']['token_verified'] is False
        assert result == analysis.recover_candidate(path)
        assert (path / 'candidate.py').read_bytes() == source


def test_worker_startup_error_preserves_candidate_repair_allowance(tmp_path, monkeypatch):
    import pandas as pd
    from runtime import workbench_isolation as broker
    from runtime.supervised_execution import ExecutionResult
    from test_workbench_agent import InternalDouble, ExternalDouble
    from tools.workbench_agent import AgentWorkbench, AgentError
    import pytest

    def loader_failure(*args, limits, **kwargs):
        receipt = broker._initial(limits)
        receipt.update(status='abnormal_exit', enforcement_installed=True)
        receipt['isolation'].update(token_verified=True, capability_count=0, phase='complete')
        return ExecutionResult(receipt)

    monkeypatch.setattr(broker, 'execute', loader_failure)
    service = AgentWorkbench(tmp_path, internal=InternalDouble(), external=ExternalDouble())
    task = service.draft(pd.DataFrame({'m':[8,13]}),'Synthetic measurement')
    key = task['id']
    service.publish(key, task['proposal_sha256'])
    candidate = service.generate(key, task['bundle_sha256'])
    tested = service.public_test(key, candidate['candidate_sha256'])['public_test']
    assert tested['status'] == 'ISOLATION_BLOCKED'
    assert tested['reason_code'] == 'PUBLIC_ISOLATION_UNAVAILABLE'
    with pytest.raises(AgentError):
        service.repair(key, candidate['candidate_sha256'])
    assert not service.get(key).get('repair_child_id')


def test_known_dev3_identity_is_read_only_and_exact(monkeypatch):
    from tools import workbench_analysis as analysis
    active = analysis.storage.runtime_identity()
    old = dict(active, executable_sha256='e5065556c71714727c07fe9e107c0c0a5ad77d3c5681637d794e8f9f998bf376')
    monkeypatch.setattr(analysis.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(analysis.storage, 'runtime_identity', lambda limits: active)
    assert analysis._matches_agent_runtime(old, 1)
    for key, wrong in [('executable_sha256','1'*64),('worker_sha256','2'*64),
                       ('supervisor_sha256','3'*64),('interpreter_version',[3,8,6]),('input_policy','other')]:
        assert not analysis._matches_agent_runtime(dict(old, **{key:wrong}), 1)


def test_transient_workspace_has_a_protected_dacl(tmp_path, monkeypatch):
    import ctypes
    from pathlib import Path
    import tempfile
    from runtime import workbench_win32 as win
    import re

    read_sd = win.api(win.advapi, 'GetNamedSecurityInfoW', win.DWORD, win.w.LPCWSTR, win.DWORD,
                      win.DWORD, win.P,win.P,win.P,win.P,win.PTR(win.P))
    to_text = win.api(win.advapi,'ConvertSecurityDescriptorToStringSecurityDescriptorW',win.w.BOOL,
                      win.P,win.DWORD,win.DWORD,win.PTR(win.P),win.P)
    def dacl(path):
        sd, out = win.P(), win.P()
        assert read_sd(str(path),1,4,None,None,None,None,win.ref(sd)) == 0
        try:
            win.check(to_text(sd,1,4,win.ref(out),None))
            return ctypes.wstring_at(out)
        finally:
            if out: win.local_free(out)
            win.local_free(sd)
    parent_before = dacl(tmp_path)
    monkeypatch.setattr(tempfile,'tempdir',str(tmp_path))
    with win.PrivateDirectory('synthetic-only-') as workspace:
        directory = Path(workspace)
        file = directory / 'synthetic-marker.txt'
        file.write_text('synthetic')
        protected = dacl(directory)
        assert protected.startswith('D:P') and protected.count('(A;') == 2
        assert '(A;OICI;FA;;;SY)' in protected and re.search(r'\(A;OICI;FA;;;S-1-[0-9-]+\)',protected)
        assert dacl(file).count('(A;') == 2
        assert dacl(tmp_path) == parent_before
    assert not directory.exists() and dacl(tmp_path) == parent_before
