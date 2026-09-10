#!/usr/bin/env python3
"""Fail fast unless buffered native MolMIM beats the conventional-cached median."""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from pathlib import Path
from typing import Any


class ComparisonError(ValueError):
    pass


DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")


def _positive_median(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ComparisonError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ComparisonError(f"{label} must be positive and finite")
    return result


def _n3_median(value: dict[str, Any], label: str) -> float:
    run_ids = value.get("run_ids")
    if (
        not isinstance(run_ids, list)
        or len(run_ids) != 3
        or any(
            not isinstance(run_id, str)
            or len(run_id) > 28
            or DNS_LABEL.fullmatch(run_id) is None
            for run_id in run_ids
        )
        or len(set(run_ids)) != 3
    ):
        raise ComparisonError(f"{label} run IDs must be three unique DNS labels")
    raw = value.get("demand_to_two_semantic_seconds")
    if not isinstance(raw, list) or len(raw) != 3:
        raise ComparisonError(f"{label} must contain exactly three raw timings")
    timings = [
        _positive_median(item, f"{label} raw timing {index}")
        for index, item in enumerate(raw, 1)
    ]
    statistics_seconds = value.get("statistics_seconds")
    if not isinstance(statistics_seconds, dict):
        raise ComparisonError(f"{label} statistics are malformed")
    reported = _positive_median(
        statistics_seconds.get("demand_to_two_semantic_median"), f"{label} median"
    )
    computed = statistics.median(timings)
    if reported != computed:
        raise ComparisonError(f"{label} median does not match its raw timings")
    return computed


def compare(conventional: dict[str, Any], native: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(conventional, dict) or not isinstance(native, dict):
        raise ComparisonError("comparison inputs must be JSON objects")
    if (
        conventional.get("schema")
        != "archvteams.nebius.ai/molmim-conventional-cached-n3/v1"
        or conventional.get("status") != "PASS"
        or conventional.get("mode") != "conventional-cached"
        or conventional.get("trial_count") != 3
        or conventional.get("request_count") != 6
        or conventional.get("semantic_pass_count") != 6
    ):
        raise ComparisonError("conventional input is not an n=3 strict PASS")
    if (
        native.get("schema") != "archvteams.nebius.ai/molmim-native-n3/v1"
        or native.get("status") != "PASS"
        or native.get("image_io_mode") != "buffered"
        or native.get("checkpoint_id") != "molmim-native-f7-v2-buffered"
        or native.get("trial_count") != 3
        or native.get("request_count") != 6
        or native.get("semantic_pass_count") != 6
    ):
        raise ComparisonError("native input is not the exact buffered n=3 strict PASS")
    conventional_median = _n3_median(conventional, "conventional")
    native_median = _n3_median(native, "buffered native")
    delta = native_median - conventional_median
    ratio = native_median / conventional_median
    won = native_median < conventional_median
    return {
        "schema": "archvteams.nebius.ai/molmim-startup-decision/v1",
        "status": "PASS" if won else "REJECTED",
        "recommendation": (
            "PROMOTE_BUFFERED_NATIVE_FOR_FURTHER_QUALIFICATION"
            if won
            else "REJECT_NATIVE_RESTORE_KEEP_CONVENTIONAL_CACHED"
        ),
        "conventional_median_seconds": conventional_median,
        "buffered_native_median_seconds": native_median,
        "native_minus_conventional_seconds": delta,
        "native_to_conventional_ratio": ratio,
        "native_percent_change": (ratio - 1.0) * 100.0,
        "strict_fail_fast": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conventional", type=Path, required=True)
    parser.add_argument("--buffered-native", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = compare(
            json.loads(args.conventional.read_text(encoding="utf-8")),
            json.loads(args.buffered_native.read_text(encoding="utf-8")),
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        print(f"MolMIM startup comparison refused: {exc}", file=sys.stderr)
        return 2
    json.dump(result, sys.stdout, sort_keys=True, indent=2)
    sys.stdout.write("\n")
    # Exit 3 intentionally stops a set -e execution plan on a restore regression.
    return 0 if result["status"] == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
