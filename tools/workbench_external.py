"""Local discovery, explicit per-entry approval and pre-submission selection.

This controller never dispatches model prompts or retries failed generations.
Persisted local preferences are operator settings, not security credentials.
"""
import copy
import hashlib
import json
from pathlib import Path
import threading
import uuid

from tools.workbench_connectors import ConnectorError
from tools.workbench_lifecycle import atomic


class ExternalAgentError(ConnectorError):
    def __init__(self, message):
        super().__init__('external_agent_configuration', message)


def check(condition, message):
    if not condition:
        raise ExternalAgentError(message)


class ExternalAgents:
    def __init__(self, root, budget, discover=None, probe=None, factories=None):
        from tools.workbench_discovery import discover_agents, probe_agent
        self.root, self.budget = Path(root), budget
        self.root.mkdir(parents=True, exist_ok=True)
        self._discover, self._probe = discover or discover_agents, probe or probe_agent
        self.factories = factories
        self.lock = threading.RLock()
        self._connectors = {}
        path = self.root / 'settings.json'
        self.settings = (json.loads(path.read_text('utf-8')) if path.exists() else {
            'version': 1, 'entries': [], 'manual_entries': [], 'approvals': {},
            'preferences': {'fixed_id': None, 'priority': [], 'allow_fallback': False},
            'active': False})
        check(self.settings.get('version') == 1, '外侧连接设置版本未知。')

    def _save(self):
        atomic(self.root / 'settings.json', self.settings)

    def _entry(self, entry_id):
        entry = next((r for r in self.settings['entries'] if r['id'] == entry_id), None)
        check(entry is not None, '请先检测或指定已知 Agent 入口。')
        return entry

    @staticmethod
    def _identity(entry):
        path = Path(entry['executable'])
        check(path.is_file() and not path.is_symlink() and
              not getattr(path.lstat(), 'st_file_attributes', 0) & 0x400,
              'Agent 入口不存在或是重解析链接，请重新检测。')
        check(path.stat().st_size <= 512 * 1024 * 1024, 'Agent 入口过大，无法核对。')
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _factory(self, kind):
        if self.factories is not None:
            return self.factories.get(kind)
        if kind == 'codex':
            from tools.workbench_codex import CodexAdapter
            return CodexAdapter
        if kind == 'claude':
            from tools.workbench_claude import ClaudeCodeAdapter
            return ClaudeCodeAdapter
        if kind in ('opencode', 'custom_acp'):
            from tools.workbench_acp import ACPAdapter
            return ACPAdapter
        return None

    def view(self):
        with self.lock:
            result = copy.deepcopy(self.settings)
            result.pop('approvals')
            from tools.workbench_discovery import AGENTS
            result['catalog'] = [dict(kind=kind, name=name) for kind, name in AGENTS.items()]
            for row in result['entries']:
                approval = self.settings['approvals'].get(row['id'])
                row['authorized'] = bool(approval)
                row['generation_supported'] = self._factory(row['kind']) is not None
                if approval:
                    row['approved_model'] = approval['model']
                    row['allocation'] = approval['allocation']
                    row['max_invocations'] = approval['max_invocations']
                    row['timeout_seconds'] = approval['timeout_seconds']
            result['selection'] = self.selection()
            return result

    def discover(self, payload):
        with self.lock:
            check(type(payload) is dict and not set(payload) - {'manual_entries'}, '检测参数无效。')
            manual = payload.get('manual_entries', self.settings['manual_entries'])
            check(type(manual) is list and len(manual) <= 20, '最多指定 20 个入口。')
            previous = {r['id']: r for r in self.settings['entries']}
            rows = self._discover(manual)
            # Preserve only observations whose exact entry still exists. Selection
            # and adapter launch check bytes again; discovery never runs the entry.
            for row in rows:
                old = previous.get(row['id'])
                if old and old.get('executable') == row.get('executable'):
                    for key in ('interface_status', 'auth_status', 'version', 'probe', 'probe_sha256'):
                        if key in old:
                            row[key] = old[key]
            self.settings['entries'], self.settings['manual_entries'] = rows, manual
            self._save()
            return self.view()

    def probe(self, entry_id):
        with self.lock:
            connector = self._connectors.get(entry_id)
            check(not connector or not connector.operation(), '当前入口正在运行，请结束后再检查。')
            original = self._entry(entry_id)
            row = self._probe(copy.deepcopy(original))
            factory = self._factory(row['kind'])
            if factory and row.get('interface_status') == 'compatible':
                fingerprint = self._identity(row)
                profile = dict(executable=row['executable'], executable_sha256=fingerprint,
                    version=row.get('version'), agent_kind=row['kind'], entry_id=entry_id,
                    authorized=False, data_scope='public_bundle_only', allocation=1,
                    max_invocations=1, timeout_seconds=60, model='')
                adapter = factory(self.root / 'runtimes' / entry_id, self.budget, profile)
                result = adapter.probe()
                row['probe'] = result
                row['probe_sha256'] = fingerprint
                row['auth_status'] = result.get('authentication', 'unknown')
                if result.get('compatible') is False:
                    row['interface_status'] = 'incompatible'
            original.update(row)
            self._connectors.pop(entry_id, None)
            self._save()
            return self.view()

    def preferences(self, payload):
        with self.lock:
            check(type(payload) is dict and not set(payload) - {'fixed_id', 'priority', 'allow_fallback'}, '选择规则参数无效。')
            fixed, priority = payload.get('fixed_id'), payload.get('priority', [])
            known = {r['id'] for r in self.settings['entries']}
            check(fixed is None or fixed in known, '固定入口尚未发现。')
            check(type(priority) is list and len(priority) <= 30 and
                  all(type(v) is str and v in known for v in priority) and len(set(priority)) == len(priority),
                  '优先级必须是已发现入口，且不能重复。')
            check(type(payload.get('allow_fallback', False)) is bool, '后备规则必须明确指定。')
            self.settings['preferences'] = dict(fixed_id=fixed, priority=priority,
                allow_fallback=payload.get('allow_fallback', False))
            self._save()
            return self.view()

    def enable(self, payload):
        with self.lock:
            allowed = {'entry_id', 'authorized', 'data_scope', 'account_scope', 'model',
                       'allocation', 'max_invocations', 'timeout_seconds'}
            check(type(payload) is dict and not set(payload) - allowed, '授权参数无效。')
            check(payload.get('authorized') is True and payload.get('data_scope') == 'public_bundle_only'
                  and payload.get('account_scope') == 'dedicated_external_home',
                  '请确认仅公开材料、独立外侧登录目录及本连接的费用范围。')
            check(type(payload.get('model')) is str and 1 <= len(payload['model'].strip()) <= 200,
                  '请填写实际要使用的模型标识。')
            for key, low, high in [('allocation', 1, 10000000), ('max_invocations', 1, 100), ('timeout_seconds', 10, 300)]:
                check(type(payload.get(key)) is int and low <= payload[key] <= high, '请填写有效的预留、次数与超时。')
            entry = self._entry(payload.get('entry_id'))
            check(self._factory(entry['kind']) is not None, '该工具已发现，但尚未接入此生成入口；Qoder 请使用保留的 SDK 连接。')
            check(entry.get('interface_status') == 'compatible', '先检查该入口的正式接口。')
            fingerprint = self._identity(entry)
            check(fingerprint == entry.get('probe_sha256'), '入口发生变化，请重新检查并确认用途。')
            profile = dict(payload, executable=entry['executable'], executable_sha256=fingerprint,
                version=entry.get('version'), agent_kind=entry['kind'], approval_id=uuid.uuid4().hex)
            # Model-dependent native runtimes (such as OpenCode free models)
            # can resolve authentication only after the model is specified.
            adapter = self._factory(entry['kind'])(self.root / 'runtimes' / entry['id'], self.budget, profile)
            observed = adapter.probe()
            check(observed.get('compatible') is not False, '当前模型与入口检查未通过，请核对配置。')
            entry['probe'] = observed
            entry['auth_status'] = observed.get('authentication', 'unknown')
            self.settings['approvals'][entry['id']] = profile
            self.settings['active'] = True
            priority = self.settings['preferences']['priority']
            if entry['id'] not in priority:
                priority.append(entry['id'])
            self._connectors[entry['id']] = adapter
            self._save()
            return self.view()

    def revoke(self, entry_id):
        with self.lock:
            connector = self._connectors.get(entry_id)
            check(not connector or not connector.operation(), '请先取消或结束当前生成，再撤销授权。')
            self.settings['approvals'].pop(entry_id, None)
            self._connectors.pop(entry_id, None)
            self._save()
            return self.view()

    def deactivate(self):
        with self.lock:
            self.settings['active'] = False
            self._save()

    def selection(self):
        from tools.workbench_discovery import select_agent
        with self.lock:
            rows = copy.deepcopy(self.settings['entries'])
            approvals = self.settings['approvals']
            for row in rows:
                row['authorized'] = row['id'] in approvals
                connector = self._connectors.get(row['id'])
                if connector and not connector.readiness().get('ready'):
                    row['auth_status'] = 'unavailable'
            preferences = dict(self.settings['preferences'], approved_ids=list(approvals))
            if not self.settings['active']:
                preferences['approved_ids'] = []
            return select_agent(rows, preferences)

    def connector(self):
        with self.lock:
            selection = self.selection()
            entry = selection['selected']
            check(entry is not None, '无可用的已授权外侧 Agent：' + selection['reason'])
            profile = self.settings['approvals'][entry['id']]
            check(self._identity(entry) == profile['executable_sha256'], 'Agent 已更新；请重新检查并授权此版本。')
            connector = self._connectors.get(entry['id'])
            if connector is None:
                connector = self._factory(entry['kind'])(self.root / 'runtimes' / entry['id'], self.budget, copy.deepcopy(profile))
                connector.probe()
                self._connectors[entry['id']] = connector
            return connector

    def resolve(self):
        """Bounded status-only fallback before a new submission, never call()."""
        with self.lock:
            for _ in range(len(self.settings['entries']) + 1):
                selection = self.selection()
                if selection['selected'] is None:
                    return selection, None
                connector = self.connector()
                if connector.readiness().get('ready'):
                    return selection, connector
                # Cached unready connectors become ineligible. Only the user's
                # saved pre-submission fallback policy can admit another entry.
            return {'selected':None,'reason':'no_eligible_approved_agent'}, None
