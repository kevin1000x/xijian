"""Codex CLI public-package adapter. No imported personal sessions or tools.

The ledger allocation limits admission, not the provider's billing or requests.
Only the inspected CLI version is admitted; provider generation needs approval.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import uuid

from tools.workbench_connectors import ConnectorError
from tools.workbench_qoder import _spawn_in_job
from tools import local_synthetic_workflow as storage

REVIEWED_VERSION = '0.155.0-alpha.9'
MAX_BYTES = 65536


class CodexAdapter:
    source = 'codex'

    def __init__(self, root, budget, profile):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.budget = budget
        self.profile = copy.deepcopy(profile)
        self._compatible = False
        self._authenticated = False
        self._features = []
        self._lock = threading.Lock()
        self._call_lock = threading.Lock()
        self._process = self._tree = self._operation = None
        self._cancelled = False
        self.last_receipt = None

    def _environment(self):
        names = ('PATH', 'SystemRoot', 'WINDIR', 'USERPROFILE', 'LOCALAPPDATA',
                 'APPDATA', 'TEMP', 'TMP', 'COMSPEC', 'PATHEXT')
        env = {k: os.environ[k] for k in names if k in os.environ}
        # This CLI version unconditionally loads global AGENTS.md even with
        # --ignore-user-config. A separate home is necessary; never copy auth,
        # rules, sessions or MCP configuration from the operator's main home.
        home = self.root / 'codex-home'
        home.mkdir(exist_ok=True)
        env['CODEX_HOME'] = str(home)
        return env

    def _identity_ok(self):
        try:
            entry = Path(self.profile['executable'])
            return (entry.is_absolute() and entry.suffix.lower() == '.exe'
                and not entry.is_symlink()
                and not getattr(entry.lstat(), 'st_file_attributes', 0) & 0x400
                and hashlib.sha256(entry.read_bytes()).hexdigest() == self.profile['executable_sha256']
                and self.profile.get('version', '').replace('codex-cli ', '', 1) == REVIEWED_VERSION)
        except (OSError, KeyError, TypeError):
            return False

    def _used(self):
        approval = self.profile.get('approval_id', self.profile.get('entry_id'))
        used = 0
        for path in self.root.glob('codex-*/admission.json'):
            try:
                if json.loads(path.read_text(encoding='utf-8')).get('approval_id') == approval:
                    used += 1
            except (OSError, ValueError):
                return self.profile.get('max_invocations', 1)
        return used

    def readiness(self):
        missing = []
        if not self._clean_home(): missing.append('external_home_instructions')
        if not self._identity_ok(): missing.append('reviewed_executable_identity')
        if not self._compatible: missing.append('compatible_interface_probe')
        if not self._authenticated: missing.append('authentication')
        if self.profile.get('authorized') is not True: missing.append('authorization')
        if self.profile.get('data_scope') != 'public_bundle_only': missing.append('public_bundle_scope')
        if not isinstance(self.profile.get('model'), str) or not self.profile['model'].strip(): missing.append('model')
        allocation = self.profile.get('allocation')
        if type(allocation) is not int or allocation < 1: missing.append('allocation')
        elif self.budget.snapshot()['remaining'] < allocation: missing.append('token_budget')
        maximum = self.profile.get('max_invocations')
        if type(maximum) is not int or not 1 <= maximum <= 100 or self._used() >= maximum:
            missing.append('invocation_allowance')
        timeout = self.profile.get('timeout_seconds', 120)
        if type(timeout) is not int or not 1 <= timeout <= 300: missing.append('timeout')
        return dict(ready=not missing, source=self.source, live=True, missing=missing,
            compatible=self._compatible, boundary_verified=self._compatible,
            authentication_scope='dedicated_external_home',
            authentication='authenticated' if self._authenticated else 'unauthenticated',
            task_verified=False, model=self.profile.get('model'), version=self.profile.get('version'),
            allocation_per_generation=allocation, actual_usage_known=False,
            budget_coverage='ledger_allocation_not_provider_cap', token_budget=self.budget.snapshot())

    def operation(self):
        with self._lock:
            return self._operation

    def _clean_home(self):
        home = self.root / 'codex-home'
        return (not home.is_symlink() and
            not (home.exists() and getattr(home.lstat(), 'st_file_attributes', 0) & 0x400) and
            not any((home / name).exists() or (home / name).is_symlink()
                    for name in ('AGENTS.md', 'AGENTS.override.md')))

    def _run(self, args, workspace, operation, payload=None, timeout=30):
        """Bounded stdout, hidden window, kill-on-close job before prompt release."""
        if not self._clean_home():
            raise ConnectorError('codex_home_instructions', '独立外侧目录出现额外指令，已停止连接；请使用干净的外侧目录。')
        with self._lock:
            if self._operation is not None:
                raise ConnectorError('operation_active', 'Codex 已有操作运行。')
            self._operation, self._cancelled = operation, False
            try:
                process, tree = _spawn_in_job([self.profile['executable']] + args,
                    cwd=str(workspace), env=self._environment(), stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self._process = process
                self._tree = tree
            except Exception:
                if self._process is not None: self._process.kill(); self._process.wait()
                self._process = self._tree = self._operation = None
                raise ConnectorError('codex_launch_failed', 'Codex 启动或进程边界检查失败；未重试。')
        output = bytearray()
        overflow = threading.Event()
        def drain():
            while True:
                chunk = process.stdout.read(4096)
                if not chunk: break
                if len(output) + len(chunk) > 262144:
                    overflow.set(); tree.terminate(); break
                output.extend(chunk)
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        def send():
            try:
                if payload: process.stdin.write(payload)
                process.stdin.close()
            except (OSError, ValueError):
                pass
        writer = threading.Thread(target=send, daemon=True)
        writer.start()
        try:
            try: process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                raise ConnectorError('codex_timeout', 'Codex 超时，未自动重试。')
            reader.join(5)
            if self._cancelled: raise ConnectorError('codex_cancelled', 'Codex 已取消；本次预留保持计费。')
            if overflow.is_set() or reader.is_alive(): raise ConnectorError('codex_output_limit', 'Codex 输出超出限制。')
            return process.returncode, bytes(output)
        finally:
            tree.terminate(); tree.close()
            if process.poll() is None: process.kill(); process.wait(timeout=10)
            reader.join(2)
            writer.join(2)
            process.stdout.close()
            with self._lock:
                self._tree = self._process = self._operation = None

    def probe(self):
        self._compatible = self._authenticated = False
        if not self._identity_ok(): return dict(status='incompatible', compatible=False,
            boundary_verified=False, authentication='unknown', model_requests=0)
        with tempfile.TemporaryDirectory(prefix='xijian-codex-probe-') as workspace:
            _, version = self._run(['--version'], workspace, 'probe-version')
            _, help_text = self._run(['exec', '--help'], workspace, 'probe-help')
            _, features = self._run(['features', 'list'], workspace, 'probe-features')
            # Official status only; returned text is discarded, never persisted.
            status, _ = self._run(['login', 'status'], workspace, 'probe-auth')
        self._features = [line.split()[0].decode('ascii') for line in features.splitlines()
            if len(line.split()) >= 3 and re.fullmatch(rb'[a-z0-9_.]+', line.split()[0])
            and line.split()[-1] in (b'true', b'false')
            and b'removed' not in line.split() and b'deprecated' not in line.split()]
        flags = (b'--ignore-user-config', b'--ignore-rules', b'--ephemeral', b'--json', b'--strict-config', b'--output-last-message')
        self._compatible = (('codex-cli ' + REVIEWED_VERSION) in version.decode('utf-8', 'replace').splitlines()
            and all(flag in help_text for flag in flags)
            and all(feature in self._features for feature in ('hooks', 'plugins', 'shell_tool', 'skip_host_skill_discovery')))
        self._authenticated = status == 0
        return dict(status='compatible' if self._compatible else 'incompatible', compatible=self._compatible,
            boundary_verified=self._compatible, authentication='authenticated' if self._authenticated else 'unauthenticated',
            authentication_scope='dedicated_external_home',
            login_home=str(self.root / 'codex-home'),
            version=REVIEWED_VERSION, model_requests=0, task_verified=False)

    def _arguments(self, output):
        args = ['exec', '--ignore-user-config', '--ignore-rules', '--strict-config',
            '--ephemeral', '--json', '--skip-git-repo-check', '--sandbox', 'read-only',
            '--color', 'never', '--model', self.profile['model'], '--output-last-message', str(output)]
        settings = ['approval_policy="never"', 'project_doc_max_bytes=0',
            'skills.include_instructions=false', 'skills.bundled.enabled=false',
            'mcp_servers={}', 'web_search="disabled"', 'tools.update_plan.enabled=false',
            'tools.experimental_request_user_input.enabled=false',
            'analytics.enabled=false', 'feedback.enabled=false', 'check_for_update_on_startup=false',
            'suppress_unstable_features_warning=true',
            'developer_instructions=""', 'model_provider="openai"']
        settings.extend('features.{}={}'.format(feature, 'true' if feature == 'skip_host_skill_discovery' else 'false')
            for feature in self._features)
        for setting in settings: args.extend(['-c', setting])
        args.append('-')
        return args

    def cancel(self):
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                return dict(cancelled=False, status='idle')
            self._cancelled = True
            self._tree.terminate()
            return dict(cancelled=True, status='terminated', operation_id=self._operation)

    def call(self, public_bundle):
        if not self._call_lock.acquire(False):
            raise ConnectorError('operation_active', 'Codex 已有生成操作。')
        try:
            return self._call(public_bundle)
        finally:
            self._call_lock.release()

    def _call(self, public_bundle):
        if not self.readiness()['ready']:
            raise ConnectorError('codex_not_ready', '请核对 Codex 接口、账户和本次使用授权。')
        raw = storage._bytes(public_bundle)
        if len(raw) > MAX_BYTES: raise ConnectorError('public_bundle_too_large', '公开包超过 64 KB。')
        operation = 'codex-' + uuid.uuid4().hex
        allocation = self.profile['allocation']
        folder = self.root / operation
        folder.mkdir()
        admission = dict(approval_id=self.profile.get('approval_id', self.profile.get('entry_id')),
            operation_id=operation, entry_id=self.profile.get('entry_id'))
        (folder / 'admission.json').write_bytes(storage._bytes(admission))
        self.budget.reserve(operation, allocation)
        receipt = dict(operation_id=operation, source='codex', configured_model=self.profile['model'],
            public_bundle_sha256=hashlib.sha256(raw).hexdigest(), status='unknown',
            allocation=allocation, accounting='ledger_allocation_not_provider_cap',
            cli_invocations=1, wire_inventory_verified=False, actual_usage_known=False)
        self.last_receipt = receipt
        try:
            with tempfile.TemporaryDirectory(prefix='xijian-public-codex-') as workspace:
                output = Path(workspace) / 'analysis.py'
                (Path(workspace) / 'public-bundle.json').write_bytes(raw)
                (folder / 'public-bundle.json').write_bytes(raw)
                prompt = (b'Return only the complete UTF-8 Python analysis program, without markdown fences. '
                    b'The following approved public bundle is the sole task input.\n' + raw)
                code, events = self._run(self._arguments(output), workspace, operation, prompt,
                    self.profile.get('timeout_seconds', 120))
                parsed = []
                for line in events.splitlines():
                    try: parsed.append(json.loads(line))
                    except (ValueError, UnicodeError): pass
                completed = sum(event.get('type') == 'turn.completed' for event in parsed if isinstance(event, dict))
                bad, started = False, False
                for event in parsed:
                    if not isinstance(event, dict):
                        bad = True
                        continue
                    if event.get('type') == 'turn.started': started = True
                    if event.get('type') in ('turn.failed', 'error'): bad = True
                    if event.get('type', '').startswith('item.'):
                        item = event.get('item', {})
                        # CLI emits this local metadata warning before a turn on
                        # an empty home. It is not a failed tool/model operation.
                        startup_warning = (not started and item.get('type') == 'error'
                            and item.get('message', '').startswith('Model metadata for `'))
                        if item.get('type') not in ('agent_message', 'reasoning') and not startup_warning:
                            bad = True
                if code or completed != 1 or bad:
                    raise ConnectorError('codex_unresolved', 'Codex 未完整交付或出现非预期工具事件；未自动重试。')
                if (not output.is_file() or output.is_symlink()
                        or getattr(output.lstat(), 'st_file_attributes', 0) & 0x400
                        or output.stat().st_size > MAX_BYTES):
                    raise ConnectorError('codex_candidate_missing', 'Codex 未交付限定大小的程序。')
                candidate = output.read_bytes()
                candidate.decode('utf-8')
                if not candidate.strip(): raise ConnectorError('codex_candidate_empty', 'Codex 返回空程序。')
                (folder / 'analysis.py').write_bytes(candidate)
                receipt.update(status='complete', candidate_sha256=hashlib.sha256(candidate).hexdigest())
                return candidate
        except Exception as error:
            receipt.update(status='unresolved', error_code=getattr(error, 'code', 'codex_unresolved'))
            raise
        finally:
            receipt['token_budget'] = self.budget.settle(operation)
            (folder / 'receipt.json').write_bytes(storage._bytes(receipt))
