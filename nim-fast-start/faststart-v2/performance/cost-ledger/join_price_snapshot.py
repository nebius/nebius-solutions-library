#!/usr/bin/env python3
"""Join an explicit price snapshot to a usage ledger using Decimal arithmetic."""

from __future__ import annotations

import argparse
import sys

from ledgerlib import LedgerError, dump_json, join_price_snapshot, load_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, help="unpriced usage-ledger JSON")
    parser.add_argument("--price-snapshot", required=True, help="explicit price snapshot JSON")
    parser.add_argument("--output", required=True, help="joined ledger JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        ledger, _ = load_json(args.ledger)
        snapshot, snapshot_sha256 = load_json(args.price_snapshot)
        dump_json(
            join_price_snapshot(ledger, snapshot, snapshot_sha256),
            args.output,
        )
    except (LedgerError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
