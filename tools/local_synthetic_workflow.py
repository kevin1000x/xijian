"""Fresh trusted-synthetic local batches; no providers, retry or formal authority."""
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid

from runtime.supervised_execution import Limits, execute
from tools.local_attempt_ledger import Ledger, LedgerError

MAX_JSON = 2 * 1024 * 1024
SCOPE = "synthetic-local-qualification"
STATUSES = ("SUCCESS", "FAILED", "SCORE_ABORT", "UNKNOWN_BLOCKED", "NOT_RUN")
EXEC_STATUSES = {"input_invalid", "cancelled", "enforcement_unavailable", "abnormal_exit",
                 "cleanup_failed", "timeout", "output_limit", "memory_limit", "process_limit",
                 "invalid_worker_output", "result_limit", "success", "load_error", "analyze_error",
                 "normalization_error", "system_exit", "digest_mismatch"}


class WorkflowError(RuntimeError):
    """Fixed local evidence failure; do not retry an existing batch."""


def _boundary(label):
    """Persistence test seam, never a candidate callback."""


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _fail():
    raise WorkflowError("invalid local evidence")


def _safe(path):
    for part in (path,) + tuple(path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
            _fail()


def _read(path, maximum=MAX_JSON):
    _safe(path)
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        _fail()
    return raw


def _json(path):
    raw = _read(path)
    value = json.loads(raw.decode("utf-8"))
    if _bytes(value) != raw:
        _fail()
    return value


def _keys(value, keys):
    if type(value) is not dict or set(value) != set(keys.split()):
        _fail()


def _digest(value):
    if type(value) is not str or not re.fullmatch("[0-9a-f]{64}", value):
        _fail()


def _entries(root, names):
    for entry in root.iterdir():
        _safe(entry)
        if entry.name not in names and not (entry.is_file() and re.fullmatch(r"\.tmp-[0-9a-f]{32}", entry.name)):
            _fail()


def _publish(path, raw):
    _safe(path)
    if path.exists():
        if _read(path, max(MAX_JSON, len(raw))) != raw:
            _fail()
        return
    temporary = path.with_name(".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


class _Writer:
    def __init__(self, root, create=False):
        self.root, self.create, self.stream = root, create, None

    def __enter__(self):
        if os.name != "nt":
            _fail()
        import msvcrt
        _safe(self.root)
        if self.create:
            self.root.mkdir()
        path = self.root / "writer.lock"
        _safe(path)
        stream = path.open("x+b" if self.create else "r+b", buffering=0)
        try:
            if self.create:
                stream.write(b"0")
                os.fsync(stream.fileno())
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            if stream.read(2) != b"0":
                _fail()
            self.stream = stream
        except BaseException:
            stream.close()
            raise
        return self

    def __exit__(self, *unused):
        self.stream.close()


def runtime_identity(limits=None):
    limits = limits or Limits()
    if type(limits) is not Limits:
        _fail()
    if (limits.timeout_seconds > 300 or limits.memory_bytes > 2 * 1024 ** 3 or
            limits.process_count > 16 or any(getattr(limits, name) > 1024 ** 2 for name in
            ("input_bytes", "candidate_bytes", "result_bytes", "output_bytes"))):
        _fail()
    runtime = Path(__file__).resolve().parents[1] / "runtime"
    # Content identity deliberately excludes installation paths.
    return {"interpreter_version": list(sys.version_info[:3]),
            "executable_sha256": _hash(_read(Path(sys.executable), 64 * 1024 ** 2)),
            "supervisor_sha256": _hash(_read(runtime / "supervised_execution.py")),
            "worker_sha256": _hash(_read(runtime / "execution_worker.py")),
            "limits": asdict(limits), "input_policy": "records-as-dataframe", "normalization": "json"}


def _slot_id(value):
    if (type(value) is not str or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", value) or
            re.fullmatch(r"(?i:con|prn|aux|nul|com[0-9]|lpt[0-9])", value) or
            value.lower() in {"writer", "manifest", "report"}):
        _fail()


def _request(request, batch_id, name, runtime):
    _keys(request, "version batch_id slot_id records expected")
    if (type(request["version"]) is not int or request["version"] != 1 or
            request["batch_id"] != batch_id or request["slot_id"] != name or
            type(request["records"]) is not list or len(request["records"]) > 4096 or
            any(type(row) is not dict for row in request["records"]) or
            len(json.dumps(request["records"], allow_nan=False).encode("utf-8")) > runtime["limits"]["input_bytes"] or
            len(_bytes(request["expected"])) > runtime["limits"]["result_bytes"]):
        _fail()


def _receipt(receipt, runtime):
    _keys(receipt, "status stage limits enforcement_installed cleanup_complete output_bytes_seen output_bytes_retained result_bytes")
    if (type(receipt["status"]) is not str or receipt["status"] not in EXEC_STATUSES or
            receipt["stage"] not in {"startup", "load", "analyze", "normalize", "complete"} or
            _bytes(receipt["limits"]) != _bytes(runtime["limits"]) or
            type(receipt["enforcement_installed"]) is not bool or type(receipt["cleanup_complete"]) is not bool or
            any(type(receipt[k]) is not int or receipt[k] < 0 for k in
                ("output_bytes_seen", "output_bytes_retained", "result_bytes")) or
            receipt["output_bytes_retained"] != 0 or receipt["result_bytes"] > runtime["limits"]["result_bytes"] + 1):
        _fail()
    if receipt["status"] == "success" and (receipt["stage"] != "complete" or
            not receipt["enforcement_installed"] or not receipt["cleanup_complete"] or
            not 0 < receipt["result_bytes"] <= runtime["limits"]["result_bytes"] or
            receipt["output_bytes_seen"] >= runtime["limits"]["output_bytes"]):
        _fail()


def _execution(record, entry, request, runtime):
    _keys(record, "version slot_id request_sha256 candidate_sha256 runtime_sha256 receipt expected_sha256 observed_sha256")
    if (type(record["version"]) is not int or record["version"] != 1 or
            any(record[k] != entry[k] for k in ("slot_id", "request_sha256", "candidate_sha256")) or
            record["runtime_sha256"] != _hash(_bytes(runtime)) or
            record["expected_sha256"] != _hash(_bytes(request["expected"]))):
        _fail()
    _receipt(record["receipt"], runtime)
    if record["receipt"]["status"] == "success":
        _digest(record["observed_sha256"])
    elif record["observed_sha256"] is not None:
        _fail()
    receipt = record["receipt"]
    if (receipt["status"] in {"cancelled", "enforcement_unavailable", "cleanup_failed", "input_invalid"} or
            not receipt["enforcement_installed"] or not receipt["cleanup_complete"]):
        return "SCORE_ABORT"
    return "SUCCESS" if receipt["status"] == "success" and record["observed_sha256"] == record["expected_sha256"] else "FAILED"


def _load(root):
    manifest = _json(root / "manifest.json")
    _keys(manifest, "version scope batch_id runtime slots")
    if (type(manifest["version"]) is not int or manifest["version"] != 1 or manifest["scope"] != SCOPE or
            type(manifest["batch_id"]) is not str or not re.fullmatch(r"synthetic-[A-Za-z0-9_-]{1,64}", manifest["batch_id"]) or
            type(manifest["slots"]) is not list or not 1 <= len(manifest["slots"]) <= 32):
        _fail()
    runtime = manifest["runtime"]
    _keys(runtime, "interpreter_version executable_sha256 supervisor_sha256 worker_sha256 limits input_policy normalization")
    if _bytes(runtime) != _bytes(runtime_identity(Limits(**runtime["limits"]))):
        _fail()
    names, requests = set(), []
    for entry in manifest["slots"]:
        _keys(entry, "slot_id request_sha256 candidate_sha256")
        name = entry["slot_id"]
        _slot_id(name)
        if name.lower() in names:
            _fail()
        names.add(name.lower())
        for key in ("request_sha256", "candidate_sha256"):
            _digest(entry[key])
        folder = root / name
        _safe(folder)
        _entries(folder, {"request.json", "candidate.py", "execution.json", "ledger"})
        request = _json(folder / "request.json")
        _request(request, manifest["batch_id"], name, runtime)
        if (_hash(_bytes(request)) != entry["request_sha256"] or
                _hash(_read(folder / "candidate.py", runtime["limits"]["candidate_bytes"])) != entry["candidate_sha256"]):
            _fail()
        requests.append(request)
    _entries(root, {"manifest.json", "report.json", "writer.lock"} | {e["slot_id"] for e in manifest["slots"]})
    return manifest, requests


def _ledger_id(batch_id, slot_id):
    return "synthetic-" + _hash(_bytes([batch_id, slot_id]))[:48]


def _recover(root):
    manifest, requests = _load(root)
    if (root / "report.json").exists():
        from tools.verify_local_synthetic_workflow import verify_report
        if not verify_report(root, manifest["runtime"])["valid"]:
            _fail()
        return _json(root / "report.json")
    # Validate *all* sidecars and bound all D reads before allowing any recovery write.
    sidecars = []
    for entry, request in zip(manifest["slots"], requests):
        folder = root / entry["slot_id"]
        record = _json(folder / "execution.json") if (folder / "execution.json").exists() else None
        classification = _execution(record, entry, request, manifest["runtime"]) if record is not None else None
        ledger = folder / "ledger"
        if ledger.exists():
            _safe(ledger)
            for path in ledger.iterdir():
                _read(path, manifest["runtime"]["limits"]["candidate_bytes"] if path.name == "candidate.bin" else 4096)
            marker = _json(ledger / "marker.json")
            _keys(marker, "version kind ledger_id prev synthetic")
            if marker["ledger_id"] != _ledger_id(manifest["batch_id"], entry["slot_id"]):
                _fail()
            if ((ledger / "candidate.bin").exists() and
                    _read(ledger / "candidate.bin", manifest["runtime"]["limits"]["candidate_bytes"]) !=
                    _read(folder / "candidate.py", manifest["runtime"]["limits"]["candidate_bytes"])):
                _fail()
            if (ledger / "terminal.json").exists():
                terminal = _json(ledger / "terminal.json")
                _keys(terminal, "version kind ledger_id prev status")
                if record is None or terminal["status"] != classification:
                    _fail()
            if record is not None and not (ledger / "candidate.json").exists():
                _fail()
        elif record is not None:
            _fail()
        sidecars.append(record)
    slots, halted = [], False
    for entry, record in zip(manifest["slots"], sidecars):
        folder = root / entry["slot_id"]
        ledger = folder / "ledger"
        status = "NOT_RUN"
        if ledger.exists():
            with Ledger.open(ledger) as writer:
                if writer.ledger_id != _ledger_id(manifest["batch_id"], entry["slot_id"]):
                    _fail()
                status = writer.recover()
                if status in {"INITIALIZED", "PREPARED"}:
                    status = "NOT_RUN"
                candidate = ledger / "candidate.bin"
                if candidate.exists() and _read(candidate, manifest["runtime"]["limits"]["candidate_bytes"]) != _read(folder / "candidate.py"):
                    _fail()
        if halted and status != "NOT_RUN":
            _fail()
        halted = halted or status != "SUCCESS"
        row = dict(entry, status=status, execution_sha256=_hash(_bytes(record)) if record is not None else None)
        for kind in ("terminal", "outcome", "unknown"):
            path = ledger / (kind + ".json")
            row[kind + "_sha256"] = _hash(_read(path)) if path.exists() else None
        slots.append(row)
    counts = {status: sum(row["status"] == status for row in slots) for status in STATUSES}
    verdict = "UNKNOWN_BLOCKED" if counts["UNKNOWN_BLOCKED"] else "QUALIFIED" if counts["SUCCESS"] == len(slots) else "ABORTED"
    report = dict(version=1, scope=SCOPE, batch_id=manifest["batch_id"], manifest_sha256=_hash(_bytes(manifest)),
                  runtime_sha256=_hash(_bytes(manifest["runtime"])), slots=slots, counts=counts, verdict=verdict)
    _boundary("report:before_publication")
    _publish(root / "report.json", _bytes(report))
    return report


def run_batch(root, batch_id, slots, limits=None, cancel=None):
    """Exclusively create and execute a fresh bounded batch once; returns report."""
    root = Path(os.path.abspath(str(root)))
    try:
        limits = limits or Limits()
        runtime = runtime_identity(limits)
        if (type(batch_id) is not str or not re.fullmatch(r"synthetic-[A-Za-z0-9_-]{1,64}", batch_id) or
                type(slots) is not list or not 1 <= len(slots) <= 32):
            _fail()
        entries, frozen, names = [], [], set()
        for slot in slots:
            _keys(slot, "slot_id candidate records expected")
            name = slot["slot_id"]
            _slot_id(name)
            if name.lower() in names or type(slot["candidate"]) is not bytes or not 0 < len(slot["candidate"]) <= limits.candidate_bytes:
                _fail()
            names.add(name.lower())
            request = dict(version=1, batch_id=batch_id, slot_id=name, records=slot["records"], expected=slot["expected"])
            _request(request, batch_id, name, runtime)
            encoded = _bytes(request)
            if len(encoded) > MAX_JSON:
                _fail()
            frozen.append((encoded, slot["candidate"]))
            entries.append(dict(slot_id=name, request_sha256=_hash(encoded), candidate_sha256=_hash(slot["candidate"])))
        with _Writer(root, create=True):
            for entry, (request, candidate) in zip(entries, frozen):
                folder = root / entry["slot_id"]
                folder.mkdir()
                _publish(folder / "request.json", request)
                _publish(folder / "candidate.py", candidate)
            manifest = dict(version=1, scope=SCOPE, batch_id=batch_id, runtime=runtime, slots=entries)
            _publish(root / "manifest.json", _bytes(manifest))
            for entry in entries:
                _, requests = _load(root)
                request = next(r for r in requests if r["slot_id"] == entry["slot_id"])
                folder = root / entry["slot_id"]
                with Ledger.create(folder / "ledger", _ledger_id(batch_id, entry["slot_id"])) as ledger:
                    ledger.prepare()
                    ledger.start()
                    _boundary("start:published")
                    ledger.candidate(_read(folder / "candidate.py", limits.candidate_bytes))
                    try:
                        result = execute(folder / "candidate.py", entry["candidate_sha256"], request["records"], limits=limits, cancel=cancel)
                    except Exception:
                        # No trustworthy observation: recovery records UNKNOWN, never a fabricated terminal.
                        break
                    record = dict(version=1, **entry, runtime_sha256=_hash(_bytes(runtime)), receipt=result.receipt,
                                  expected_sha256=_hash(_bytes(request["expected"])),
                                  observed_sha256=_hash(_bytes(result.payload)) if result.receipt["status"] == "success" else None)
                    status = _execution(record, entry, request, runtime)
                    _publish(folder / "execution.json", _bytes(record))
                    _boundary("execution:published")
                    ledger.terminal(status)
                    _boundary("terminal:published")
                    ledger.recover()
                if status != "SUCCESS":
                    break
            return _recover(root)
    except (OSError, ValueError, TypeError, KeyError, RecursionError, LedgerError):
        raise WorkflowError("invalid local evidence") from None


def recover_batch(root):
    """Never executes. Projects verified terminals or permanently blocks unknown starts."""
    root = Path(os.path.abspath(str(root)))
    try:
        with _Writer(root):
            return _recover(root)
    except (OSError, ValueError, TypeError, KeyError, RecursionError, LedgerError):
        raise WorkflowError("invalid local evidence") from None
