"""Windows desktop shell and strictly bounded frozen-host dispatch."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

VERSION = '0.4.0-dev.4'
RESOURCE_ROOT = Path(__file__).resolve().parents[1]
APP_ID = 'local-analysis-workbench-v1'


def default_state():
    base = os.environ.get('LOCALAPPDATA')
    if not base:
        raise ValueError('Windows local application data directory is unavailable.')
    return Path(base) / 'Xijian' / 'desktop-v1'


def worker_mode(arguments):
    """No arbitrary -c, -m or script execution in the bundled host."""
    args = list(arguments)
    if len(args) == 4 and args[:2] == ['-I', '-u']:
        if Path(args[2]).resolve() == RESOURCE_ROOT / 'runtime' / 'execution_worker.py':
            return 'analysis', [args[3]]
    if len(args) >= 3 and args[:2] == ['-I', '-B']:
        if Path(args[2]).resolve() == RESOURCE_ROOT / 'tools' / 'workbench_mcp.py':
            return 'mcp', args[3:]
        if len(args) == 4 and args[2] == '-c':
            from tools.workbench_connectors import _HTTP_WORKER
            if args[3] == _HTTP_WORKER:
                return 'http', []
            from tools.workbench_remote import HTTP_WORKER
            if args[3] == HTTP_WORKER:
                return 'remote_http', []
    raise ValueError('Unsupported bundled host command.')


def owned_url(metadata, pid):
    try:
        address = metadata['url']
        parsed = urlsplit(address)
        if (type(metadata['pid']) is int and metadata['pid'] == pid
                and parsed.scheme == 'http' and parsed.hostname == '127.0.0.1'
                and parsed.port and parsed.path == '/' and not parsed.query
                and not parsed.fragment and not parsed.username and not parsed.password):
            return address
    except (KeyError, TypeError, ValueError):
        pass
    return None


def host_main(arguments=None):
    args = list(sys.argv[1:] if arguments is None else arguments)
    if args and args[0] == '-I':
        mode, rest = worker_mode(args)
        if mode == 'analysis':
            sys.argv = ['execution_worker.py'] + rest
            from runtime.execution_worker import main
            return main()
        if mode == 'mcp':
            sys.argv = ['workbench_mcp.py'] + rest
            from tools.workbench_mcp import main
            return main()
        if mode == 'remote_http':
            from tools.workbench_remote import HTTP_WORKER as worker
        else:
            from tools.workbench_connectors import _HTTP_WORKER as worker
        exec(compile(worker, '<workbench-http-worker>', 'exec'), {'__name__': '__main__'})
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--state', type=Path)
    parser.add_argument('--self-check', action='store_true')
    parser.add_argument('--runtime-smoke', type=Path)
    parsed = parser.parse_args(args)
    if parsed.runtime_smoke:
        from runtime.mvp_binding import issue_binding
        from tools import workbench_analysis as analysis
        import pandas as pd
        root = parsed.runtime_smoke.resolve()
        root.mkdir(parents=True, exist_ok=False)
        candidate = root / 'original.py'
        code = b'def analyze(df):\n    return {"rows": [{"total": int(df["COL_0001"].sum())}]}\n'
        candidate.write_bytes(code)
        frame = pd.DataFrame({'synthetic_amount': [11, 23, 47]})
        binding = issue_binding(frame, analysis.NAMESPACE, analysis.SCOPE)
        contract = {'columns': [{'key': 'total', 'label': 'Total', 'type': 'integer',
                    'nullable': False, 'restore_from': None}], 'max_rows': 10}
        result = analysis.run_candidate(frame, code, contract, binding, root / 'run',
            source='local_test_double', bundle_sha256='0' * 64)
        summary = {'version': VERSION, 'frozen': bool(getattr(sys, 'frozen', False)),
                   'status': result['status'], 'rows': result.get('rows'),
                   'candidate_unchanged': candidate.read_bytes() == code}
        (root / 'desktop-runtime-check.json').write_text(json.dumps(summary), encoding='utf-8')
        print(json.dumps(summary), flush=True)
        return 0 if result.get('rows') == [{'total': 81}] and summary['candidate_unchanged'] else 1
    if parsed.self_check:
        from tools.local_synthetic_workflow import runtime_identity
        identity = runtime_identity()
        print(json.dumps({'version': VERSION, 'app_id': APP_ID,
                          'frozen': bool(getattr(sys, 'frozen', False)),
                          'runtime': identity}, sort_keys=True), flush=True)
        return 0
    if not parsed.serve or parsed.state is None:
        parser.error('--serve and --state are required')
    from tools.workbench_server import main
    sys.argv = ['workbench_server.py', '--state', str(parsed.state)]
    main()
    return 0


class DesktopSession:
    def __init__(self, state):
        self.state = Path(state).resolve()
        self.process = None
        self.log = None
        self.lock = None
        self.address = None
        self.csrf = None
        self.tree = None

    def start(self):
        self.state.mkdir(parents=True, exist_ok=True)
        import msvcrt
        self.lock = (self.state / 'desktop.lock').open('a+b')
        self.lock.seek(0)
        if os.fstat(self.lock.fileno()).st_size == 0:
            self.lock.write(b'0')
            self.lock.flush()
        self.lock.seek(0)
        try:
            msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.lock.close()
            self.lock = None
            raise RuntimeError('此工作区已经打开，请使用现有窗口。') from None
        if getattr(sys, 'frozen', False):
            command = [str(Path(sys.executable).with_name('XijianHost.exe'))]
        else:
            command = [getattr(sys, '_base_executable', sys.executable), '-B', str(RESOURCE_ROOT / 'desktop_host.py')]
        self.log = (self.state / 'desktop-host.log').open('ab')
        self.process = subprocess.Popen(command + ['--serve', '--state', str(self.state)],
            cwd=str(self.state), stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        from tools.workbench_qoder import _ProcessTree
        self.tree = _ProcessTree(self.process)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError('本地服务启动失败。请查看工作区中的 desktop-host.log。')
            try:
                meta = json.loads((self.state / 'server.json').read_text(encoding='utf-8'))
                address = owned_url(meta, self.process.pid)
                if address:
                    with urlopen(address + 'api/bootstrap', timeout=1) as response:
                        info = json.load(response)
                    if (info.get('app_id') == APP_ID and info.get('version') == VERSION
                            and info.get('state_id') == meta.get('state_id')):
                        self.address, self.csrf = address, info['csrf_token']
                        return address
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(.1)
        raise RuntimeError('本地服务未能及时就绪；已有记录保留。')

    def close(self):
        try:
            if self.address and self.process is not None and self.process.poll() is None:
                request = Request(self.address + 'api/shutdown', data=b'{}', headers={
                    'Content-Type': 'application/json', 'Origin': self.address.rstrip('/'),
                    'X-Workbench-Token': self.csrf})
                try:
                    with urlopen(request, timeout=3) as response:
                        response.read(4096)
                    self.process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        finally:
            if self.tree is not None:
                self.tree.terminate()
                self.tree.close()
                self.tree = None
            if self.process is not None and self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=5)
            if self.log:
                self.log.close()
            if self.lock:
                self.lock.close()


def main():
    parser = argparse.ArgumentParser(description='析间 · Windows 本地桌面版')
    parser.add_argument('--state', type=Path)
    parser.add_argument('--smoke-seconds', type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    session = DesktopSession(args.state or default_state())
    try:
        import webview
        address = session.start()
        webview.settings['ALLOW_DOWNLOADS'] = True
        window = webview.create_window('析间 ' + VERSION, address, width=1280, height=850,
            min_size=(900, 640), confirm_close=not bool(args.smoke_seconds))
        def started():
            if args.smoke_seconds:
                report = {'version': VERSION, 'renderer': 'edgechromium', 'loaded': False}
                try:
                    report['loaded'] = window.events.loaded.wait(20)
                    if report['loaded']:
                        time.sleep(max(1, min(args.smoke_seconds, 10)))
                        report['page'] = window.evaluate_js("({title:document.title, service:document.querySelector('#service-status').textContent, taskEntry:!!document.querySelector('#agent-draft'), cancelEntry:!!document.querySelector('#agent-cancel-task')})")
                except Exception:
                    report['error'] = 'renderer_check_failed'
                finally:
                    (session.state / 'desktop-window-check.json').write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
                    window.destroy()
        webview.start(started, gui='edgechromium', debug=False,
                      private_mode=True, storage_path=str(session.state / 'webview'))
        return 0
    except Exception as error:
        import ctypes
        # Do not expose connector credentials or a Python traceback in the GUI.
        text = str(error) if isinstance(error, RuntimeError) else '桌面启动未完成。请确认 WebView2 和应用文件齐全。'
        if args.smoke_seconds:
            session.state.mkdir(parents=True, exist_ok=True)
            (session.state / 'desktop-window-check.json').write_text(json.dumps({'loaded':False,'error':str(error)}), encoding='utf-8')
        else:
            ctypes.windll.user32.MessageBoxW(None, text, '析间', 0x10)
        return 1
    finally:
        session.close()
