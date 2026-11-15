"""Bounded, metadata-only local discovery and explicit no-model CLI probes.

Finding a filename is not trust, authentication, task validation, or permission.
Known entry names are hints for the operator; no personal configuration is read.
"""
import copy
import hashlib
from itertools import islice
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time


AGENTS = {'codex': 'Codex', 'qoder': 'Qoder', 'opencode': 'OpenCode',
          'claude': 'Claude Code', 'gemini': 'Gemini CLI', 'cursor': 'Cursor',
          'copilot': 'GitHub Copilot', 'antigravity': 'Antigravity',
          'windsurf': 'Windsurf', 'trae': 'Trae', 'custom_acp': '自定义 ACP'}
# Product names and official command names are separate: desktop brands often
# ship a second CLI, and aliases must not be mistaken for a generic shell command.
COMMANDS = {kind: (kind,) for kind in AGENTS}
COMMANDS.update(qoder=('qoder', 'qodercli', 'qoderclicn'),
    cursor=('cursor-agent',), antigravity=('agy', 'antigravity'),
    trae=('trae', 'traecli'))
PROBE_TIMEOUT = 5.0
PROBE_OUTPUT_LIMIT = 32768
CODEX_FLAGS = ('--json', '--output-last-message', '--ignore-user-config',
               '--ignore-rules', '--ephemeral')


def _bounded_glob(base, pattern):
    try:
        # Every pattern is fixed here, with bounded depth; never recurse with **.
        return sorted(islice(base.glob(pattern), 64), key=lambda p: str(p).lower())
    except (OSError, ValueError):
        return []


def _known_candidates():
    candidates = []
    home = os.environ.get('USERPROFILE') or os.environ.get('HOME')
    local = os.environ.get('LOCALAPPDATA') or (str(Path(home) / 'AppData' / 'Local') if home and os.name == 'nt' else None)
    roaming = os.environ.get('APPDATA') or (str(Path(home) / 'AppData' / 'Roaming') if home and os.name == 'nt' else None)
    program = os.environ.get('ProgramFiles')
    bases = []
    if home:
        bases.extend([Path(home) / '.local' / 'bin', Path(home) / '.bun' / 'bin',
                      Path(home) / '.opencode' / 'bin'])
    if roaming:
        bases.append(Path(roaming) / 'npm')
    if program:
        bases.append(Path(program) / 'nodejs')
    if local:
        bases.extend([Path(local) / 'Programs' / 'nodejs', Path(local) / 'Microsoft' / 'WinGet' / 'Links'])
        candidates.append(('opencode', Path(local) / 'Programs' / '@opencode-aidesktop' / 'OpenCode.exe', 'desktop'))
        for path in _bounded_glob(Path(local) / 'OpenAI' / 'Codex' / 'bin', '*/codex.exe'):
            candidates.append(('codex', path, 'known_location'))
        for kind, relative in (
                ('claude', 'AnthropicClaude/claude.exe'),
                ('claude', 'Programs/Claude/Claude.exe'),
                ('cursor', 'Programs/cursor/Cursor.exe'),
                ('antigravity', 'Programs/Antigravity/Antigravity.exe'),
                ('windsurf', 'Programs/Windsurf/Windsurf.exe'),
                ('trae', 'Programs/Trae/Trae.exe')):
            candidates.append((kind, Path(local) / relative, 'desktop'))
        for path in _bounded_glob(Path(local) / 'AnthropicClaude', 'app-*/claude.exe'):
            candidates.append(('claude', path, 'desktop'))
        candidates.append(('antigravity', Path(local) / 'agy' / 'bin' / 'agy.exe', 'known_location'))
        for name in ('cursor-agent.exe', 'cursor-agent.cmd', 'cursor-agent.ps1'):
            candidates.append(('cursor', Path(local) / 'cursor-agent' / name, 'known_location'))
        for name in ('Qoder CN', 'Qoder'):
            base = Path(local) / 'Programs' / name
            candidates.append(('qoder', base / (name + '.exe'), 'desktop'))
            for path in _bounded_glob(base / '.qoder-versions', '*/' + name + '.exe'):
                candidates.append(('qoder', path, 'desktop'))
    for base in bases:
        for kind, names in COMMANDS.items():
            for name in names:
                for suffix in ('.exe', '.cmd', '.ps1', ''):
                    candidates.append((kind, base / (name + suffix), 'known_location'))
        # Resolve known package-native binaries without reading or executing shims.
        for pattern in ('node_modules/@openai/codex/vendor/*/codex/codex.exe',
                        'node_modules/@openai/codex-*/vendor/*/codex/codex.exe'):
            candidates.extend(('codex', path, 'known_package') for path in _bounded_glob(base, pattern))
    return candidates


def _record(kind, path, source):
    executable = str(path.resolve()) if path else None
    identity = kind + ':' + (os.path.normcase(executable) if executable else 'not_installed')
    installed = bool(path and path.is_file())
    return {'id': kind + '-' + hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20],
            'kind': kind, 'name': AGENTS[kind], 'executable': executable,
            'installed': installed, 'status': 'discovered' if installed else 'not_installed',
            'version': None, 'interface_status': 'desktop_only' if source == 'desktop' else 'unchecked', 'auth_status': 'unknown',
            'task_validation': 'not_tested', 'authorized': False, 'source': source}


def discover_agents(manual_entries=None):
    """List known entries without launching processes or reading file contents.

    Manual entries: [{"kind": "codex", "executable": "C:/.../codex.exe"}].
    A missing manual entry remains visible; each absent vendor gets one record.
    """
    if manual_entries is None:
        manual_entries = []
    if not isinstance(manual_entries, list) or len(manual_entries) > 64:
        raise ValueError('manual_entries must be a list of at most 64 entries')
    manual = []
    for entry in manual_entries:
        if not isinstance(entry, dict) or entry.get('kind') not in AGENTS:
            raise ValueError('manual entry requires a recognized kind')
        value = entry.get('executable')
        if (not isinstance(value, str) or not value or len(value) > 4096
                or any(ord(char) < 32 for char in value)):
            raise ValueError('manual executable must be an absolute file path')
        path = Path(value)
        if not path.is_absolute():
            raise ValueError('manual executable must be an absolute file path')
        manual.append((entry['kind'], path, 'manual'))
    candidates = []
    for value in os.environ.get('PATH', '').split(os.pathsep)[:128]:
        # Empty/relative PATH entries could scan the private project cwd.
        path = Path(value.strip('"')) if value else None
        if path is None or not path.is_absolute():
            continue
        for kind, names in COMMANDS.items():
            for name in names:
                for suffix in ('.exe', '.cmd', '.ps1', ''):
                    candidates.append((kind, path / (name + suffix), 'path'))
        for pattern in ('node_modules/@openai/codex/vendor/*/codex/codex.exe',
                        'node_modules/@openai/codex-*/vendor/*/codex/codex.exe'):
            candidates.extend(('codex', p, 'known_package') for p in _bounded_glob(path, pattern))
    candidates.extend(_known_candidates())
    candidates.extend(manual)
    result = {}
    for kind, path, source in candidates:
        try:
            if source != 'manual' and not path.is_file():
                continue
            record = _record(kind, path, source)
        except (OSError, ValueError):
            continue
        result.setdefault(record['id'], record)
    kinds = {record['kind'] for record in result.values()}
    for kind in AGENTS:
        if kind not in kinds:
            record = _record(kind, None, 'not_found')
            result[record['id']] = record
    order = {kind: index for index, kind in enumerate(AGENTS)}
    return sorted(result.values(), key=lambda r: (order[r['kind']], (r['executable'] or '').lower()))


def _run_probe(command, timeout=PROBE_TIMEOUT, output_limit=PROBE_OUTPUT_LIMIT):
    """Drain only a bounded byte count; timeout/output overflow terminates the CLI.

    Return (returncode, bounded text, error). Probe callers never expose raw text.
    No shell, project cwd, preload vars, or provider secrets are inherited.
    """
    chunks = bytearray()
    overflow = threading.Event()
    env = {key: os.environ[key] for key in
           ('PATH', 'SystemRoot', 'WINDIR', 'USERPROFILE', 'HOME', 'LOCALAPPDATA',
            'APPDATA', 'TEMP', 'TMP', 'PATHEXT') if key in os.environ}
    env.update({'CI': '1', 'NO_COLOR': '1'})
    with tempfile.TemporaryDirectory(prefix='xijian-agent-probe-') as workspace:
        for key in ('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'XDG_CONFIG_HOME',
                    'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'XDG_STATE_HOME', 'CODEX_HOME',
                    'CLAUDE_CONFIG_DIR', 'OPENCODE_TEST_HOME'):
            folder = Path(workspace) / key.lower()
            folder.mkdir()
            env[key] = str(folder)
        env.update(OPENCODE_DISABLE_MODELS_FETCH='true', OPENCODE_DISABLE_AUTOUPDATE='true',
                   OPENCODE_DISABLE_PROJECT_CONFIG='true', OPENCODE_PURE='true',
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
        tree = None
        try:
            options = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, shell=False, cwd=workspace, env=env,
                start_new_session=os.name != 'nt',
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if os.name == 'nt':
                from tools.workbench_qoder import _spawn_in_job
                process, tree = _spawn_in_job(command, **options)
            else:
                process = subprocess.Popen(command, **options)
        except (OSError, ValueError):
            return None, '', 'process_tree_unavailable' if os.name == 'nt' else 'launch_failed'
        def read_output():
            try:
                while True:
                    block = process.stdout.read(1024)
                    if not block:
                        break
                    room = output_limit - len(chunks)
                    chunks.extend(block[:max(room, 0)])
                    if len(block) > room:
                        overflow.set()
                        break
            except (OSError, ValueError):
                pass
        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        error = None
        while process.poll() is None:
            if overflow.is_set():
                error = 'output_limit'
                break
            if time.monotonic() >= deadline:
                error = 'timeout'
                break
            time.sleep(0.01)
        if error:
            if tree is not None:
                tree.terminate()
            else:
                import signal
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
        if tree is not None:
            tree.close()
        elif not error:
            # A completed help parent must not leave background children alive.
            import signal
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
        reader.join(timeout=0.2)
        if overflow.is_set():
            error = 'output_limit'
        # Do not synchronously close a pipe while a child still holds its writer.
        if not reader.is_alive():
            process.stdout.close()
        return process.returncode, bytes(chunks).decode('utf-8', errors='replace'), error


def probe_agent(record):
    """Explicit bounded version/help check; never authenticates or generates.

    Compatible means the required CLI flags are advertised, not task-verified.
    Qoder desktop and shell wrappers need their specific trusted adapter path.
    """
    result = copy.deepcopy(record)
    result.update({'version': None, 'interface_status': 'unchecked'})
    kind = result.get('kind')
    path = Path(result.get('executable') or '')
    # Electron desktop entrypoints may open personal sessions even with --help.
    # Recognize the package by metadata before any CLI query, including manual paths.
    if (result.get('source') == 'desktop' or (path.parent / 'resources' / 'app.asar').is_file()
            or (path.parent / 'resources' / 'app' / 'product.json').is_file()):
        result['interface_status'] = 'desktop_only'
        return result
    if kind not in AGENTS or kind in ('qoder', 'custom_acp') or path.suffix.lower() in ('.cmd', '.bat', '.ps1', '.js', '.mjs', '.cjs'):
        result['interface_status'] = 'entry_unsupported'
        return result
    if (not path.is_absolute() or path.name.lower() not in tuple(name + suffix for name in COMMANDS[kind] for suffix in ('', '.exe'))
            or (os.name == 'nt' and path.suffix.lower() != '.exe')):
        result['interface_status'] = 'entry_unsupported'
        return result
    if not path.is_file():
        result.update({'installed': False, 'status': 'not_installed', 'interface_status': 'unavailable'})
        return result
    for args in (['--version'], ['exec', '--help'] if kind == 'codex' else
                 ['acp', '--help'] if kind == 'opencode' else ['--help']):
        code, output, error = _run_probe([str(path)] + args)
        if error or code != 0:
            result['interface_status'] = error or 'probe_failed'
            return result
        if args == ['--version']:
            match = re.search(r'\b(\d{1,4}\.\d{1,4}\.\d{1,4}(?:[-+][a-zA-Z0-9.-]{1,60})?)\b', output)
            if not match:
                result['interface_status'] = 'version_unknown'
                return result
            result['version'] = match.group(1)
        elif kind == 'codex':
            result['interface_status'] = 'compatible' if all(flag in output for flag in CODEX_FLAGS) else 'incompatible'
        elif kind == 'claude':
            from tools.workbench_claude import REQUIRED_FLAGS
            result['interface_status'] = 'compatible' if all(flag in output for flag in REQUIRED_FLAGS) else 'incompatible'
        elif kind == 'opencode':
            result['interface_status'] = 'compatible' if re.search(r'\bacp\b|Agent Client Protocol', output, re.I) else 'incompatible'
        else:
            result['interface_status'] = 'adapter_unsupported'
    result['status'] = 'probed'
    return result


def select_agent(records, preferences):
    """Resolve explicit prior consent before submission, with no live checks.

    Preferences: approved_ids (default priority), priority (optional ordered IDs), fixed_id (optional),
    allow_fallback (bool), submission_started (bool, blocks any reselection).
    Auth eligibility is established separately by the adapter, never discovery.
    """
    if not isinstance(preferences, dict):
        return {'selected': None, 'reason': 'invalid_preferences'}
    if preferences.get('submission_started'):
        return {'selected': None, 'reason': 'submission_already_started'}
    approved = preferences.get('approved_ids', [])
    if not isinstance(approved, list) or any(not isinstance(item, str) for item in approved):
        return {'selected': None, 'reason': 'invalid_preferences'}
    priority = preferences.get('priority', approved)
    if not isinstance(priority, list) or any(not isinstance(item, str) for item in priority):
        return {'selected': None, 'reason': 'invalid_preferences'}
    by_id = {record.get('id'): record for record in records}
    def eligible(identity):
        record = by_id.get(identity)
        return bool(identity in approved and record and record.get('installed') is True
                    and record.get('interface_status') == 'compatible'
                    and record.get('authorized') is True
                    and record.get('auth_status') in ('authenticated', 'not_required'))
    fixed = preferences.get('fixed_id')
    if fixed:
        return {'selected': copy.deepcopy(by_id[fixed]) if eligible(fixed) else None,
                'reason': 'fixed_selected' if eligible(fixed) else 'fixed_unavailable'}
    if not approved:
        return {'selected': None, 'reason': 'no_approved_agents'}
    # A priority entry that is no longer approved blocks the preferred route;
    # skipping it requires explicit pre-submission fallback permission.
    choices = priority if preferences.get('allow_fallback') is True else priority[:1]
    for index, identity in enumerate(choices):
        if eligible(identity):
            return {'selected': copy.deepcopy(by_id[identity]),
                    'reason': 'preferred_selected' if index == 0 else 'approved_fallback_selected'}
    return {'selected': None, 'reason': 'no_eligible_approved_agent'}
