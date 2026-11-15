# -*- coding: utf-8 -*-
"""Claude Code headless public-bundle adapter with dedicated account state."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import uuid

from tools.workbench_connectors import ConnectorError
from tools.workbench_qoder import _spawn_in_job
from tools import local_synthetic_workflow as storage

REVIEWED_VERSION = '2.1.278'
MAX_BYTES = 65536
REQUIRED_FLAGS = ('--safe-mode', '--tools', '--strict-mcp-config', '--setting-sources',
    '--no-session-persistence', '--disable-slash-commands', '--output-format')


class ClaudeCodeAdapter:
    source = 'claude'

    def __init__(self, root, budget, profile):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.budget, self.profile = budget, copy.deepcopy(profile)
        self._compatible = self._authenticated = False
        self._lock, self._call_lock = threading.Lock(), threading.Lock()
        self._process = self._tree = self._operation = None
        self._cancelled = False
        self.last_receipt = None

    def _environment(self):
        env = {k: os.environ[k] for k in ('PATH', 'SystemRoot', 'WINDIR', 'COMSPEC', 'PATHEXT')
            if k in os.environ}
        home = self.root / 'claude-home'
        for name, child in (('HOME', 'user'), ('USERPROFILE', 'user'), ('APPDATA', 'roaming'),
                ('LOCALAPPDATA', 'local'), ('TEMP', 'temp'), ('TMP', 'temp'), ('CLAUDE_CONFIG_DIR', 'config')):
            path = home / child
            path.mkdir(parents=True, exist_ok=True)
            env[name] = str(path)
        env.update(CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',
            CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL='1',
            CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK='1',
            CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION='false', CLAUDE_CODE_MAX_RETRIES='0',
            CLAUDE_CODE_MAX_OUTPUT_TOKENS='8192', MAX_THINKING_TOKENS='0')
        return env

    def _identity_ok(self):
        try:
            path = Path(self.profile['executable'])
            version = self.profile.get('version', '').replace(' (Claude Code)', '')
            return (path.is_absolute() and path.suffix.lower() == '.exe' and not path.is_symlink()
                and not getattr(path.lstat(), 'st_file_attributes', 0) & 0x400
                and version == REVIEWED_VERSION
                and hashlib.sha256(path.read_bytes()).hexdigest() == self.profile['executable_sha256'])
        except (OSError, KeyError, TypeError):
            return False

    def _clean_home(self):
        home = self.root / 'claude-home'
        for path in [home] + [home / p for p in ('user', 'roaming', 'local', 'temp', 'config')]:
            if path.is_symlink() or (path.exists() and getattr(path.lstat(), 'st_file_attributes', 0) & 0x400):
                return False
        return True

    def _machine_policy_present(self):
        # Inspect policy existence only; do not load organization policy contents.
        base = Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'ClaudeCode'
        if any((base / name).exists() for name in ('managed-settings.json', 'managed-settings.d', 'managed-mcp.json')):
            return True
        if os.name == 'nt':
            import winreg
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(hive, r'SOFTWARE\Policies\ClaudeCode'):
                        return True
                except FileNotFoundError:
                    pass
                except OSError:
                    return True
        return False

    def _used(self):
        approval = self.profile.get('approval_id', self.profile.get('entry_id'))
        used = 0
        for path in self.root.glob('claude-*/admission.json'):
            try:
                if json.loads(path.read_text(encoding='utf-8')).get('approval_id') == approval:
                    used += 1
            except (OSError, ValueError):
                return self.profile.get('max_invocations', 1)
        return used

    def readiness(self):
        missing = []
        if not self._identity_ok(): missing.append('reviewed_executable_identity')
        if not self._clean_home(): missing.append('dedicated_home_boundary')
        if self._machine_policy_present(): missing.append('managed_policy_review')
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

    def cancel(self):
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                return dict(cancelled=False, status='idle')
            self._cancelled = True
            self._tree.terminate()
            return dict(cancelled=True, status='terminated', operation_id=self._operation)

    def _run(self, args, workspace, operation, payload=None, timeout=30):
        if not self._clean_home() or self._machine_policy_present():
            raise ConnectorError('claude_home_boundary', 'Claude 外侧目录或机器策略需要检查。')
        with self._lock:
            if self._operation is not None:
                raise ConnectorError('operation_active', 'Claude 已有操作运行。')
            self._operation, self._cancelled = operation, False
            try:
                process, tree = _spawn_in_job([self.profile['executable']] + args,
                    cwd=str(workspace), env=self._environment(), stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self._process, self._tree = process, tree
            except Exception:
                self._process = self._tree = self._operation = None
                raise ConnectorError('claude_launch_failed', 'Claude 启动或进程边界检查失败。')
        output, overflow = bytearray(), threading.Event()
        def drain():
            while True:
                chunk = process.stdout.read(4096)
                if not chunk: break
                if len(output) + len(chunk) > 262144:
                    overflow.set(); tree.terminate(); break
                output.extend(chunk)
        def send():
            try:
                if payload: process.stdin.write(payload)
                process.stdin.close()
            except (OSError, ValueError):
                pass
        reader, writer = threading.Thread(target=drain, daemon=True), threading.Thread(target=send, daemon=True)
        reader.start(); writer.start()
        try:
            try: process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                raise ConnectorError('claude_timeout', 'Claude 超时，未自动重试。')
            reader.join(5)
            if self._cancelled: raise ConnectorError('claude_cancelled', 'Claude 已取消；本次预留保持计费。')
            if overflow.is_set() or reader.is_alive(): raise ConnectorError('claude_output_limit', 'Claude 输出超出限制。')
            return process.returncode, bytes(output)
        finally:
            tree.terminate(); tree.close()
            if process.poll() is None: process.kill(); process.wait(timeout=10)
            reader.join(2); writer.join(2); process.stdout.close()
            with self._lock:
                self._tree = self._process = self._operation = None

    def probe(self):
        self._compatible = self._authenticated = False
        if self._identity_ok() and self._clean_home() and not self._machine_policy_present():
            with tempfile.TemporaryDirectory(prefix='xijian-claude-probe-') as workspace:
                version_code, version = self._run(['--version'], workspace, 'probe-version')
                help_code, help_text = self._run(['--help'], workspace, 'probe-help')
                status, auth = self._run(['auth', 'status'], workspace, 'probe-auth')
            self._compatible = (version_code == help_code == 0
                and (REVIEWED_VERSION + ' (Claude Code)') in version.decode('utf-8', 'replace').splitlines()
                and all(flag.encode() in help_text for flag in REQUIRED_FLAGS))
            try:
                self._authenticated = status == 0 and json.loads(auth).get('loggedIn') is True
            except (ValueError, AttributeError):
                pass
        return dict(status='compatible' if self._compatible else 'incompatible', compatible=self._compatible,
            boundary_verified=self._compatible, authentication='authenticated' if self._authenticated else 'unauthenticated',
            authentication_scope='dedicated_external_home', login_home=str(self.root / 'claude-home' / 'config'),
            login_environment={k: v for k, v in self._environment().items()
                if k in ('HOME', 'USERPROFILE', 'CLAUDE_CONFIG_DIR')},
            version=REVIEWED_VERSION, model_requests=0, task_verified=False)

    def _arguments(self):
        return ['--safe-mode', '--print', '--output-format', 'stream-json', '--verbose',
            '--tools', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
            '--setting-sources', '', '--no-session-persistence', '--disable-slash-commands',
            '--permission-prompts', 'none', '--permission-mode', 'dontAsk', '--max-turns', '1',
            '--model', self.profile['model'], '--settings',
            '{"disableAllHooks":true,"disableClaudeAiConnectors":true,"syncClaudeAiPlugins":false}',
            '--system-prompt', 'Return only the complete UTF-8 Python analysis program without markdown fences. '
            'The approved public bundle is the sole task input. No tools are available.']

    @staticmethod
    def _candidate(code, output):
        try:
            events = [json.loads(line) for line in output.splitlines() if line.strip()]
            if code or not events or not all(isinstance(e, dict) for e in events): raise ValueError()
            init = [e for e in events if e.get('type') == 'system' and e.get('subtype') == 'init']
            results = [e for e in events if e.get('type') == 'result']
            assistants = [e for e in events if e.get('type') == 'assistant']
            if (len(init) != 1 or events[0] is not init[0] or init[0].get('tools')
                    or init[0].get('mcp_servers') or len(results) != 1 or len(assistants) != 1):
                raise ValueError()
            result = results[0]
            if (events[-1] is not result or result.get('subtype') != 'success' or result.get('is_error') is not False
                    or result.get('num_turns') != 1 or result.get('stop_reason') != 'end_turn'):
                raise ValueError()
            texts = []
            for event in events:
                if event.get('type') == 'assistant':
                    message = event['message']
                    if message.get('stop_reason') not in (None, 'end_turn'): raise ValueError()
                    for block in message['content']:
                        if block.get('type') == 'text': texts.append(block['text'])
                        elif block.get('type') != 'thinking': raise ValueError()
                elif event is not init[0] and event is not result:
                    raise ValueError()
            if ''.join(texts) != result['result']: raise ValueError()
            candidate = result['result'].encode('utf-8')
            if not candidate.strip() or len(candidate) > MAX_BYTES: raise ValueError()
            return candidate
        except (ValueError, KeyError, TypeError, AttributeError, UnicodeError):
            raise ConnectorError('claude_unresolved', 'Claude 未完整交付限定大小的程序，或出现额外工具事件。')

    def call(self, public_bundle):
        if not self._call_lock.acquire(False):
            raise ConnectorError('operation_active', 'Claude 已有生成操作。')
        try:
            if not self.readiness()['ready']:
                raise ConnectorError('claude_not_ready', '请核对 Claude 接口、独立账户和本次使用授权。')
            raw = storage._bytes(public_bundle)
            if len(raw) > MAX_BYTES: raise ConnectorError('public_bundle_too_large', '公开包超过 64 KB。')
            operation, allocation = 'claude-' + uuid.uuid4().hex, self.profile['allocation']
            folder = self.root / operation
            folder.mkdir()
            (folder / 'admission.json').write_bytes(storage._bytes(dict(operation_id=operation,
                approval_id=self.profile.get('approval_id', self.profile.get('entry_id')), entry_id=self.profile.get('entry_id'))))
            self.budget.reserve(operation, allocation)
            receipt = dict(operation_id=operation, source=self.source, configured_model=self.profile['model'],
                public_bundle_sha256=hashlib.sha256(raw).hexdigest(), status='unknown', allocation=allocation,
                accounting='ledger_allocation_not_provider_cap', cli_invocations=1,
                wire_inventory_verified=False, actual_usage_known=False)
            self.last_receipt = receipt
            try:
                with tempfile.TemporaryDirectory(prefix='xijian-public-claude-') as workspace:
                    (folder / 'public-bundle.json').write_bytes(raw)
                    code, events = self._run(self._arguments(), workspace, operation,
                        b'Approved public bundle:\n' + raw, self.profile.get('timeout_seconds', 120))
                    candidate = self._candidate(code, events)
                    (folder / 'analysis.py').write_bytes(candidate)
                    receipt.update(status='complete', candidate_sha256=hashlib.sha256(candidate).hexdigest())
                    return candidate
            except Exception as error:
                receipt.update(status='unresolved', error_code=getattr(error, 'code', 'claude_unresolved'))
                raise
            finally:
                receipt['token_budget'] = self.budget.settle(operation)
                (folder / 'receipt.json').write_bytes(storage._bytes(receipt))
        finally:
            self._call_lock.release()
