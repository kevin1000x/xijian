"""Generate a deterministic Shadow package from one visible canonical fixture."""

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import pandas as pd


MAPPING_VERSION = 1
SCHEMA_VERSION = 1
VISIBLE_CSV_PARTS = ("visible", "canonical_dev.csv")
MAPPING_PARTS = ("private", "mapping.json")
SHADOW_DATA_PARTS = ("external", "baseline_b", "shadow_data.csv")
SHADOW_SCHEMA_PARTS = ("external", "baseline_b", "shadow_schema.json")


def build_mapping(columns: Sequence[str]) -> Dict[str, Any]:
    """Map unique, nonempty canonical columns to sequential Shadow symbols."""
    if isinstance(columns, (str, bytes)):
        raise ValueError("columns must be a sequence of column names")

    canonical_columns = list(columns)
    normalized_columns = []
    for canonical_column in canonical_columns:
        if not isinstance(canonical_column, str) or not canonical_column.strip():
            raise ValueError("visible CSV headers must be nonempty strings")
        normalized_columns.append(canonical_column.strip())

    if not canonical_columns:
        raise ValueError("visible CSV must contain a header row")
    if len(set(normalized_columns)) != len(normalized_columns):
        raise ValueError("visible CSV headers must be unique")

    fields = {
        canonical_column: "COL_{0:04d}".format(index)
        for index, canonical_column in enumerate(canonical_columns, start=1)
    }
    return {"mapping_version": MAPPING_VERSION, "fields": fields}


def _fixed_descendant(case_dir: Path, parts: Sequence[str]) -> Path:
    path = case_dir.joinpath(*parts).resolve()
    try:
        path.relative_to(case_dir)
    except ValueError:
        raise ValueError("fixed generator path escapes case directory")
    return path


def _read_header(visible_path: Path) -> Sequence[str]:
    with visible_path.open("r", encoding="utf-8-sig", newline="") as source:
        try:
            return next(csv.reader(source))
        except StopIteration:
            raise ValueError("visible CSV must contain a header row")


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as target:
        json.dump(payload, target, ensure_ascii=False, indent=2)
        target.write("\n")


def generate_shadow_package(case_dir: Path) -> Dict[str, Any]:
    """Create fixed mapping, Shadow CSV, and dtype-only Shadow Schema outputs."""
    resolved_case_dir = Path(case_dir).resolve()
    visible_path = _fixed_descendant(resolved_case_dir, VISIBLE_CSV_PARTS)
    mapping_path = _fixed_descendant(resolved_case_dir, MAPPING_PARTS)
    shadow_data_path = _fixed_descendant(resolved_case_dir, SHADOW_DATA_PARTS)
    shadow_schema_path = _fixed_descendant(resolved_case_dir, SHADOW_SCHEMA_PARTS)

    if not visible_path.is_file():
        raise FileNotFoundError(str(visible_path))

    canonical_columns = list(_read_header(visible_path))
    mapping = build_mapping(canonical_columns)
    canonical_df = pd.read_csv(visible_path)
    if list(canonical_df.columns) != canonical_columns:
        raise ValueError("parsed visible CSV headers differ from the source header")

    shadow_symbols = list(mapping["fields"].values())
    shadow_df = canonical_df.copy()
    shadow_df.columns = shadow_symbols
    shadow_schema = {
        "schema_version": SCHEMA_VERSION,
        "fields": [
            {"name": name, "dtype": str(dtype)}
            for name, dtype in zip(shadow_symbols, shadow_df.dtypes)
        ],
    }

    _write_json(mapping_path, mapping)
    shadow_data_path.parent.mkdir(parents=True, exist_ok=True)
    shadow_df.to_csv(
        shadow_data_path,
        index=False,
        encoding="utf-8",
        lineterminator="\n",
    )
    _write_json(shadow_schema_path, shadow_schema)

    return {
        "mapping_path": mapping_path,
        "shadow_data_path": shadow_data_path,
        "shadow_schema_path": shadow_schema_path,
        "row_count": len(shadow_df),
        "column_count": len(shadow_df.columns),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the fixed POC-0 Shadow generation command."""
    parser = argparse.ArgumentParser(
        description="Generate a Baseline B Shadow package from a visible fixture."
    )
    parser.add_argument("--case-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    generate_shadow_package(args.case_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
