#!/usr/bin/env python3
"""Build a deterministic usage ledger from one or more explicit receipts."""

from __future__ import annotations

import argparse
import sys

from ledgerlib import LedgerError, build_usage_ledger, dump_json, load_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--receipt",
        action="append",
        required=True,
        help="compact faststart-usage-receipt/v1 JSON; repeat for receipt shards",
    )
    parser.add_argument("--output", required=True, help="output usage-ledger JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        receipts = [load_json(path) for path in args.receipt]
        dump_json(build_usage_ledger(receipts), args.output)
    except (LedgerError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
