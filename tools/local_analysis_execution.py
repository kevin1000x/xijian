"""Shared publication and ledger checks for local supervised analysis records."""
from tools import local_synthetic_workflow as storage
from tools.local_attempt_ledger import Ledger


def execute_once(root, manifest, records, candidate, observe, *, limits, cancel=None,
                 executor, boundary):
    """Caller owns the root lock and has frozen candidate/request/manifest."""
    with Ledger.create(root / 'ledger', 'synthetic-' + manifest['run_id']) as ledger:
        ledger.prepare()
        ledger.start()
        boundary('start:published')
        ledger.candidate(candidate)
        try:
            result = executor(root / 'candidate.py', manifest['candidate_sha256'], records,
                              limits=limits, cancel=cancel)
        except Exception:
            result = None
        if result is not None:
            payload = result.payload if result.receipt['status'] == 'success' else None
            record = dict(version=1, manifest_sha256=storage._hash(storage._bytes(manifest)),
                          receipt=result.receipt, payload=payload,
                          payload_sha256=storage._hash(storage._bytes(payload)))
            status, _, _ = observe(record)
            storage._publish(root / 'execution.json', storage._bytes(record))
            boundary('execution:published')
            storage._publish(root / 'execution-ref.json', storage._bytes(dict(
                manifest_sha256=storage._hash(storage._bytes(manifest)),
                execution_sha256=storage._hash(storage._bytes(record)))))
            boundary('reference:published')
            ledger.terminal(status)
            boundary('terminal:published')
            ledger.recover()


def recover_ledger(root, manifest, candidate, record, observed, *, error=ValueError,
                   require_reference=True, allow_reference=True, read_only=False):
    """Validate identity before optional projection; read_only never publishes."""
    reference = root / 'execution-ref.json'
    if reference.exists():
        committed = storage._json(reference)
        storage._keys(committed, 'manifest_sha256 execution_sha256')
        if (not allow_reference or record is None or
                committed['manifest_sha256'] != storage._hash(storage._bytes(manifest)) or
                committed['execution_sha256'] != storage._hash(storage._bytes(record))):
            raise error('execution_reference_mismatch')
    status = 'NOT_RUN'
    if (root / 'ledger').exists():
        for path in (root / 'ledger').iterdir():
            storage._read(path, manifest['runtime']['limits']['candidate_bytes']
                          if path.name == 'candidate.bin' else 4096)
        ledger = Ledger(root / 'ledger')
        ledger._acquire(create=False)
        with ledger:
            receipts = ledger._validate()
            if (ledger.ledger_id != 'synthetic-' + manifest['run_id'] or
                    ((root / 'ledger/candidate.bin').exists() and
                     storage._read(root / 'ledger/candidate.bin') != candidate) or
                    (record is not None and 'candidate' not in receipts) or
                    ('terminal' in receipts and (receipts['terminal']['status'] != observed or
                     (require_reference and not reference.exists())))):
                raise error('ledger_identity_changed')
            if not read_only:
                status = ledger.recover()
            elif 'terminal' in receipts:
                status = receipts['terminal']['status']
            elif 'start' in receipts or 'unknown' in receipts:
                status = 'UNKNOWN_BLOCKED'
            else:
                status = 'PREPARED' if 'prepared' in receipts else 'INITIALIZED'
    elif record is not None:
        raise error('missing_execution_ledger')
    return status
