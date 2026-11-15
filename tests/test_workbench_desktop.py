"""Desktop launch boundaries and frozen worker compatibility."""
from pathlib import Path
import pytest
from tools import workbench_desktop as desktop


def test_user_state_is_independent_of_checkout(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    assert desktop.default_state() == tmp_path / 'Xijian' / 'desktop-v1'


def test_bundled_dispatch_rejects_generic_interpreter_commands(tmp_path):
    for args in (['-c', 'print(1)'], ['-I', '-B', '-c', 'print(1)'],
                 ['-I', '-u', str(tmp_path / 'execution_worker.py'), str(tmp_path)]):
        with pytest.raises(ValueError):
            desktop.worker_mode(args)


def test_exact_worker_and_http_dispatch_are_recognized(tmp_path):
    from tools.workbench_connectors import _HTTP_WORKER
    worker = desktop.RESOURCE_ROOT / 'runtime' / 'execution_worker.py'
    assert desktop.worker_mode(['-I', '-u', str(worker), str(tmp_path)]) == ('analysis', [str(tmp_path)])
    assert desktop.worker_mode(['-I', '-B', '-c', _HTTP_WORKER]) == ('http', [])


def test_launch_metadata_must_identify_owned_local_server():
    good = {'url': 'http://127.0.0.1:23456/', 'pid': 123}
    assert desktop.owned_url(good, 123) == good['url']
    for update in ({'pid': 124}, {'url': 'https://example.com/'},
                   {'url': 'http://127.0.0.1:23456/other'}, {'url': 'http://user@127.0.0.1:23456/'}):
        assert desktop.owned_url(dict(good, **update), 123) is None

def test_desktop_starts_and_closes_only_its_owned_service(tmp_path):
    session = desktop.DesktopSession(tmp_path)
    try:
        address = session.start()
        assert address.startswith('http://127.0.0.1:')
        import json
        from urllib.request import urlopen
        metadata = json.loads((tmp_path / 'server.json').read_text(encoding='utf-8'))
        with urlopen(address + 'api/bootstrap', timeout=2) as response:
            bootstrap = json.load(response)
        assert metadata['version'] == bootstrap['version'] == desktop.VERSION
        assert session.process.poll() is None
        second = desktop.DesktopSession(tmp_path)
        try:
            with pytest.raises(RuntimeError, match='已经打开'):
                second.start()
        finally:
            second.close()
    finally:
        session.close()
    assert session.process.poll() == 0