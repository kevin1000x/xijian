"""Independent read-only verifier for local synthetic evidence; standard library only."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import stat


def _reject():
    raise ValueError()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _safe(path):
    for item in (path,) + tuple(path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
            _reject()


def _raw(path, limit=2 * 1024 ** 2):
    _safe(path)
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        _reject()
    return data


def _json(path):
    data = _raw(path)
    value = json.loads(data.decode("utf-8"))
    if _canonical(value) != data:
        _reject()
    return value


def _fields(obj, fields):
    if type(obj) is not dict or set(obj) != set(fields.split()):
        _reject()


def _digest(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        _reject()


def _inventory(root, files, directories=()):
    _safe(root)
    if not root.is_dir():
        _reject()
    for path in root.iterdir():
        _safe(path)
        if path.name in directories:
            if not path.is_dir():
                _reject()
        elif not path.is_file() or (path.name not in files and not re.fullmatch(r"\.tmp-[0-9a-f]{32}", path.name)):
            _reject()


def _runtime(runtime):
    _fields(runtime, "interpreter_version executable_sha256 supervisor_sha256 worker_sha256 limits input_policy normalization")
    version = runtime["interpreter_version"]
    if (type(version) is not list or len(version) != 3 or
            any(type(v) is not int or v < 0 for v in version) or
            runtime["input_policy"] != "records-as-dataframe" or runtime["normalization"] != "json"):
        _reject()
    for name in ("executable_sha256", "supervisor_sha256", "worker_sha256"):
        _digest(runtime[name])
    limits = runtime["limits"]
    _fields(limits, "timeout_seconds memory_bytes output_bytes result_bytes input_bytes candidate_bytes process_count")
    timeout = limits["timeout_seconds"]
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 300:
        _reject()
    for name, value in limits.items():
        if name != "timeout_seconds" and (type(value) is not int or value <= 0):
            _reject()
    if (limits["memory_bytes"] > 2 * 1024 ** 3 or limits["process_count"] > 16 or limits["result_bytes"] < 256 or
            any(limits[name] > 1024 ** 2 for name in ("output_bytes", "result_bytes", "input_bytes", "candidate_bytes"))):
        _reject()


def _observation(observation, entry, request, runtime):
    _fields(observation, "version slot_id request_sha256 candidate_sha256 runtime_sha256 receipt expected_sha256 observed_sha256")
    if (type(observation["version"]) is not int or observation["version"] != 1 or
            any(observation[name] != entry[name] for name in ("slot_id", "request_sha256", "candidate_sha256")) or
            observation["runtime_sha256"] != _sha(_canonical(runtime)) or
            observation["expected_sha256"] != _sha(_canonical(request["expected"]))):
        _reject()
    receipt = observation["receipt"]
    _fields(receipt, "status stage limits enforcement_installed cleanup_complete output_bytes_seen output_bytes_retained result_bytes")
    allowed = {"input_invalid", "cancelled", "enforcement_unavailable", "abnormal_exit", "cleanup_failed", "timeout",
               "output_limit", "memory_limit", "process_limit", "invalid_worker_output", "result_limit", "success",
               "load_error", "analyze_error", "normalization_error", "system_exit", "digest_mismatch"}
    if (type(receipt["status"]) is not str or receipt["status"] not in allowed or
            receipt["stage"] not in {"startup", "load", "analyze", "normalize", "complete"} or
            _canonical(receipt["limits"]) != _canonical(runtime["limits"]) or
            any(type(receipt[key]) is not bool for key in ("enforcement_installed", "cleanup_complete")) or
            any(type(receipt[key]) is not int or receipt[key] < 0 for key in
                ("output_bytes_seen", "output_bytes_retained", "result_bytes")) or
            receipt["output_bytes_retained"] != 0 or receipt["result_bytes"] > runtime["limits"]["result_bytes"] + 1):
        _reject()
    if receipt["status"] == "success":
        _digest(observation["observed_sha256"])
        if (receipt["stage"] != "complete" or not receipt["enforcement_installed"] or not receipt["cleanup_complete"] or
                not 0 < receipt["result_bytes"] <= runtime["limits"]["result_bytes"] or
                receipt["output_bytes_seen"] >= runtime["limits"]["output_bytes"]):
            _reject()
    elif observation["observed_sha256"] is not None:
        _reject()
    if (receipt["status"] in {"input_invalid", "cancelled", "enforcement_unavailable", "cleanup_failed"} or
            not receipt["enforcement_installed"] or not receipt["cleanup_complete"]):
        return "SCORE_ABORT"
    if receipt["status"] == "success" and observation["expected_sha256"] == observation["observed_sha256"]:
        return "SUCCESS"
    return "FAILED"


def _ledger(folder, identity, candidate, observation_status, has_observation):
    if not folder.exists():
        if has_observation:
            _reject()
        return "NOT_RUN", {key + "_sha256": None for key in ("terminal", "outcome", "unknown")}
    order = ("marker", "prepared", "start", "candidate", "terminal", "outcome")
    extra = {"marker": "synthetic", "prepared": "", "start": "", "candidate": "sha256 size",
             "terminal": "status", "outcome": "status", "unknown": "status"}
    _inventory(folder, {key + ".json" for key in extra} | {"candidate.bin", "writer.lock"})
    if _raw(folder / "writer.lock", 1) != b"0":
        _reject()
    records, hashes = {}, {}
    for kind in order + ("unknown",):
        path = folder / (kind + ".json")
        if not path.exists():
            continue
        if path.stat().st_size > 4096:
            _reject()
        record = _json(path)
        _fields(record, "version kind ledger_id prev " + extra[kind])
        if (type(record["version"]) is not int or record["version"] != 1 or
                record["kind"] != kind or record["ledger_id"] != identity):
            _reject()
        if kind == "marker":
            if record["prev"] is not None or record["synthetic"] is not True:
                _reject()
        else:
            previous = "start" if kind == "unknown" else order[order.index(kind) - 1]
            if previous not in records or record["prev"] != hashes[previous]:
                _reject()
        if kind == "candidate":
            if (type(record["size"]) is not int or record["size"] != len(candidate) or
                    record["sha256"] != _sha(candidate) or _raw(folder / "candidate.bin", 1024 ** 2) != candidate):
                _reject()
        if kind in {"terminal", "outcome"}:
            if not has_observation or record["status"] != observation_status:
                _reject()
        if kind == "unknown" and (record["status"] != "UNKNOWN_BLOCKED" or "terminal" in records):
            _reject()
        records[kind], hashes[kind] = record, _sha(_canonical(record))
    if "marker" not in records or (has_observation and "candidate" not in records):
        _reject()
    if (folder / "candidate.bin").exists() and ("start" not in records or _raw(folder / "candidate.bin", 1024 ** 2) != candidate):
        _reject()
    if "terminal" in records:
        if "outcome" not in records:
            _reject()
        status = records["terminal"]["status"]
    elif "start" in records:
        if "unknown" not in records:
            _reject()
        status = "UNKNOWN_BLOCKED"
    else:
        if has_observation:
            _reject()
        status = "NOT_RUN"
    return status, {key + "_sha256": hashes.get(key) for key in ("terminal", "outcome", "unknown")}


def _verify(root, expected_runtime):
    _runtime(expected_runtime)
    manifest, report = _json(root / "manifest.json"), _json(root / "report.json")
    _fields(manifest, "version scope batch_id runtime slots")
    _fields(report, "version scope batch_id manifest_sha256 runtime_sha256 slots counts verdict")
    if (type(manifest["version"]) is not int or manifest["version"] != 1 or manifest["scope"] != "synthetic-local-qualification" or
            type(manifest["batch_id"]) is not str or re.fullmatch(r"synthetic-[A-Za-z0-9_-]{1,64}", manifest["batch_id"]) is None or
            _canonical(manifest["runtime"]) != _canonical(expected_runtime) or
            type(manifest["slots"]) is not list or not 1 <= len(manifest["slots"]) <= 32):
        _reject()
    entries, names, rows = manifest["slots"], set(), []
    stopped = False
    for entry in entries:
        _fields(entry, "slot_id request_sha256 candidate_sha256")
        name = entry["slot_id"]
        if (type(name) is not str or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", name) or
                re.fullmatch(r"(?i:con|prn|aux|nul|com[0-9]|lpt[0-9])", name) or
                name.lower() in names or name.lower() in {"writer", "manifest", "report"}):
            _reject()
        names.add(name.lower())
        for key in ("request_sha256", "candidate_sha256"):
            _digest(entry[key])
        folder = root / name
        _inventory(folder, {"candidate.py", "request.json", "execution.json"}, {"ledger"})
        request, candidate = _json(folder / "request.json"), _raw(folder / "candidate.py", expected_runtime["limits"]["candidate_bytes"])
        _fields(request, "version batch_id slot_id records expected")
        if (type(request["version"]) is not int or request["version"] != 1 or request["batch_id"] != manifest["batch_id"] or
                request["slot_id"] != name or _sha(_canonical(request)) != entry["request_sha256"] or
                not candidate or _sha(candidate) != entry["candidate_sha256"] or type(request["records"]) is not list or
                len(request["records"]) > 4096 or any(type(row) is not dict for row in request["records"]) or
                len(json.dumps(request["records"], allow_nan=False).encode("utf-8")) > expected_runtime["limits"]["input_bytes"] or
                len(_canonical(request["expected"])) > expected_runtime["limits"]["result_bytes"]):
            _reject()
        observation = _json(folder / "execution.json") if (folder / "execution.json").exists() else None
        observation_status = _observation(observation, entry, request, expected_runtime) if observation is not None else None
        identity = "synthetic-" + _sha(_canonical([manifest["batch_id"], name]))[:48]
        status, references = _ledger(folder / "ledger", identity, candidate, observation_status, observation is not None)
        if stopped and status != "NOT_RUN":
            _reject()
        stopped = stopped or status != "SUCCESS"
        rows.append(dict(entry, status=status, execution_sha256=_sha(_canonical(observation)) if observation is not None else None, **references))
    _inventory(root, {"writer.lock", "manifest.json", "report.json"}, {e["slot_id"] for e in entries})
    # The lock file is coordination metadata and may be held by the recovering caller.
    if (root / "writer.lock").stat().st_size != 1:
        _reject()
    labels = ("SUCCESS", "FAILED", "SCORE_ABORT", "UNKNOWN_BLOCKED", "NOT_RUN")
    counts = {label: len([row for row in rows if row["status"] == label]) for label in labels}
    verdict = "QUALIFIED"
    if counts["UNKNOWN_BLOCKED"]:
        verdict = "UNKNOWN_BLOCKED"
    elif counts["SUCCESS"] != len(entries):
        verdict = "ABORTED"
    expected = dict(version=1, scope="synthetic-local-qualification", batch_id=manifest["batch_id"],
                    manifest_sha256=_sha(_canonical(manifest)), runtime_sha256=_sha(_canonical(expected_runtime)),
                    slots=rows, counts=counts, verdict=verdict)
    if _canonical(report) != _canonical(expected):
        _reject()


def verify_report(root, expected_runtime):
    """Use a separately trusted runtime identity; return only fixed categories."""
    try:
        _verify(Path(root).absolute(), expected_runtime)
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        return {"valid": False, "category": "invalid_local_evidence"}
    return {"valid": True, "category": "verified_local_synthetic"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root")
    parser.add_argument("--expected-runtime", required=True)
    args = parser.parse_args(argv)
    try:
        # Trust-anchor files are user setup, not immutable evidence artifacts.
        trusted = json.loads(_raw(Path(args.expected_runtime).absolute()).decode("utf-8"))
        result = verify_report(args.root, trusted)
    except (OSError, ValueError, TypeError, RecursionError):
        result = {"valid": False, "category": "invalid_local_evidence"}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
