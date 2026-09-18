#!/usr/bin/env python3
"""Validate and independently recompute a faststart-usage-ledger/v1 document."""

from __future__ import annotations

import argparse
import sys

from ledgerlib import LedgerError, load_json, validate_ledger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledger", help="usage-ledger JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        ledger, _ = load_json(args.ledger)
        validate_ledger(ledger)
    except (LedgerError, OSError) as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 2
    print("VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
