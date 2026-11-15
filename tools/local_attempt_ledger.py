"""Versioned synthetic-only, single-writer Windows receipt ledger. No execution."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid


class LedgerError(RuntimeError):
    """Invalid or conflicting local evidence; no automatic continuation."""


class Busy(LedgerError):
    """A live writer holds the ledger lock."""


_ORDER = ("marker", "prepared", "start", "candidate", "terminal", "outcome")
_FIELDS = {"marker": {"synthetic"}, "prepared": set(), "start": set(),
           "candidate": {"sha256", "size"}, "terminal": {"status"},
           "outcome": {"status"}, "unknown": {"status"}}
_STATUSES = {"SUCCESS", "FAILED", "SCORE_ABORT"}


def _boundary(label):
    """Internal test seam at persistence boundaries; not an execution callback."""


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _safe(path):
    """Reject reparse points on the complete existing path, including ancestors."""
    for part in (path,) + tuple(path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
            raise LedgerError("reparse path forbidden")


class Ledger:
    """Hold one nonblocking process-lifetime writer lock via a context manager."""

    @classmethod
    def create(cls, root, ledger_id):
        if not isinstance(ledger_id, str) or not re.fullmatch(
                r"synthetic-[A-Za-z0-9_-]{1,64}", ledger_id):
            raise LedgerError("synthetic identifier required")
        ledger = cls(root)
        _safe(ledger.root)
        try:
            ledger.root.mkdir()
        except FileExistsError:
            raise LedgerError("root already exists") from None
        _boundary("root:created")
        try:
            ledger._acquire(create=True)
            ledger.ledger_id = ledger_id
            ledger._publish("marker.json", _bytes(ledger._record(
                "marker", None, synthetic=True)))
            return ledger
        except BaseException:
            ledger.close()
            raise

    @classmethod
    def open(cls, root):
        ledger = cls(root)
        try:
            ledger._acquire(create=False)
            ledger._validate()
            ledger.recover()
            return ledger
        except BaseException:
            ledger.close()
            raise

    def __init__(self, root):
        self.root = Path(os.path.abspath(str(root)))
        self._lock = None
        self.ledger_id = None

    def _acquire(self, create):
        if os.name != "nt":
            raise LedgerError("only Windows locking qualified")
        import msvcrt
        _safe(self.root)
        path = self.root / "writer.lock"
        _safe(path)
        try:
            stream = open(str(path), "x+b" if create else "r+b", buffering=0)
        except (FileNotFoundError, FileExistsError):
            raise LedgerError("incomplete initialization") from None
        try:
            if create:
                stream.write(b"0")
                os.fsync(stream.fileno())
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise Busy("writer busy") from None
            self._lock = stream
        except BaseException:
            stream.close()
            raise

    def close(self):
        if self._lock is not None:
            # Closing releases the byte-range lock, also on process death.
            self._lock.close()
            self._lock = None

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()

    def _guard(self):
        if self._lock is None or self._lock.closed:
            raise LedgerError("closed writer")
        _safe(self.root)

    def _record(self, kind, prev, **fields):
        return dict(version=1, kind=kind, ledger_id=self.ledger_id,
                    prev=prev, **fields)

    def _publish(self, filename, data):
        self._guard()
        final = self.root / filename
        _safe(final)
        if final.exists():
            if final.read_bytes() != data:
                raise LedgerError("conflicting immutable publication")
            return
        temporary = self.root / (".tmp-" + uuid.uuid4().hex)
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                _boundary(filename + ":temp_written")
                stream.flush()
                os.fsync(stream.fileno())
                _boundary(filename + ":flushed")
            try:
                os.link(str(temporary), str(final))
            except FileExistsError:
                raise LedgerError("publication collision") from None
            _boundary(filename + ":published")
        finally:
            if temporary.exists():
                temporary.unlink()

    def _validate(self):
        self._guard()
        allowed = {kind + ".json" for kind in _FIELDS} | {"writer.lock", "candidate.bin"}
        for path in self.root.iterdir():
            _safe(path)
            if not path.is_file() or (path.name not in allowed and not re.fullmatch(
                    r"\.tmp-[0-9a-f]{32}", path.name)):
                raise LedgerError("unexpected ledger entry")
        self._lock.seek(0)
        if self._lock.read() != b"0":
            raise LedgerError("invalid lock file")
        records = {}
        for kind in _ORDER + ("unknown",):
            path = self.root / (kind + ".json")
            if not path.exists():
                continue
            raw = path.read_bytes()
            try:
                record = json.loads(raw.decode("utf-8"))
                if (not isinstance(record, dict) or
                        set(record) != {"version", "kind", "ledger_id", "prev"} | _FIELDS[kind] or
                        type(record["version"]) is not int or record["version"] != 1 or
                        record["kind"] != kind or _bytes(record) != raw):
                    raise ValueError()
            except (ValueError, TypeError, UnicodeError):
                raise LedgerError("invalid receipt schema") from None
            if kind == "marker":
                identity = record["ledger_id"]
                if (not isinstance(identity, str) or not re.fullmatch(
                        r"synthetic-[A-Za-z0-9_-]{1,64}", identity) or
                        record["synthetic"] is not True or record["prev"] is not None):
                    raise LedgerError("invalid creation marker")
                if self.ledger_id is not None and self.ledger_id != identity:
                    raise LedgerError("identity changed")
                self.ledger_id = identity
            else:
                prior = "start" if kind == "unknown" else _ORDER[_ORDER.index(kind) - 1]
                if (prior not in records or record["ledger_id"] != self.ledger_id or
                        record["prev"] != _hash(_bytes(records[prior]))):
                    raise LedgerError("broken predecessor chain")
            if kind == "candidate":
                candidate = self.root / "candidate.bin"
                if (not candidate.is_file() or type(record["size"]) is not int or
                        record["size"] < 0 or record["size"] != candidate.stat().st_size or
                        record["sha256"] != _hash(candidate.read_bytes())):
                    raise LedgerError("candidate bytes mismatch")
            if kind in ("terminal", "outcome"):
                if not isinstance(record["status"], str) or record["status"] not in _STATUSES:
                    raise LedgerError("invalid status")
                if kind == "outcome" and record["status"] != records["terminal"]["status"]:
                    raise LedgerError("outcome mismatch")
            if kind == "unknown" and (record["status"] != "UNKNOWN_BLOCKED" or "terminal" in records):
                raise LedgerError("invalid unknown state")
            records[kind] = record
        if "marker" not in records:
            raise LedgerError("incomplete initialization")
        if (self.root / "candidate.bin").exists() and "start" not in records:
            raise LedgerError("orphan candidate before start")
        return records

    def _advance(self, kind, **fields):
        records = self._validate()
        if "unknown" in records:
            raise LedgerError("UNKNOWN_BLOCKED is permanent")
        prior = _ORDER[_ORDER.index(kind) - 1]
        if prior not in records:
            raise LedgerError("predecessor required")
        self._publish(kind + ".json", _bytes(self._record(
            kind, _hash(_bytes(records[prior])), **fields)))

    def prepare(self):
        self._advance("prepared")

    def start(self):
        self._advance("start")

    def candidate(self, data):
        records = self._validate()
        if type(data) is not bytes or "start" not in records or "unknown" in records:
            raise LedgerError("candidate requires bytes and active start")
        self._publish("candidate.bin", data)
        self._advance("candidate", sha256=_hash(data), size=len(data))

    def terminal(self, status):
        if not isinstance(status, str) or status not in _STATUSES:
            raise LedgerError("invalid terminal status")
        self._advance("terminal", status=status)

    def recover(self):
        records = self._validate()
        if "unknown" in records:
            return "UNKNOWN_BLOCKED"
        if "terminal" in records:
            status = records["terminal"]["status"]
            self._advance("outcome", status=status)
            return status
        if "start" in records:
            self._publish("unknown.json", _bytes(self._record(
                "unknown", _hash(_bytes(records["start"])), status="UNKNOWN_BLOCKED")))
            return "UNKNOWN_BLOCKED"
        return "PREPARED" if "prepared" in records else "INITIALIZED"
