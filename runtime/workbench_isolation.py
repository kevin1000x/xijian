"""Broker for newly executed workbench candidates. Only synthetic use is qualified."""
from contextlib import ExitStack
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import threading
import time

from runtime.supervised_execution import ExecutionResult, Limits, _WindowsJob, _environment

POLICY = 'windows-appcontainer-zero-capabilities-single-process-v1'
DEV_MANIFEST = '40e6a059de50fbd6042de318c0cce05d2113cad6d2ba0beba3eb951b31df0d20'
_cache_lock = threading.Lock()
_cache = None


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _distribution():
    if getattr(sys, 'frozen', False):
        directory = Path(sys.executable).resolve().parent
        manifest = directory.parent / 'manifest.json'
    else:
        directory = Path(__file__).resolve().parents[1] / 'dist' / 'windows' / '0.4.0-dev.3' / 'Xijian'
        manifest = directory.parent / 'manifest.json'
    if manifest.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('runtime manifest size')
    content = manifest.read_bytes()
    if not getattr(sys, 'frozen', False) and _hash(content) != DEV_MANIFEST:
        raise ValueError('runtime manifest identity')
    return directory, content


def identity():
    # Identity is available even on a source checkout without an installed runner.
    if getattr(sys, 'frozen', False):
        manifest = _hash(_distribution()[1])
    else:
        manifest = DEV_MANIFEST
    directory = Path(__file__).resolve().parent
    return {'policy': POLICY, 'manifest_sha256': manifest,
            'broker_sha256': _hash((directory / 'workbench_isolation.py').read_bytes()),
            'launcher_sha256': _hash((directory / 'workbench_win32.py').read_bytes())}


def _runtime(win):
    global _cache
    with _cache_lock:
        if _cache is not None:
            return Path(_cache.name) / 'XijianHost.exe'
        source, raw = _distribution()
        manifest = json.loads(raw)
        if manifest['platform'] != 'windows-x64':
            raise ValueError('runtime platform')
        temp = win.PrivateDirectory('xijian-public-runtime-')
        root = Path(temp.name)
        try:
            win.grant(root, 'S-1-15-2-2', '(OI)(CI)(RX)')
            seen = set()
            for item in manifest['files']:
                name = item['path']
                if name == 'Xijian.exe':
                    continue
                relative = PurePosixPath(name)
                if (not relative.parts or relative.as_posix() != name or '..' in relative.parts or
                    '\\' in name or ':' in name or relative.is_absolute() or name.casefold() in seen or
                    (name != 'XijianHost.exe' and relative.parts[0] != '_internal')):
                    raise ValueError('invalid runtime member')
                seen.add(name.casefold())
                target, original = root / name, source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                with original.open('rb') as src, target.open('xb') as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        dst.write(chunk)
                if target.stat().st_size != item['bytes'] or digest.hexdigest() != item['sha256']:
                    raise ValueError('runtime component changed')
            if 'xijianhost.exe' not in seen or '_internal/runtime/execution_worker.py' not in seen:
                raise ValueError('missing runtime components')
            _cache = temp
        except BaseException:
            temp.cleanup()
            raise
        return root / 'XijianHost.exe'


def _initial(limits):
    return {'status':'input_invalid','stage':'startup','limits':asdict(limits),
            'enforcement_installed':False,'cleanup_complete':True,'output_bytes_seen':0,
            'output_bytes_retained':0,'result_bytes':0,
            'isolation':{'policy':POLICY,'token_verified':False,'lpac':False,
                         'capability_count':None,'job_process_limit':1,'phase':'input','os_error':None}}


def _session(win, profile, host, folder, source, expected, records, limits, cancel, receipt):
    policy = receipt['isolation']
    program = folder / 'candidate.py'
    program.write_bytes(source)
    (folder / 'request.json').write_text(json.dumps({'candidate':str(program), 'sha256':expected,
        'records':records,'limits':asdict(limits)}, allow_nan=False), encoding='utf-8')
    stage = folder / 'stage'
    result = folder / 'result.json'
    stage.touch()
    result.touch()
    win.grant(folder,profile.text,'(OI)(CI)(RX)')
    for item in (stage, result):
        win.grant(item,profile.text,'(W)',low=True)
    # Keep original file handles across execution. Neither directory nor name is writable.
    with ExitStack() as owned:
        stage_fd = owned.enter_context(stage.open('rb'))
        result_fd = owned.enter_context(result.open('rb'))
        job = _WindowsJob(replace(limits, process_count=1))
        owned.callback(job.close)
        stdin_read, stdin_write = os.pipe()
        stdout_read, stdout_write = os.pipe()
        for fd in (stdin_read,stdin_write,stdout_read,stdout_write):
            owned.callback(os.close, fd)
        policy['phase'] = 'launch'
        command = [host,'-I','-u',host.parent/'_internal'/'runtime'/'execution_worker.py',folder]
        env = _environment(str(folder))
        env.update(LOCALAPPDATA=str(folder), APPDATA=str(folder), USERPROFILE=str(folder))
        proc = win.start(profile, command, folder, env, stdin_read, stdout_write, job)
        owned.callback(win.close, proc.process)
        payload = None
        try:
            policy.update(token_verified=True,capability_count=0,phase='worker')
            receipt['enforcement_installed'] = True
            os.write(stdin_write,b'G')
            deadline = time.monotonic() + limits.timeout_seconds
            while True:
                amount = win.drain(stdout_read, limits.output_bytes - receipt['output_bytes_seen'])
                receipt['output_bytes_seen'] += amount
                violation = job.violation()
                if cancel is not None and cancel.is_set():
                    receipt['status']='cancelled'
                elif time.monotonic() >= deadline:
                    receipt['status']='timeout'
                elif receipt['output_bytes_seen'] >= limits.output_bytes:
                    receipt['status']='output_limit'
                elif os.fstat(result_fd.fileno()).st_size > limits.result_bytes or os.fstat(stage_fd.fileno()).st_size > 32:
                    receipt['status']='result_limit'
                elif violation:
                    receipt['status']=violation
                elif win.wait_for(proc.process, 0) == 0 and not amount:
                    code = win.DWORD()
                    win.check(win.exit_code(proc.process, win.ref(code)))
                    receipt['status']='invalid_worker_output' if code.value == 0 else 'abnormal_exit'
                    policy['os_error'] = code.value if code.value else None
                else:
                    time.sleep(0.005)
                    continue
                break
        finally:
            try:
                job.terminate()
                until = time.monotonic() + 5
                while job.active() and time.monotonic() < until:
                    time.sleep(0.01)
                receipt['cleanup_complete'] = job.active() == 0
            except OSError:
                receipt['cleanup_complete'] = False
        stage_value = stage_fd.read(32).decode('ascii',errors='replace')
        if stage_value in {'startup','load','analyze','normalize','complete'}:
            receipt['stage']=stage_value
        if receipt['status']=='invalid_worker_output':
            raw = result_fd.read(limits.result_bytes + 1)
            receipt['result_bytes'] = len(raw)
            if len(raw) > limits.result_bytes:
                receipt['status']='result_limit'
            else:
                try:
                    answer = json.loads(raw.decode('utf-8'),parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    if (type(answer) is dict and set(answer)=={'status','payload'} and
                        answer['status'] in {'success','load_error','analyze_error','normalization_error','system_exit','digest_mismatch','result_limit'} and
                        (answer['status']=='success' and receipt['stage']=='complete' or
                         answer['status']!='success' and answer['payload'] is None)):
                        receipt['status'] = answer['status']
                        payload = answer['payload']
                except (ValueError,UnicodeError):
                    pass
        return payload, proc.pid


def execute(candidate_path, expected_sha256, synthetic_records, limits=None, cancel=None):
    limits = limits or Limits()
    receipt = _initial(limits)
    payload, pid = None, None
    try:
        if type(synthetic_records) is not list or len(json.dumps(synthetic_records,allow_nan=False).encode('utf-8')) > limits.input_bytes:
            return ExecutionResult(receipt)
        with Path(candidate_path).open('rb') as stream:
            source = stream.read(limits.candidate_bytes + 1)
        if len(source) > limits.candidate_bytes or _hash(source) != expected_sha256:
            receipt['status']='digest_mismatch'
            return ExecutionResult(receipt)
        if cancel is not None and cancel.is_set():
            receipt['status']='cancelled'
            return ExecutionResult(receipt)
        receipt['status']='enforcement_unavailable'
        receipt['isolation']['phase']='runtime'
        if os.name != 'nt':
            return ExecutionResult(receipt)
        from runtime import workbench_win32 as win
        host = _runtime(win)
        receipt['isolation']['phase']='profile'
        with win.Profile() as profile:
            with win.PrivateDirectory('xijian-synthetic-task-') as temp:
                receipt['isolation']['phase']='workspace'
                payload,pid = _session(win,profile,host,Path(temp),source,expected_sha256,
                                       synthetic_records,limits,cancel,receipt)
                receipt['isolation']['phase']='cleanup'
        receipt['isolation']['phase']='complete'
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        code = getattr(error,'winerror',None) or getattr(error,'errno',None) or getattr(error,'returncode',None)
        receipt['isolation']['os_error'] = code if type(code) is int else None
        if receipt['isolation']['phase']=='cleanup':
            receipt['cleanup_complete']=False
        receipt['status']='abnormal_exit' if receipt['enforcement_installed'] else 'enforcement_unavailable'
    if not receipt['cleanup_complete']:
        receipt['status']='cleanup_failed'
    return ExecutionResult(receipt,payload if receipt['status']=='success' else None,pid)
