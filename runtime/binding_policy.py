"""Phase 3A binding-layer gates: R1 schema drift and R2 binding validity.

Standalone versioned checks over the hidden canonical frame, the mapping
document, the disclosed dtype-only schema and the semantic binding document.
They decide compatibility and validity and record observed drift; they never
rebind, never execute, and never modify the historical binder that produced the
frozen D-032 and D-077/D-082 results.

Rules are frozen in docs/phase-3a-robustness-spec.md sections 4 and 5.
"""

import hashlib
import json
import re
from typing import Any, Dict, List, Mapping, Optional
import warnings

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    warnings.filterwarnings(
        "ignore",
        message=r"Pandas requires version .* of 'numexpr'.*",
        category=UserWarning,
    )
    import pandas as pd

from runtime import scoped_enum


POLICY_VERSION = 1
SCHEMA_VERSION = 1
MAPPING_VERSION = 1
BINDING_VERSION = 1

ACCEPT = "accept"
REJECT = "reject"

MISSING_DECLARED_COLUMN = "missing_declared_column"
DTYPE_MISMATCH = "dtype_mismatch"
EXTRA_COLUMN = "extra_column"
DTYPE_WIDENING = "dtype_widening"

NAMESPACE_MISMATCH = "namespace_mismatch"
ENUM_SCOPE_MISMATCH = "enum_scope_mismatch"
BINDING_STALE = "binding_stale"
UNKNOWN_ENUM_TOKEN = "unknown_enum_token"
UNKNOWN_ENUM_VALUE = "unknown_enum_value"
ENUM_SCOPE_REQUIRED = "enum_scope_required"

# The single declared dtype exception: null introduction widens int64 to float64
# on the same symbol. Every other difference, including narrowing, is rejected.
WIDENING_PAIR = ("int64", "float64")

_SYMBOL_PATTERN = re.compile(r"^COL_[0-9]{4}$")
_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_BINDING_KEYS = {"binding_version", "project_namespace", "symbol_scope",
                 "schema_fingerprint", "fields", "enum_mappings"}
_ENUM_KEYS = {"scope", "forward", "reverse"}
_VALIDITY_PRECEDENCE = (NAMESPACE_MISMATCH, ENUM_SCOPE_MISMATCH, BINDING_STALE)


class BindingPolicyError(Exception):
    """Raised when a policy input is malformed or has an undeclared shape."""


def _validate_policy_version(policy_version: Any) -> None:
    if type(policy_version) is not int or policy_version != POLICY_VERSION:
        raise BindingPolicyError("policy_version must be integer {0}".format(POLICY_VERSION))


def _validate_disclosed_schema(disclosed_schema: Any) -> Dict[str, str]:
    if (not isinstance(disclosed_schema, Mapping)
            or set(disclosed_schema) != {"schema_version", "fields"}):
        raise BindingPolicyError("disclosed schema must have exactly schema_version and fields")
    version = disclosed_schema["schema_version"]
    if type(version) is not int or version != SCHEMA_VERSION:
        raise BindingPolicyError("schema_version must be integer {0}".format(SCHEMA_VERSION))
    fields = disclosed_schema["fields"]
    if not isinstance(fields, list) or not fields:
        raise BindingPolicyError("disclosed schema fields must be a nonempty list")

    dtypes: Dict[str, str] = {}
    for field in fields:
        if not isinstance(field, Mapping) or set(field) != {"name", "dtype"}:
            raise BindingPolicyError("each disclosed field must have exactly name and dtype")
        name = field["name"]
        dtype = field["dtype"]
        if not isinstance(name, str) or not _SYMBOL_PATTERN.match(name):
            raise BindingPolicyError("disclosed field names must be COL_0001-style symbols")
        if not isinstance(dtype, str) or not dtype:
            raise BindingPolicyError("disclosed field dtypes must be nonempty strings")
        if name in dtypes:
            raise BindingPolicyError("disclosed symbols must be unique")
        dtypes[name] = dtype
    return dtypes


def _validate_mapping_fields(fields: Any) -> Dict[str, str]:
    if not isinstance(fields, Mapping) or not fields:
        raise BindingPolicyError("mapping fields must be a nonempty object")

    normalized: Dict[str, str] = {}
    for canonical_name, symbol in fields.items():
        if not isinstance(canonical_name, str) or not canonical_name:
            raise BindingPolicyError("mapping fields require nonempty string input names")
        if not isinstance(symbol, str) or not _SYMBOL_PATTERN.match(symbol):
            raise BindingPolicyError("mapping fields require COL_0001-style targets")
        normalized[canonical_name] = symbol
    if len(set(normalized.values())) != len(normalized):
        raise BindingPolicyError("mapping targets must be unique")
    return normalized


def _validate_mapping_document(mapping: Any) -> Dict[str, str]:
    if not isinstance(mapping, Mapping) or set(mapping) != {"mapping_version", "fields"}:
        raise BindingPolicyError("mapping must have exactly mapping_version and fields")
    version = mapping["mapping_version"]
    if type(version) is not int or version != MAPPING_VERSION:
        raise BindingPolicyError("mapping_version must be integer {0}".format(MAPPING_VERSION))
    return _validate_mapping_fields(mapping["fields"])


def _validate_frame(canonical_df: Any) -> Dict[str, str]:
    if not isinstance(canonical_df, pd.DataFrame):
        raise BindingPolicyError("hidden input must be a DataFrame")
    columns = list(canonical_df.columns)
    if len(set(columns)) != len(columns):
        raise BindingPolicyError("hidden input columns must be unique")
    actual: Dict[str, str] = {}
    for column in columns:
        if not isinstance(column, str) or not column:
            raise BindingPolicyError("hidden input columns must be nonempty strings")
        actual[column] = str(canonical_df[column].dtype)
    return actual


def _finding(code: str,
             symbol: Optional[str] = None,
             column: Optional[str] = None,
             disclosed: Optional[str] = None,
             actual: Optional[str] = None) -> Dict[str, Optional[str]]:
    return {"code": code, "symbol": symbol, "column": column,
            "disclosed": disclosed, "actual": actual}


def _order(finding: Mapping[str, Optional[str]]) -> tuple:
    # Declared-schema findings group first by symbol; findings about columns
    # absent from the disclosure follow, ordered by column name.
    if finding["symbol"] is not None:
        return (0, finding["symbol"], finding["code"])
    return (1, finding["column"] or "", finding["code"])


def check_binding_compatibility(canonical_df: Any,
                                mapping: Any,
                                disclosed_schema: Any,
                                policy_version: Any = POLICY_VERSION) -> Dict[str, Any]:
    """Decide R1 schema-drift compatibility before any binding or execution."""
    _validate_policy_version(policy_version)
    disclosed_dtypes = _validate_disclosed_schema(disclosed_schema)
    symbols_by_column = _validate_mapping_document(mapping)
    actual_dtypes = _validate_frame(canonical_df)

    mapped_symbols = set(symbols_by_column.values())
    undeclared = sorted(mapped_symbols - set(disclosed_dtypes))
    if undeclared:
        raise BindingPolicyError(
            "mapping targets absent from the disclosed schema: {0}".format(undeclared))
    unmapped_disclosure = sorted(set(disclosed_dtypes) - mapped_symbols)
    if unmapped_disclosure:
        raise BindingPolicyError(
            "disclosed symbols absent from the mapping: {0}".format(unmapped_disclosure))

    columns_by_symbol = {symbol: column for column, symbol in symbols_by_column.items()}
    reasons: List[Dict[str, Optional[str]]] = []
    drift: List[Dict[str, Optional[str]]] = []

    for symbol in sorted(disclosed_dtypes):
        column = columns_by_symbol[symbol]
        disclosed_dtype = disclosed_dtypes[symbol]
        if column not in actual_dtypes:
            reasons.append(_finding(MISSING_DECLARED_COLUMN, symbol=symbol, column=column,
                                    disclosed=disclosed_dtype))
            continue
        actual_dtype = actual_dtypes[column]
        if actual_dtype == disclosed_dtype:
            continue
        if (disclosed_dtype, actual_dtype) == WIDENING_PAIR:
            drift.append(_finding(DTYPE_WIDENING, symbol=symbol, column=column,
                                  disclosed=disclosed_dtype, actual=actual_dtype))
            continue
        reasons.append(_finding(DTYPE_MISMATCH, symbol=symbol, column=column,
                                disclosed=disclosed_dtype, actual=actual_dtype))

    for column in sorted(actual_dtypes):
        if column not in symbols_by_column:
            # Declared limitation: this column carries an undisclosed canonical
            # label. Phase 3A measures it only for candidates that do not
            # reference it and does not close that disclosure path.
            drift.append(_finding(EXTRA_COLUMN, column=column, actual=actual_dtypes[column]))

    reasons.sort(key=_order)
    drift.sort(key=_order)
    return {
        "policy_version": POLICY_VERSION,
        "decision": REJECT if reasons else ACCEPT,
        "reasons": reasons,
        "observed_drift": drift,
    }


def canonical_json(value: Any) -> str:
    """The canonical encoding the Phase 3A qualification record declares."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def fingerprint_disclosed_schema(disclosed_schema: Any) -> str:
    """Lowercase SHA-256 of the canonical disclosed schema.

    Declared field order is preserved deliberately. A re-disclosure that reorders
    columns is a different disclosure document, so bindings issued against the
    earlier one are stale even though a name-selecting program is unaffected and
    row R1 still accepts the frame. Row R2-C2 measures that no other check can see
    this, so the fingerprint comparison is the only discriminator.
    """
    dtypes = _validate_disclosed_schema(disclosed_schema)
    return hashlib.sha256(canonical_json({
        "schema_version": SCHEMA_VERSION,
        "fields": [{"dtype": dtypes[name], "name": name} for name in dtypes],
    }).encode("utf-8")).hexdigest()


def _reason(code: str,
            expected: Optional[str] = None,
            presented: Optional[str] = None) -> Dict[str, Optional[str]]:
    return {"code": code, "expected": expected, "presented": presented}


def _validate_scoped_enum_mapping(enum_mapping: Any) -> None:
    if not isinstance(enum_mapping, Mapping):
        raise BindingPolicyError("enum mapping must be an object")
    if set(enum_mapping) != _ENUM_KEYS:
        raise BindingPolicyError("enum mapping must have exactly scope, forward and reverse")
    scope = enum_mapping["scope"]
    if not isinstance(scope, str) or not scope:
        raise BindingPolicyError("enum mapping scope must be a nonempty string")
    forward, reverse = enum_mapping["forward"], enum_mapping["reverse"]
    if (not isinstance(forward, Mapping) or not isinstance(reverse, Mapping) or not forward
            or not all(isinstance(key, str) and key and isinstance(token, str) and token
                       for key, token in forward.items())
            or len(set(forward.values())) != len(forward)
            or dict(reverse) != {token: key for key, token in forward.items()}):
        raise BindingPolicyError("enum mapping forward and reverse must be consistent")


def _validate_binding_document(binding: Any) -> Dict[str, Any]:
    if not isinstance(binding, Mapping) or set(binding) != _BINDING_KEYS:
        raise BindingPolicyError("binding must have exactly the six declared keys")
    version = binding["binding_version"]
    if type(version) is not int or version != BINDING_VERSION:
        raise BindingPolicyError("binding_version must be integer {0}".format(BINDING_VERSION))
    namespace = binding["project_namespace"]
    if not isinstance(namespace, str) or not namespace:
        raise BindingPolicyError("project_namespace must be a nonempty string")
    scope = binding["symbol_scope"]
    if not isinstance(scope, str) or not scope:
        raise BindingPolicyError("symbol_scope must be a nonempty string")
    fingerprint = binding["schema_fingerprint"]
    if not isinstance(fingerprint, str) or not _FINGERPRINT_PATTERN.match(fingerprint):
        raise BindingPolicyError("schema_fingerprint must be lowercase SHA-256")
    fields = _validate_mapping_fields(binding["fields"])

    enum_mappings = binding["enum_mappings"]
    if not isinstance(enum_mappings, Mapping) or not enum_mappings:
        raise BindingPolicyError("enum_mappings must be a nonempty object")
    targets = set(fields.values())
    validated: Dict[str, Any] = {}
    for symbol, enum_mapping in enum_mappings.items():
        if symbol not in targets:
            raise BindingPolicyError("enum mapping symbol is not a binding target")
        _validate_scoped_enum_mapping(enum_mapping)
        if enum_mapping["scope"] != scope:
            raise BindingPolicyError("enum mapping scope differs from the binding symbol scope")
        validated[symbol] = enum_mapping

    return {"project_namespace": namespace, "symbol_scope": scope,
            "schema_fingerprint": fingerprint, "fields": fields,
            "enum_mappings": validated}


def check_binding_validity(binding: Any,
                           disclosed_schema: Any,
                           expected_namespace: Any,
                           expected_scope: Any) -> Dict[str, Any]:
    """Decide R2 binding validity before any binding or execution."""
    if not isinstance(expected_namespace, str) or not expected_namespace:
        raise BindingPolicyError("expected_namespace must be a nonempty string")
    if not isinstance(expected_scope, str) or not expected_scope:
        raise BindingPolicyError("expected_scope must be a nonempty string")
    validated = _validate_binding_document(binding)
    current = fingerprint_disclosed_schema(disclosed_schema)

    detected: List[Dict[str, Optional[str]]] = []
    if validated["project_namespace"] != expected_namespace:
        detected.append(_reason(NAMESPACE_MISMATCH, expected_namespace,
                                validated["project_namespace"]))
    if validated["symbol_scope"] != expected_scope:
        detected.append(_reason(ENUM_SCOPE_MISMATCH, expected_scope,
                                validated["symbol_scope"]))
    if validated["schema_fingerprint"] != current:
        detected.append(_reason(BINDING_STALE, current, validated["schema_fingerprint"]))

    detected.sort(key=lambda reason: _VALIDITY_PRECEDENCE.index(reason["code"]))
    return {
        "binding_version": BINDING_VERSION,
        "decision": REJECT if detected else ACCEPT,
        "reasons": detected,
    }


def _enum_reject(code: str,
                 expected: Optional[str] = None,
                 presented: Optional[str] = None) -> Dict[str, Any]:
    return {"decision": REJECT, "reason": _reason(code, expected, presented), "value": None}


def _enum_operation(direction: str, key: Any, enum_mapping: Any,
                    expected_scope: Any) -> Dict[str, Any]:
    if not isinstance(expected_scope, str) or not expected_scope:
        raise BindingPolicyError("expected_scope must be a nonempty string")
    if not isinstance(enum_mapping, Mapping):
        raise BindingPolicyError("enum mapping must be an object")
    if "scope" not in enum_mapping:
        return _enum_reject(ENUM_SCOPE_REQUIRED, expected_scope)
    _validate_scoped_enum_mapping(enum_mapping)
    if enum_mapping["scope"] != expected_scope:
        return _enum_reject(ENUM_SCOPE_MISMATCH, expected_scope, enum_mapping["scope"])

    convert = scoped_enum.restore_value if direction == "restore" else scoped_enum.map_value
    unknown = UNKNOWN_ENUM_TOKEN if direction == "restore" else UNKNOWN_ENUM_VALUE
    try:
        value = convert(key, enum_mapping, expected_scope)
    except ValueError as error:
        message = str(error)
        if message in ("unknown enum token", "unknown enum value"):
            return _enum_reject(unknown, presented=key if isinstance(key, str) else None)
        raise BindingPolicyError("scoped enum codec rejected the mapping") from error
    return {"decision": ACCEPT, "reason": None, "value": value}


def restore_enum_value(token: Any, enum_mapping: Any, expected_scope: Any) -> Dict[str, Any]:
    """Restore an enum token through the wired scoped codec with fixed reason codes."""
    return _enum_operation("restore", token, enum_mapping, expected_scope)


def map_enum_value(value: Any, enum_mapping: Any, expected_scope: Any) -> Dict[str, Any]:
    """Map an enum value through the wired scoped codec with fixed reason codes."""
    return _enum_operation("map", value, enum_mapping, expected_scope)
