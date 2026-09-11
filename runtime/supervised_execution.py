"""Opt-in Windows supervision for newly authored synthetic candidates only."""
import ctypes
from ctypes import wintypes as wt
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time


@dataclass(frozen=True)
class Limits:
    timeout_seconds: float = 10.0
    memory_bytes: int = 512 * 1024 * 1024
    output_bytes: int = 65536
    result_bytes: int = 65536
    input_bytes: int = 65536
    candidate_bytes: int = 1024 * 1024
    process_count: int = 8

    def __post_init__(self):
        if (isinstance(self.timeout_seconds, bool) or
                not isinstance(self.timeout_seconds, (int, float)) or
                not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0):
            raise ValueError("invalid deadline")
        for name in ("memory_bytes", "output_bytes", "result_bytes", "input_bytes",
                     "candidate_bytes", "process_count"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError("invalid limit")
        if self.result_bytes < 256:
            raise ValueError("result budget too small for protocol")


@dataclass(frozen=True)
class ExecutionResult:
    receipt: dict
    payload: object = None
    pid: object = None


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wt.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wt.DWORD), ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits),
                ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _Accounting(ctypes.Structure):
    _fields_ = [("times", ctypes.c_int64 * 4), ("faults", wt.DWORD),
                ("total", wt.DWORD), ("active", wt.DWORD), ("terminated", wt.DWORD)]


class _CompletionPort(ctypes.Structure):
    _fields_ = [("key", ctypes.c_void_p), ("port", wt.HANDLE)]


class _WindowsJob:
    """Kernel-enforced aggregate/process commit limits and tree lifetime."""
    def __init__(self, limits):
        if os.name != "nt":
            raise OSError("Windows enforcement unavailable")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wt.LPCWSTR], wt.HANDLE),
            "SetInformationJobObject": ([wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD], wt.BOOL),
            "QueryInformationJobObject": ([wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p], wt.BOOL),
            "AssignProcessToJobObject": ([wt.HANDLE, wt.HANDLE], wt.BOOL),
            "TerminateJobObject": ([wt.HANDLE, wt.UINT], wt.BOOL),
            "CloseHandle": ([wt.HANDLE], wt.BOOL),
            "OpenProcess": ([wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE),
            "CreateIoCompletionPort": ([wt.HANDLE, wt.HANDLE, ctypes.c_size_t, wt.DWORD], wt.HANDLE),
            "GetQueuedCompletionStatus": ([wt.HANDLE, ctypes.POINTER(wt.DWORD), ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_void_p), wt.DWORD], wt.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result
        self.handle = self.kernel.CreateJobObjectW(None, None)
        self.port = None
        try:
            self._check(self.handle)
            info = _ExtendedLimits()
            # ACTIVE_PROCESS | PROCESS_MEMORY | JOB_MEMORY | KILL_ON_JOB_CLOSE.
            info.BasicLimitInformation.LimitFlags = 0x8 | 0x100 | 0x200 | 0x2000
            info.BasicLimitInformation.ActiveProcessLimit = limits.process_count
            info.ProcessMemoryLimit = info.JobMemoryLimit = limits.memory_bytes
            self._check(self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)))
            self.port = self.kernel.CreateIoCompletionPort(ctypes.c_void_p(-1), None, 0, 1)
            self._check(self.port)
            association = _CompletionPort(1, self.port)
            self._check(self.kernel.SetInformationJobObject(self.handle, 7, ctypes.byref(association), ctypes.sizeof(association)))
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _check(value):
        if not value:
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, pid):
        handle = self.kernel.OpenProcess(0x0101, False, pid)
        self._check(handle)
        try:
            self._check(self.kernel.AssignProcessToJobObject(self.handle, handle))
        finally:
            self.kernel.CloseHandle(handle)

    def violation(self):
        result = None
        for _ in range(128):
            message, key, pointer = wt.DWORD(), ctypes.c_size_t(), ctypes.c_void_p()
            if not self.kernel.GetQueuedCompletionStatus(self.port, ctypes.byref(message), ctypes.byref(key), ctypes.byref(pointer), 0):
                break
            if message.value in (9, 10):
                result = "memory_limit"
            elif message.value == 3 and result is None:
                result = "process_limit"
        return result

    def terminate(self):
        self._check(self.kernel.TerminateJobObject(self.handle, 1))

    def active(self):
        info = _Accounting()
        self._check(self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None))
        return info.active

    def close(self):
        for name in ("handle", "port"):
            handle = getattr(self, name, None)
            if handle:
                self.kernel.CloseHandle(handle)
                setattr(self, name, None)


def _environment(tempdir):
    # Do not copy parent PATH, home, PYTHON*, or arbitrary provider credentials.
    env = {name: os.environ[name] for name in ("SystemRoot", "WINDIR") if name in os.environ}
    env.update(TEMP=tempdir, TMP=tempdir, OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1", OMP_NUM_THREADS="1")
    return env


def execute(candidate_path, expected_sha256, synthetic_records, limits=None,
            normalization="json", cancel=None):
    """Run once; only receipt is suitable for public logging (see contract)."""
    limits = limits or Limits()
    receipt = {"status": "input_invalid", "stage": "startup",
               "limits": asdict(limits), "enforcement_installed": False,
               "cleanup_complete": True, "output_bytes_seen": 0,
               "output_bytes_retained": 0, "result_bytes": 0}
    try:
        if (normalization != "json" or not isinstance(synthetic_records, list) or
                not isinstance(expected_sha256, str) or len(expected_sha256) != 64 or
                any(c not in "0123456789abcdef" for c in expected_sha256)):
            return ExecutionResult(receipt)
        encoded = json.dumps(synthetic_records, allow_nan=False).encode("utf-8")
        if len(encoded) > limits.input_bytes:
            return ExecutionResult(receipt)
        path = str(Path(candidate_path).resolve())
    except (ValueError, TypeError, OSError):
        return ExecutionResult(receipt)
    if cancel is not None and cancel.is_set():
        receipt["status"] = "cancelled"
        return ExecutionResult(receipt)
    proc, job, threads, forced, payload = None, None, [], None, None
    overflow = threading.Event()
    output_lock = threading.Lock()

    def drain(stream):
        while True:
            block = stream.read1(4096)
            if not block:
                return
            with output_lock:
                receipt["output_bytes_seen"] += len(block)
                if receipt["output_bytes_seen"] >= limits.output_bytes:
                    overflow.set()

    with tempfile.TemporaryDirectory(prefix="synthetic-supervision-") as folder:
        folder = Path(folder)
        request = {"candidate": path, "sha256": expected_sha256,
                   "records": synthetic_records, "limits": asdict(limits)}
        (folder / "request.json").write_text(json.dumps(request, allow_nan=False), encoding="utf-8")
        try:
            job = _WindowsJob(limits)
            started = time.monotonic()
            worker = str(Path(__file__).with_name("execution_worker.py").resolve())
            proc = subprocess.Popen([sys.executable, "-I", "-u", worker, str(folder)],
                                    cwd=str(folder), env=_environment(str(folder)),
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, shell=False,
                                    creationflags=0x08000000)
            for stream in (proc.stdout, proc.stderr):
                thread = threading.Thread(target=drain, args=(stream,), daemon=True)
                thread.start()
                threads.append(thread)
            job.assign(proc.pid)
            receipt["enforcement_installed"] = True
            proc.stdin.write(b"G")
            proc.stdin.close()
            while True:
                violation = job.violation()
                if cancel is not None and cancel.is_set():
                    forced = "cancelled"
                elif time.monotonic() - started >= limits.timeout_seconds:
                    forced = "timeout"
                elif overflow.is_set():
                    forced = "output_limit"
                elif violation:
                    forced = violation
                if forced or proc.poll() is not None:
                    break
                time.sleep(0.005)
        except KeyboardInterrupt:
            forced = "cancelled"
        except (OSError, ValueError, subprocess.SubprocessError):
            forced = "enforcement_unavailable" if not receipt["enforcement_installed"] else "abnormal_exit"
        finally:
            cleanup_deadline = time.monotonic() + 5
            if job is not None:
                try:
                    forced = forced or job.violation()
                    job.terminate()
                    while job.active() and time.monotonic() < cleanup_deadline:
                        time.sleep(0.005)
                    receipt["cleanup_complete"] = job.active() == 0
                except OSError:
                    receipt["cleanup_complete"] = False
                finally:
                    job.close()
            if proc is not None:
                if proc.poll() is None:
                    proc.kill()
                try:
                    proc.wait(timeout=max(0.01, cleanup_deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    receipt["cleanup_complete"] = False
                for thread in threads:
                    thread.join(timeout=max(0.01, cleanup_deadline - time.monotonic()))
                if any(thread.is_alive() for thread in threads):
                    receipt["cleanup_complete"] = False
                else:
                    for stream in (proc.stdout, proc.stderr):
                        stream.close()
                if not proc.stdin.closed:
                    proc.stdin.close()
        try:
            with (folder / "stage").open("rb") as stage_file:
                stage = stage_file.read(32).decode("ascii")
            if stage in {"startup", "load", "analyze", "normalize", "complete"}:
                receipt["stage"] = stage
        except (OSError, UnicodeError):
            pass
        if overflow.is_set() and forced not in {"cancelled", "timeout"}:
            forced = "output_limit"
        if not receipt["cleanup_complete"]:
            forced = "cleanup_failed"
        if forced:
            receipt["status"] = forced
        elif proc.returncode != 0:
            receipt["status"] = "abnormal_exit"
        else:
            receipt["status"] = "invalid_worker_output"
            try:
                with (folder / "result.json").open("rb") as result_file:
                    raw = result_file.read(limits.result_bytes + 1)
                receipt["result_bytes"] = len(raw)
                if len(raw) > limits.result_bytes:
                    receipt["status"] = "result_limit"
                else:
                    message = json.loads(raw.decode("utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
                    allowed = {"success", "load_error", "analyze_error", "normalization_error", "system_exit", "digest_mismatch", "result_limit"}
                    if (type(message) is dict and set(message) == {"status", "payload"} and
                            message["status"] in allowed and
                            (message["status"] == "success" or message["payload"] is None)):
                        receipt["status"] = message["status"]
                        if message["status"] == "success":
                            payload = message["payload"]
            except (OSError, ValueError, UnicodeError, TypeError):
                pass
    return ExecutionResult(receipt, payload, proc.pid if proc else None)
