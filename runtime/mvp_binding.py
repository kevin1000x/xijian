"""Future-only binding for finite, nonempty, trusted synthetic CSV-like frames.

No historical caller is redirected here. Contract digests are integrity checks,
not signatures; namespace/scope/current disclosure must come from a trusted caller.
"""
import copy
import hashlib
import json
import math
import re
from typing import Any, Dict, Optional, Tuple, List

from runtime import binding_policy as policy
from runtime.shadow_generator import build_mapping

pd = policy.pd
KIND = 'offline-scoped-binding'
VERSION = 1
KEYS = {'kind', 'version', 'revision', 'predecessor', 'namespace', 'scope',
        'mapping', 'schema', 'schema_fingerprint', 'enums', 'digest'}
DTYPES = {'int64', 'float64', 'bool', 'object'}


class BindingRejected(ValueError):
    """Fixed reason code only; never include input labels or values."""


def _reject(code: str) -> None:
    raise BindingRejected(code)


def _digest(value: Any) -> str:
    return hashlib.sha256(policy.canonical_json(value).encode('utf-8')).hexdigest()


def _hex(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _identity(namespace: Any, scope: Any) -> None:
    if not all(type(x) is str and x.strip() for x in (namespace, scope)):
        _reject('invalid_identity')


def _domain(data: Any) -> None:
    if (not isinstance(data, pd.DataFrame) or not 1 <= len(data) <= 4096
            or not 1 <= len(data.columns) <= 9999
            or not all(type(c) is str and c.strip() for c in data.columns)
            or not data.columns.is_unique):
        _reject('unsupported_input')
    for name in data.columns:
        series = data[name]
        dtype = str(series.dtype)
        if dtype not in DTYPES or series.isna().any():
            _reject('unsupported_input')
        if dtype == 'object' and not all(type(x) is str and x for x in series):
            _reject('unsupported_input')
        if dtype == 'float64' and not all(math.isfinite(x) for x in series):
            _reject('unsupported_input')


def _validated(contract: Any) -> Dict[str, Any]:
    try:
        if type(contract) is not dict or set(contract) != KEYS:
            _reject('invalid_binding')
        c = copy.deepcopy(contract)
        if (c['kind'] != KIND or type(c['version']) is not int or c['version'] != VERSION
                or type(c['revision']) is not int or c['revision'] < 1
                or (c['revision'] == 1 and c['predecessor'] is not None)
                or (c['revision'] > 1 and not _hex(c['predecessor']))):
            _reject('invalid_binding')
        _identity(c['namespace'], c['scope'])
        if not _hex(c['digest']) or _digest({k: v for k, v in c.items() if k != 'digest'}) != c['digest']:
            _reject('invalid_binding')
        if policy.fingerprint_disclosed_schema(c['schema']) != c['schema_fingerprint']:
            _reject('invalid_binding')
        mapping = c['mapping']
        if (type(mapping) is not dict or set(mapping) != {'mapping_version', 'fields'}
                or type(mapping['mapping_version']) is not int or mapping['mapping_version'] != 1
                or type(mapping['fields']) is not dict or not mapping['fields']):
            _reject('invalid_binding')
        fields = mapping['fields']
        if not all(type(name) is str and name.strip() for name in fields):
            _reject('invalid_binding')
        symbols = [f['name'] for f in c['schema']['fields']]
        if (symbols != ['COL_{0:04d}'.format(i + 1) for i in range(len(symbols))]
                or len(fields) != len(symbols) or set(fields.values()) != set(symbols)
                or len({name.strip() for name in fields}) != len(fields)
                or any(f['dtype'] not in DTYPES for f in c['schema']['fields'])):
            _reject('invalid_binding')
        enums = c['enums']
        enum_symbols = {f['name'] for f in c['schema']['fields'] if f['dtype'] == 'object'}
        if type(enums) is not dict or set(enums) != enum_symbols:
            _reject('invalid_binding')
        for enum in enums.values():
            if type(enum) is not dict or enum.get('scope') != c['scope']:
                _reject('invalid_binding')
            # The public wrapper validates complete forward/reverse shape and scope.
            first = next(iter(enum['forward']))
            if policy.map_enum_value(first, enum, c['scope'])['decision'] != policy.ACCEPT:
                _reject('invalid_binding')
            if set(enum['forward'].values()) != {'ENUM_{0:04d}'.format(i + 1)
                                                for i in range(len(enum['forward']))}:
                _reject('invalid_binding')
        return c
    except (ValueError, TypeError, KeyError, StopIteration, RecursionError,
            policy.BindingPolicyError):
        raise BindingRejected('invalid_binding') from None


def issue_binding(data: Any, namespace: str, scope: str,
                  previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Automatically issue/reissue from a fresh synthetic frame; no files or code run."""
    _identity(namespace, scope)
    _domain(data)
    old = _validated(previous) if previous is not None else None
    if old is not None:
        if old['namespace'] != namespace:
            _reject('namespace_mismatch')
        if old['scope'] != scope:
            _reject('scope_mismatch')
        previous_fields = old['mapping']['fields']
        if not set(previous_fields).issubset(data.columns):
            _reject('column_retirement_unsupported')
        columns = sorted(previous_fields, key=previous_fields.get)
        columns.extend(name for name in data.columns if name not in previous_fields)
    else:
        columns = list(data.columns)
    try:
        mapping = build_mapping(columns)
    except ValueError:
        raise BindingRejected('unsupported_input') from None
    schema = {'schema_version': 1, 'fields': [
        {'name': mapping['fields'][name], 'dtype': str(data[name].dtype)} for name in columns]}
    enums: Dict[str, Any] = {}
    for name in columns:
        if str(data[name].dtype) != 'object':
            continue
        symbol = mapping['fields'][name]
        forward = dict(old['enums'].get(symbol, {}).get('forward', {})) if old else {}
        for value in sorted(set(data[name]) - set(forward)):
            forward[value] = 'ENUM_{0:04d}'.format(len(forward) + 1)
        enums[symbol] = {'scope': scope, 'forward': forward,
                         'reverse': {v: k for k, v in forward.items()}}
    c = {'kind': KIND, 'version': VERSION, 'revision': old['revision'] + 1 if old else 1,
         'predecessor': old['digest'] if old else None, 'namespace': namespace, 'scope': scope,
         'mapping': mapping, 'schema': schema,
         'schema_fingerprint': policy.fingerprint_disclosed_schema(schema), 'enums': enums}
    c['digest'] = _digest(c)
    return _validated(c)


def public_schema(contract: Dict[str, Any]) -> Dict[str, Any]:
    """Return detached dtype-only disclosure; never return internal binding metadata."""
    return _validated(contract)['schema']


def bind_records(data: Any, contract: Dict[str, Any], current_schema: Dict[str, Any],
                 expected_namespace: str, expected_scope: str, *,
                 expected_binding_digest: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Validate and project before execution; only plain JSON records cross to worker."""
    c = _validated(contract)
    _identity(expected_namespace, expected_scope)
    if c['namespace'] != expected_namespace:
        _reject('namespace_mismatch')
    if c['scope'] != expected_scope:
        _reject('scope_mismatch')
    if not _hex(expected_binding_digest):
        _reject('invalid_identity')
    if c['digest'] != expected_binding_digest:
        _reject('binding_revision_stale')
    try:
        current_fingerprint = policy.fingerprint_disclosed_schema(current_schema)
    except policy.BindingPolicyError:
        raise BindingRejected('invalid_disclosure') from None
    if current_fingerprint != c['schema_fingerprint']:
        _reject('binding_stale')
    try:
        gate = policy.check_binding_compatibility(data, c['mapping'], current_schema)
    except (policy.BindingPolicyError, TypeError, ValueError):
        raise BindingRejected('unsupported_input') from None
    if gate['decision'] != policy.ACCEPT:
        _reject(gate['reasons'][0]['code'])
    if any(x['code'] == policy.DTYPE_WIDENING for x in gate['observed_drift']):
        _reject('dtype_reissue_required')
    fields = c['mapping']['fields']
    columns = sorted(fields, key=fields.get)
    projected = data.loc[:, columns].copy(deep=True).reset_index(drop=True)
    projected.attrs = {}
    _domain(projected)
    projected.columns = [fields[name] for name in columns]
    for symbol, enum in c['enums'].items():
        values = []
        for value in projected[symbol]:
            outcome = policy.map_enum_value(value, enum, expected_scope)
            if outcome['decision'] != policy.ACCEPT:
                _reject(outcome['reason']['code'])
            values.append(outcome['value'])
        projected[symbol] = values
    try:
        records = json.loads(policy.canonical_json(projected.to_dict(orient='records')))
        reconstructed = pd.DataFrame(records)
        if (list(reconstructed.columns) != list(projected.columns)
                or list(map(str, reconstructed.dtypes)) != list(map(str, projected.dtypes))
                or not reconstructed.equals(projected)):
            _reject('transport_not_lossless')
    except (ValueError, TypeError, OverflowError):
        raise BindingRejected('transport_not_lossless') from None
    return records, {'version': VERSION, 'binding_digest': c['digest'],
                     'revision': c['revision'], 'schema_fingerprint': current_fingerprint,
                     'records_sha256': _digest(records), 'projected_columns': len(columns),
                     'dropped_columns': len(data.columns) - len(columns)}
