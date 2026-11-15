"""Execute one immutable POC-0 submission against fixed hidden fixtures.

The AST check in this module is validation-only.  It is intentionally small and
is not a production sandbox or a security boundary for enterprise data.
"""

import argparse
import ast
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime
import hashlib
import inspect
from io import StringIO
import json
import math
import numbers
from pathlib import Path
import re
import time
import types
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
import warnings

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    warnings.filterwarnings(
        "ignore",
        message=r"Pandas requires version .* of 'numexpr'.*",
        category=UserWarning,
    )
    import pandas as pd


FIXTURE_NAMES = ("normal", "counterfactual", "edge")
HIDDEN_FILENAMES = {
    "normal": "hidden_normal.csv",
    "counterfactual": "hidden_counterfactual.csv",
    "edge": "hidden_edge.csv",
}
MAPPING_VERSION = 1
_CASE_PATTERN = re.compile(r"^E-00[1-6]$")
_TRIAL_PATTERN = re.compile(r"^(E-00[1-6])-([AB])-(0[12])$")
_SHADOW_PATTERN = re.compile(r"^COL_[0-9]{4}$")


class RunnerError(Exception):
    """Base class for fail-closed runner contract errors."""


class SourceValidationError(RunnerError):
    """Raised when validation-only source inspection finds prohibited code."""

    def __init__(self, findings: Sequence[Mapping[str, Any]]) -> None:
        self.findings = [dict(finding) for finding in findings]
        super().__init__("analysis source failed validation")


class ContractError(RunnerError):
    """Raised when the fixed analyze(df) entrypoint cannot be loaded."""


class MappingValidationError(RunnerError):
    """Raised when the canonical-to-Shadow mapping cannot bind safely."""


class NormalizationError(RunnerError):
    """Raised for unsupported or non-JSON-compatible result values."""


class TrialValidationError(RunnerError):
    """Raised when D-08 Trial identifiers and paths disagree."""


def sha256_file(path: Path) -> str:
    """Return lowercase SHA-256 for the exact bytes at *path*."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _finding(category: str, node: ast.AST, identifier: str) -> Dict[str, Any]:
    return {
        "category": category,
        "line": int(getattr(node, "lineno", 0) or 0),
        "column": int(getattr(node, "col_offset", 0) or 0),
        "node": type(node).__name__,
        "identifier": identifier,
    }


def _import_category(module: str, member: Optional[str] = None) -> Optional[str]:
    root = module.split(".", 1)[0]
    identifier = "{0}.{1}".format(module, member) if member else module
    if root in {"pathlib", "io", "glob", "shutil", "tempfile"}:
        return "file"
    if root == "os":
        if member in {"system", "popen", "spawnl", "spawnlp", "spawnv", "spawnvp"}:
            return "shell"
        if member in {"getenv", "putenv", "unsetenv", "environ"}:
            return "environment"
        if member in {"getpid", "getppid", "getuid", "geteuid", "getlogin"}:
            return "process"
        return "environment"
    if root in {"socket", "urllib", "http", "requests", "ftplib"}:
        return "network"
    if root == "subprocess":
        return "subprocess"
    if root == "importlib":
        return "dynamic_import"
    if root in {"pip", "ensurepip"} or "pip" in identifier.split("."):
        return "install"
    return None


def _qualified_name(node: ast.AST, aliases: Mapping[str, str]) -> Optional[str]:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _qualified_name(node.value, aliases)
        if base:
            return "{0}.{1}".format(base, node.attr)
    return None


def _literal_tokens(node: ast.AST) -> List[str]:
    tokens = []
    for child in ast.walk(node):
        if isinstance(child, ast.Str):
            tokens.append(child.s.lower())
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            tokens.append(child.value.lower())
    return tokens


PANDAS_FILE_READERS = {
    "pandas.read_clipboard",
    "pandas.read_csv",
    "pandas.read_excel",
    "pandas.read_feather",
    "pandas.read_fwf",
    "pandas.read_gbq",
    "pandas.read_hdf",
    "pandas.read_html",
    "pandas.read_json",
    "pandas.read_orc",
    "pandas.read_parquet",
    "pandas.read_pickle",
    "pandas.read_sas",
    "pandas.read_spss",
    "pandas.read_sql",
    "pandas.read_sql_query",
    "pandas.read_sql_table",
    "pandas.read_stata",
    "pandas.read_table",
    "pandas.read_xml",
}
PANDAS_FILE_OBJECTS = {
    "pandas.ExcelFile",
    "pandas.ExcelWriter",
    "pandas.HDFStore",
}
PANDAS_PATH_WRITERS = {
    "to_clipboard",
    "to_csv",
    "to_excel",
    "to_feather",
    "to_gbq",
    "to_hdf",
    "to_html",
    "to_json",
    "to_latex",
    "to_markdown",
    "to_orc",
    "to_parquet",
    "to_pickle",
    "to_sql",
    "to_stata",
    "to_xml",
}
PANDAS_DESTINATION_KEYWORDS = {
    "buf",
    "con",
    "excel_writer",
    "fname",
    "path",
    "path_or_buf",
}


def _pandas_writer_has_destination(identifier: str, call: ast.Call) -> bool:
    writer = identifier.rsplit(".", 1)[-1]
    if writer not in PANDAS_PATH_WRITERS:
        return False
    if writer == "to_clipboard":
        return True
    if writer == "to_sql":
        return len(call.args) >= 2 or any(
            keyword.arg == "con" for keyword in call.keywords
        )
    return bool(call.args) or any(
        keyword.arg in PANDAS_DESTINATION_KEYWORDS
        for keyword in call.keywords
    )


def _call_category(identifier: str, call: ast.Call) -> Optional[str]:
    if identifier in PANDAS_FILE_READERS or identifier in PANDAS_FILE_OBJECTS:
        return "file"
    if _pandas_writer_has_destination(identifier, call):
        return "file"
    if identifier in {"open", "io.open"}:
        return "file"
    if identifier.endswith(
        (".open", ".read_text", ".write_text", ".read_bytes", ".write_bytes")
    ):
        return "file"
    if identifier in {"eval", "exec", "compile"}:
        return "dynamic_code"
    if identifier == "__import__" or identifier.startswith("importlib."):
        return "dynamic_import"
    if identifier.startswith(("socket.", "urllib.", "http.", "requests.", "ftplib.")):
        return "network"
    if identifier.startswith("subprocess."):
        tokens = _literal_tokens(call)
        if "pip" in tokens and "install" in tokens:
            return "install"
        return "subprocess"
    if identifier in {
        "os.system",
        "os.popen",
        "os.spawnl",
        "os.spawnlp",
        "os.spawnv",
        "os.spawnvp",
    }:
        return "shell"
    if identifier in {"os.getenv", "os.putenv", "os.unsetenv"}:
        return "environment"
    if identifier in {"os.getpid", "os.getppid", "os.getuid", "os.geteuid", "os.getlogin"}:
        return "process"
    if identifier.startswith(("pip.", "ensurepip.")):
        return "install"
    return None


def validate_analysis_source(path: Path) -> Dict[str, Any]:
    """Parse source without transformation and reject the frozen D-14 set."""
    source_path = Path(path)
    try:
        source = source_path.read_bytes()
        tree = ast.parse(source, filename=str(source_path))
    except SyntaxError as error:
        finding = {
            "category": "syntax",
            "line": int(error.lineno or 0),
            "column": max(int(error.offset or 1) - 1, 0),
            "node": "SyntaxError",
            "identifier": "SyntaxError",
        }
        raise SourceValidationError([finding])

    aliases: Dict[str, str] = {}
    findings: List[Dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local_name = alias.asname or alias.name.split(".", 1)[0]
                aliases[local_name] = alias.name
                category = _import_category(alias.name)
                if category:
                    findings.append(_finding(category, node, alias.name))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                local_name = alias.asname or alias.name
                identifier = "{0}.{1}".format(module, alias.name) if module else alias.name
                aliases[local_name] = identifier
                category = _import_category(module, alias.name)
                if category:
                    findings.append(_finding(category, node, identifier))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            identifier = _qualified_name(node.func, aliases)
            if identifier:
                category = _call_category(identifier, node)
                if category:
                    findings.append(_finding(category, node, identifier))
        elif isinstance(node, ast.Attribute):
            identifier = _qualified_name(node, aliases)
            if identifier in {"os.environ", "sys.argv", "sys.modules", "sys.path", "sys.executable"}:
                category = "environment" if identifier == "os.environ" else "process"
                findings.append(_finding(category, node, identifier))

    deduplicated = []
    seen = set()
    for finding in findings:
        marker = (
            finding["category"],
            finding["line"],
            finding["column"],
            finding["node"],
            finding["identifier"],
        )
        if marker not in seen:
            seen.add(marker)
            deduplicated.append(finding)
    deduplicated.sort(
        key=lambda item: (
            item["line"], item["column"], item["category"], item["identifier"]
        )
    )
    if deduplicated:
        raise SourceValidationError(deduplicated)
    return {"status": "ok", "findings": []}


def _validate_mapping_document(mapping: Mapping[str, Any]) -> Dict[str, str]:
    if not isinstance(mapping, Mapping):
        raise MappingValidationError("mapping must be an object")
    if type(mapping.get("mapping_version")) is not int or mapping.get("mapping_version") != MAPPING_VERSION:
        raise MappingValidationError("mapping_version must be integer 1")
    fields = mapping.get("fields")
    if not isinstance(fields, Mapping) or not fields:
        raise MappingValidationError("mapping fields must be a nonempty object")

    normalized: Dict[str, str] = {}
    for canonical_name, shadow_symbol in fields.items():
        if not isinstance(canonical_name, str) or not canonical_name:
            raise MappingValidationError("mapping fields require nonempty string input names")
        if not isinstance(shadow_symbol, str) or not _SHADOW_PATTERN.match(shadow_symbol):
            raise MappingValidationError("mapping fields require COL_0001-style targets")
        normalized[canonical_name] = shadow_symbol
    if len(set(normalized.values())) != len(normalized):
        raise MappingValidationError("mapping targets must be unique")
    return normalized


def bind_hidden_frame(
    canonical_df: pd.DataFrame,
    mapping: Mapping[str, Any],
    arm: str,
) -> pd.DataFrame:
    """Return a fresh canonical A copy or in-memory Shadow-renamed B copy."""
    if arm not in {"A", "B"}:
        raise MappingValidationError("arm must be A or B")
    if not isinstance(canonical_df, pd.DataFrame):
        raise MappingValidationError("hidden input must be a DataFrame")
    raw_fields = mapping.get("fields") if isinstance(mapping, Mapping) else None
    if isinstance(raw_fields, Mapping):
        raw_collisions = sorted(
            set(raw_fields.values()) & (set(canonical_df.columns) - set(raw_fields))
        )
        if raw_collisions:
            raise MappingValidationError(
                "Shadow target collision: {0}".format(raw_collisions)
            )
    fields = _validate_mapping_document(mapping)
    columns = list(canonical_df.columns)
    if len(set(columns)) != len(columns):
        raise MappingValidationError("hidden input columns must be unique")
    missing = [canonical_name for canonical_name in fields if canonical_name not in columns]
    if missing:
        raise MappingValidationError("mapped input columns missing: {0}".format(missing))
    unmapped = set(columns) - set(fields)
    collisions = sorted(set(fields.values()) & unmapped)
    if collisions:
        raise MappingValidationError("Shadow target collision: {0}".format(collisions))

    bound = canonical_df.copy(deep=True)
    if arm == "B":
        bound.rename(columns=fields, inplace=True)
    return bound


def _restore_name(value: Any, reverse_mapping: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return reverse_mapping.get(value, value)
    return value


def _restore_index_names(index: pd.Index, reverse_mapping: Mapping[str, str]) -> pd.Index:
    restored = index.copy(deep=True)
    restored.names = [_restore_name(name, reverse_mapping) for name in index.names]
    return restored


def restore_input_labels(result: Any, reverse_mapping: Mapping[str, str]) -> Any:
    """Restore exact input-derived labels without changing data or enum values."""
    if isinstance(result, pd.DataFrame):
        restored = result.copy(deep=True)
        restored.columns = [
            _restore_name(column, reverse_mapping) for column in result.columns
        ]
        restored.index = _restore_index_names(result.index, reverse_mapping)
        return restored
    if isinstance(result, pd.Series):
        restored = result.copy(deep=True)
        restored.name = _restore_name(result.name, reverse_mapping)
        restored.index = _restore_index_names(result.index, reverse_mapping)
        return restored
    return result


def _json_scalar(value: Any) -> Any:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        number = float(value)
        if math.isnan(number):
            return None
        if not math.isfinite(number):
            raise NormalizationError("non-finite numeric result is unsupported")
        return number
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        missing = False
    if isinstance(missing, bool) and missing:
        return None
    raise NormalizationError("unsupported result value type: {0}".format(type(value).__name__))


def _json_label(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_json_scalar(part) for part in value]
    return _json_scalar(value)


def _index_names(index: pd.Index) -> List[Any]:
    return [_json_label(name) for name in index.names]


def _validate_comparison_policy(policy: Mapping[str, Any]) -> Tuple[List[str], bool]:
    if not isinstance(policy, Mapping):
        raise NormalizationError("normalization policy must be an object")
    keys = policy.get("keys")
    order_sensitive = policy.get("order_sensitive")
    if not isinstance(keys, list) or not keys or not all(isinstance(key, str) for key in keys):
        raise NormalizationError("normalization keys must be a nonempty string list")
    if type(order_sensitive) is not bool:
        raise NormalizationError("order_sensitive must be boolean")
    return list(keys), order_sensitive


def normalize_result(result: Any, comparison_policy: Mapping[str, Any]) -> Dict[str, Any]:
    """Normalize an actual scalar, Series, or DataFrame into D-10 envelopes."""
    keys, order_sensitive = _validate_comparison_policy(comparison_policy)
    if isinstance(result, pd.DataFrame):
        normalized = result.copy(deep=True)
        # EVAL_CASES section 8: indexes are materialized consistently, so a
        # named-index groupby result and its reset_index form compare equal.
        if any(name is not None for name in normalized.index.names):
            try:
                normalized = normalized.reset_index()
            except ValueError as error:
                raise NormalizationError(
                    "index labels collide with existing columns"
                ) from error
        positional_index = isinstance(normalized.index, pd.RangeIndex)
        if not order_sensitive:
            if all(key in normalized.columns for key in keys):
                normalized = normalized.sort_values(
                    by=keys, kind="mergesort", na_position="last"
                )
            else:
                raise NormalizationError("declared normalization keys are absent")
            if positional_index:
                normalized = normalized.reset_index(drop=True)
        columns = [_json_label(column) for column in normalized.columns]
        records = []
        for index_value, row in normalized.iterrows():
            records.append(
                {
                    "index": _json_label(index_value),
                    "values": [_json_scalar(value) for value in row.tolist()],
                }
            )
        envelope = {
            "kind": "dataframe",
            "columns": columns,
            "index_names": _index_names(normalized.index),
            "records": records,
        }
    elif isinstance(result, pd.Series):
        normalized_series = result.copy(deep=True)
        if not order_sensitive:
            normalized_series = normalized_series.sort_index()
        envelope = {
            "kind": "series",
            "name": _json_label(normalized_series.name),
            "index_names": _index_names(normalized_series.index),
            "records": [
                {"index": _json_label(index_value), "value": _json_scalar(value)}
                for index_value, value in normalized_series.items()
            ],
        }
    else:
        envelope = {"kind": "scalar", "value": _json_scalar(result)}

    try:
        json.dumps(envelope, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as error:
        raise NormalizationError("normalized result is not strict JSON") from error
    return envelope


def _fixed_descendant(root: Path, *parts: str) -> Path:
    resolved_root = Path(root).resolve()
    path = resolved_root.joinpath(*parts).resolve()
    try:
        path.relative_to(resolved_root)
    except ValueError:
        raise TrialValidationError("fixed runtime path escapes Case directory")
    return path


def _validate_trial_paths(
    case_dir: Path,
    arm: str,
    analysis_path: Path,
    trial_id: str,
    evidence_out: Optional[Path],
) -> Tuple[str, str]:
    case_id = Path(case_dir).resolve().name
    match = _TRIAL_PATTERN.match(trial_id)
    if not _CASE_PATTERN.match(case_id) or not match:
        raise TrialValidationError("invalid Case or Trial ID")
    trial_case, trial_arm, repetition = match.groups()
    if arm not in {"A", "B"} or (trial_case, trial_arm) != (case_id, arm):
        raise TrialValidationError("Trial ID must match Case and arm")
    locked_analysis_suffix = (
        "submissions", case_id, arm, repetition, "analysis.py"
    )
    if tuple(Path(analysis_path).resolve().parts[-5:]) != locked_analysis_suffix:
        raise TrialValidationError("analysis path must match the locked submission layout")
    if evidence_out is not None:
        locked_evidence_suffix = ("evidence", "trials", trial_id + ".json")
        if tuple(Path(evidence_out).resolve().parts[-3:]) != locked_evidence_suffix:
            raise TrialValidationError("evidence path must match the locked Trial ID")
    return case_id, repetition


def _load_manifest(case_dir: Path, case_id: str) -> Dict[str, Any]:
    manifest_path = _fixed_descendant(case_dir, "manifest.json")
    with manifest_path.open("r", encoding="utf-8") as source:
        manifest = json.load(source)
    if type(manifest.get("manifest_version")) is not int or manifest.get("manifest_version") != 1:
        raise TrialValidationError("manifest_version must be integer 1")
    if manifest.get("case_id") != case_id:
        raise TrialValidationError("manifest Case does not match Trial")
    locked_fixtures = {
        "visible": "visible/canonical_dev.csv",
        "normal": "private/hidden_normal.csv",
        "counterfactual": "private/hidden_counterfactual.csv",
        "edge": "private/hidden_edge.csv",
    }
    fixture_paths = manifest.get("fixture_paths", manifest.get("fixtures"))
    if fixture_paths != locked_fixtures:
        raise TrialValidationError("manifest fixture paths must use the fixed D-05 layout")
    _validate_comparison_policy(manifest.get("comparison"))
    return manifest


def _load_mapping(case_dir: Path) -> Dict[str, Any]:
    mapping_path = _fixed_descendant(case_dir, "private", "mapping.json")
    with mapping_path.open("r", encoding="utf-8") as source:
        mapping = json.load(source)
    _validate_mapping_document(mapping)
    return mapping


def _load_analysis(analysis_path: Path, frozen_hash: Optional[str] = None) -> Callable[[pd.DataFrame], Any]:
    source_bytes = Path(analysis_path).read_bytes()
    if frozen_hash is not None and hashlib.sha256(source_bytes).hexdigest() != frozen_hash:
        raise ContractError("analysis source changed before entrypoint load")
    module = types.ModuleType("_poc0_submission_{0}".format(hashlib.sha256(source_bytes).hexdigest()))
    module.__file__ = str(Path(analysis_path))
    try:
        code = compile(source_bytes, str(Path(analysis_path)), "exec", dont_inherit=True)
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            exec(code, module.__dict__)
    except BaseException as error:
        raise ContractError("analysis module could not be loaded") from error
    analyze = getattr(module, "analyze", None)
    if not callable(analyze):
        raise ContractError("analysis.py must expose callable analyze(df)")
    try:
        parameters = list(inspect.signature(analyze).parameters.values())
    except (TypeError, ValueError) as error:
        raise ContractError("analyze(df) signature is not inspectable") from error
    if (
        len(parameters) != 1
        or parameters[0].name != "df"
        or parameters[0].kind
        not in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    ):
        raise ContractError("entrypoint must have the exact analyze(df) contract")
    return analyze


def _execute_analysis(analyze: Callable[[pd.DataFrame], Any], bound_df: pd.DataFrame) -> Any:
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        return analyze(bound_df)


def _duration_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def _write_evidence(path: Path, evidence: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as output:
        json.dump(
            evidence,
            output,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        output.write("\n")


def _finish_evidence(
    evidence: Dict[str, Any],
    started: float,
    evidence_out: Optional[Path],
) -> Dict[str, Any]:
    evidence["duration_ms"] = _duration_ms(started)
    if evidence_out is not None:
        _write_evidence(evidence_out, evidence)
    return evidence


def run_trial(
    case_dir: Path,
    arm: str,
    analysis_path: Path,
    trial_id: str,
    evidence_out: Optional[Path] = None,
    before_first_hidden_read: Optional[Callable[[], None]] = None,
) -> Dict[str, Any]:
    """Run the fixed three-fixture D-08 runtime path without scoring results."""
    started = time.perf_counter()
    resolved_case_dir = Path(case_dir).resolve()
    resolved_analysis_path = Path(analysis_path).resolve()
    case_id, _ = _validate_trial_paths(
        resolved_case_dir, arm, resolved_analysis_path, trial_id, evidence_out
    )

    submission_hash = sha256_file(resolved_analysis_path)
    validation = validate_analysis_source(resolved_analysis_path)
    analyze = _load_analysis(resolved_analysis_path, submission_hash)
    manifest = _load_manifest(resolved_case_dir, case_id)
    mapping = _load_mapping(resolved_case_dir)
    reverse_mapping = {shadow: canonical for canonical, shadow in mapping["fields"].items()}

    evidence: Dict[str, Any] = {
        "trial_id": trial_id,
        "case_id": case_id,
        "arm": arm,
        "runtime_status": "running",
        "guard": {
            "status": validation["status"],
            "findings": validation["findings"],
            "scope": "validation_only_not_a_production_sandbox",
        },
        "contract": {"status": "ok", "entrypoint": "analyze"},
        "binding": {
            "status": "ok",
            "mode": "automatic_in_memory",
            "human_binding_steps": 0,
            "bound_hidden_csv_written": False,
            "source_rewritten": False,
        },
        "program_hashes": [
            {"stage": "submission", "sha256": submission_hash}
        ],
        "identity": {"status": "pending", "all_equal": None},
        "executions": [],
        "retry_count": 0,
        "warnings": [
            {
                "code": "known_numexpr_version_warning",
                "detail": "frozen environment warning recorded; no dependency change",
            }
        ],
    }

    if before_first_hidden_read is not None:
        before_first_hidden_read()

    for fixture in FIXTURE_NAMES:
        fixture_path = _fixed_descendant(
            resolved_case_dir, "private", HIDDEN_FILENAMES[fixture]
        )
        canonical_df = pd.read_csv(fixture_path)
        bound_df = bind_hidden_frame(canonical_df, mapping, arm)

        pre_execution_hash = sha256_file(resolved_analysis_path)
        evidence["program_hashes"].append(
            {
                "stage": "pre_execution",
                "fixture": fixture,
                "sha256": pre_execution_hash,
            }
        )
        if pre_execution_hash != submission_hash:
            evidence["runtime_status"] = "failed"
            evidence["identity"] = {"status": "failed", "all_equal": False}
            evidence["failure"] = {
                "stage": "identity",
                "fixture": fixture,
                "code": "program_hash_mismatch",
            }
            return _finish_evidence(evidence, started, evidence_out)

        execution_started = time.perf_counter()
        try:
            actual = _execute_analysis(analyze, bound_df)
            restored = restore_input_labels(actual, reverse_mapping)
            normalized = normalize_result(restored, manifest["comparison"])
        except BaseException as error:
            stage = "normalization" if isinstance(error, NormalizationError) else "execution"
            evidence["runtime_status"] = "failed"
            evidence["identity"] = {"status": "ok", "all_equal": True}
            evidence["failure"] = {
                "stage": stage,
                "fixture": fixture,
                "code": type(error).__name__,
            }
            return _finish_evidence(evidence, started, evidence_out)
        evidence["executions"].append(
            {
                "fixture": fixture,
                "status": "completed",
                "actual": normalized,
                "duration_ms": _duration_ms(execution_started),
            }
        )

    evidence["runtime_status"] = "completed"
    evidence["identity"] = {"status": "ok", "all_equal": True}
    return _finish_evidence(evidence, started, evidence_out)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the exact single-Trial D-08 command."""
    parser = argparse.ArgumentParser(
        description="Run one immutable POC-0 submission on fixed hidden fixtures."
    )
    parser.add_argument("--case-dir", required=True, type=Path)
    parser.add_argument("--arm", required=True, choices=("A", "B"))
    parser.add_argument("--analysis", required=True, type=Path)
    parser.add_argument("--trial-id", required=True)
    parser.add_argument("--evidence-out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        evidence = run_trial(
            args.case_dir,
            args.arm,
            args.analysis,
            args.trial_id,
            args.evidence_out,
        )
    except RunnerError:
        print("Trial {0}: runtime rejected".format(args.trial_id))
        return 1
    print("Trial {0}: runtime {1}".format(args.trial_id, evidence["runtime_status"]))
    return 0 if evidence["runtime_status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
