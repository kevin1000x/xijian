"""Cursor ACP text delivery using the reviewed Windows CLI package."""
import hashlib
import json
import os
from pathlib import Path
import re
import string
import tempfile

from tools.workbench_acp import ACPAdapter, ACPTransport, _fail
from tools.workbench_connectors import ConnectorError
from tools.workbench_discovery import _run_probe

VERSION = '2026.11.05-86047df'
NODE_SHA256 = 'd702daa815eabbff763c7d3808e235cf031cf2ca70211b56847bc72f51e240e5'
PACKAGE_SHA256 = '0a0a9f2971c44a26fb2d99d17f818105d948315bbce8554581394ec6f116c7a9'
LOGIN_KEYS = ('HOME','USERPROFILE','APPDATA','LOCALAPPDATA','CURSOR_CONFIG_DIR','TEMP','TMP')


def _linked(path):
    return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)


def runtime_entry(path):
    try:
        if (os.name == 'nt' and path.is_absolute() and path.name.lower() == 'node.exe'
                and path.parent.name == VERSION and path.is_file()
                and (path.parent/'index.js').is_file()
                and not (_linked(path) or _linked(path.parent))):
            return VERSION
    except OSError:
        pass
    return None


def _verify_package(path):
    parent = path.parent
    if any(_linked(part) for part in parent.parents):
        _fail('cursor_runtime_changed')
    files = []
    size = 0
    for folder, dirs, names in os.walk(parent):
        current = Path(folder)
        if current == parent and '.running' in dirs:
            dirs.remove('.running')  # ephemeral vendor launch locks
        for name in dirs + names:
            if _linked(current/name):
                _fail('cursor_runtime_changed')
        for name in names:
            file = current/name
            size += file.stat().st_size
            if size > 512 * 1024 * 1024 or len(files) >= 1500:
                _fail('cursor_runtime_changed')
            files.append((file.relative_to(parent).as_posix(), file))
    package = hashlib.sha256()
    for relative, file in sorted(files):
        digest = hashlib.sha256()
        with file.open('rb') as stream:
            for block in iter(lambda: stream.read(1024*1024), b''):
                digest.update(block)
        package.update(relative.encode('utf-8') + b'\0' + digest.digest())
    if package.hexdigest() != PACKAGE_SHA256:
        _fail('cursor_runtime_changed')


def _status_from_output(output):
    try:
        value = json.loads(output)
    except ValueError:
        return 'unknown'
    if not isinstance(value, dict):
        return 'unknown'
    if value.get('status') == 'authenticated' and value.get('isAuthenticated') is True:
        return 'authenticated'
    if value.get('status') in ('unauthenticated','partially-authenticated') and value.get('isAuthenticated') is False:
        return 'unauthenticated'
    return 'unknown'


def cursor_policy():
    denies = ['Shell(*)', 'Read(**)', 'Write(**)', 'WebFetch(*)', 'Mcp(*:*)']
    for drive in string.ascii_uppercase:
        denies.extend([f'Read({drive}:/**)', f'Write({drive}:/**)'])
    return {'permissions':{'allow':[],'deny':denies}}


def _write_workspace_policy(workspace):
    settings = workspace/'.cursor'
    settings.mkdir(exist_ok=True)
    config = settings/'cli.json'
    data = json.dumps(cursor_policy(), ensure_ascii=True)
    if config.exists():
        if _linked(config) or config.read_text(encoding='utf-8') != data:
            _fail('cursor_tool_policy_changed')
    else:
        with config.open('x', encoding='utf-8') as stream:
            stream.write(data)


def _option(config, name):
    choices = config.get('configOptions')
    if not isinstance(choices, list):
        _fail('cursor_session_config')
    matches = [item for item in choices if isinstance(item, dict)
               and item.get('id') == name and item.get('type') == 'select']
    if len(matches) != 1 or not isinstance(matches[0].get('options'), list):
        _fail('cursor_session_config')
    return matches[0]


def _values(options):
    for item in options:
        if isinstance(item, dict):
            if isinstance(item.get('value'), str):
                yield item['value']
            elif isinstance(item.get('options'), list):
                yield from _values(item['options'])


def _concrete_model(model):
    return (isinstance(model, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._:/+\[\]-]{0,159}', model)
            and model.strip().lower() not in {'auto', 'automatic', 'default'})


class CursorTransport(ACPTransport):
    expected_model = None

    def initialize(self):
        result = super().initialize(filesystem=False)
        if not any(isinstance(method, dict) and method.get('id') == 'cursor_login'
                   for method in result.get('authMethods', [])):
            _fail('cursor_auth_method')
        return result

    def client_request(self, method, params):
        if (method == 'session/request_permission' and self.session_id
                and params.get('sessionId') == self.session_id):
            return {'outcome':{'outcome':'cancelled'}}
        _fail('cursor_tool_denied')

    def _session_update(self, params):
        update = params.get('update')
        kind = update.get('sessionUpdate') if isinstance(update, dict) else None
        if kind in ('tool_call', 'tool_call_update', 'tool_call_update_chunk'):
            _fail('cursor_tool_activity')
        if self.expected_model:
            if kind == 'config_option_update':
                if (_option(update, 'model').get('currentValue') != self.expected_model
                        or _option(update, 'mode').get('currentValue') != 'ask'):
                    _fail('cursor_session_changed')
            if kind == 'current_mode_update' and update.get('currentModeId') != 'ask':
                _fail('cursor_session_changed')
            if kind == 'current_model_update' and update.get('currentModelId') != self.expected_model:
                _fail('cursor_session_changed')
        return super()._session_update(params)


class CursorAdapter(ACPAdapter):
    def __init__(self, root, budget, profile):
        super().__init__(root, budget, profile)
        self._models = ()

    def _runtime_policy(self):
        executable = Path(self.profile.get('executable') or '')
        if (self.profile.get('agent_kind') == 'cursor' and self.profile.get('version') == VERSION
                and self.profile.get('executable_sha256') == NODE_SHA256 and runtime_entry(executable)):
            return {'native':'cursor','candidate_mode':'text'}
        return None

    def _verified_executable(self):
        if not self._runtime_policy():
            _fail('acp_runtime_unreviewed')
        executable = Path(self.profile['executable'])
        _verify_package(executable)
        return executable

    def _environment(self):
        env = {key:os.environ[key] for key in ('SYSTEMROOT','WINDIR','SYSTEMDRIVE','COMSPEC','PATHEXT')
               if key in os.environ}
        for key, name in (('HOME','home'),('APPDATA','roaming'),('LOCALAPPDATA','local'),
                          ('TEMP','temp'),('CURSOR_CONFIG_DIR','cursor')):
            path = self.root/name
            path.mkdir(parents=True, exist_ok=True)
            if _linked(path):
                _fail('cursor_profile_changed')
            env[key] = str(path)
        config = Path(env['CURSOR_CONFIG_DIR'])
        if any(os.path.lexists(config/name) for name in ('mcp.json','plugins','skills','hooks.json','rules')):
            _fail('cursor_profile_extensions')
        env.update(USERPROFILE=env['HOME'], TMP=env['TEMP'], NO_COLOR='1', DO_NOT_TRACK='1',
                   NO_OPEN_BROWSER='1', PATH=str(Path(env['SYSTEMROOT'])/'System32'))
        return env

    def _open(self, workspace):
        executable = self._verified_executable()
        _write_workspace_policy(workspace)
        return CursorTransport([str(executable), str(executable.with_name('index.js')), 'acp'],
                               workspace, timeout=self.profile.get('timeout_seconds', 30),
                               env=self._environment())

    def _login_status(self, executable, env):
        code, output, error = _run_probe([str(executable), str(executable.with_name('index.js')),
                                         'status', '--format', 'json'], timeout=15, profile_env=env)
        if error or code != 0:
            _fail('cursor_login_status')
        return _status_from_output(output)

    def _generation_policy_verified(self):
        # This exact vendor build approves server skipApproval WebFetch requests
        # before applying local config and ACP permission callbacks.
        return False

    def readiness(self):
        value = super().readiness()
        if self._authentication != 'authenticated':
            value['missing'].append('authentication')
        model = self.profile.get('model')
        if not _concrete_model(model) or model not in self._models:
            value['missing'].append('model')
        if not self._generation_policy_verified():
            value['missing'].append('native_tool_policy_unverified')
        value['ready'] = not value['missing']
        return value

    def probe(self):
        self._protocol_checked = False
        self._authentication = 'unknown'
        self._models = ()
        try:
            executable = self._verified_executable()
            env = self._environment()
            login_workspace = Path(env['TEMP'])/'login'
            login_workspace.mkdir(exist_ok=True)
        except (ConnectorError, OSError):
            return {'status':'boundary_unreviewed','compatible':False,'protocol_checked':False,
                    'authentication':'unknown','model_requests':0}
        info = {'status':'protocol_checked','compatible':True,
                'generation_supported':self._generation_policy_verified(),'model_requests':0,
                'login_environment':{key:env[key] for key in LOGIN_KEYS},
                'login_script':str(executable.with_name('index.js')), 'login_workspace':str(login_workspace)}
        with tempfile.TemporaryDirectory(prefix='xijian-cursor-probe-') as directory:
            workspace = Path(directory)
            with self._open(workspace) as client:
                client.initialize()
                self._protocol_checked = True
                self._authentication = self._login_status(executable, env)
                if self._authentication == 'authenticated':
                    self._models = tuple(self._session_models(client, workspace))
                    info['models'] = list(self._models)
        return dict(info, protocol_checked=True, protocolVersion=1, authentication=self._authentication)

    def call(self, public_bundle):
        if not self._generation_policy_verified():
            _fail('cursor_native_tool_policy_unverified')
        if (self._authentication != 'authenticated' or self._login_status(
                self._verified_executable(), self._environment()) != 'authenticated'):
            self._authentication = 'unauthenticated'
            _fail('cursor_login_required')
        return super().call(public_bundle)

    def _start_session(self, client, workspace):
        client.request('authenticate', {'methodId':'cursor_login'})
        session = client.request('session/new', {'cwd':str(workspace), 'mcpServers':[]})
        if not isinstance(session.get('sessionId'), str) or not session['sessionId']:
            _fail('acp_session')
        client.session_id = session['sessionId']
        return session

    def _session_models(self, client, workspace):
        session = self._start_session(client, workspace)
        return [model for model in _values(_option(session, 'model')['options']) if _concrete_model(model)][:100]

    def _new_session(self, client, workspace):
        model = self.profile.get('model')
        if not _concrete_model(model):
            _fail('cursor_model_required')
        session = self._start_session(client, workspace)
        if model not in _values(_option(session, 'model')['options']):
            _fail('cursor_model_unavailable')
        mode = client.request('session/set_config_option', {
            'sessionId':client.session_id, 'configId':'mode','value':'ask'})
        if _option(mode, 'mode').get('currentValue') != 'ask':
            _fail('cursor_mode_changed')
        selected = client.request('session/set_config_option', {
            'sessionId':client.session_id, 'configId':'model','value':model})
        if (_option(selected, 'model').get('currentValue') != model
                or _option(selected, 'mode').get('currentValue') != 'ask'):
            _fail('cursor_session_changed')
        client.expected_model = model
        return {'model':model,'mode':'ask','acknowledgement':'acp_session_config'}
