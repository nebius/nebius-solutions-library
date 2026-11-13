#!/usr/bin/env python3
"""Code lines of the API service against its budget (docs/ENGINEERING.md "Size budget").

Counts non-blank lines that are neither comments nor docstrings in services/api/*.py (tests excluded) and fails
when the total is above services/api/LOC_BUDGET. The budget only ever goes down: a release that adds code must
remove at least as much elsewhere, or lower the budget with a reviewed reason. `--write` prints the per-module
table; `--set` writes the current total as the new budget (only when it is lower than the budget)."""

import ast
import io
import pathlib
import sys
import tokenize

ROOT = pathlib.Path(__file__).resolve().parent.parent / "services" / "api"
BUDGET = ROOT / "LOC_BUDGET"


def code_lines(path: pathlib.Path) -> int:
    src = path.read_text()
    tree = ast.parse(src)
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(getattr(node.body[0], "value", None), ast.Constant)
            ):
                if isinstance(node.body[0].value.value, str):
                    skip.update(range(node.body[0].lineno, node.body[0].end_lineno + 1))
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            skip.add(tok.start[0])
    return sum(1 for i, line in enumerate(src.splitlines(), 1) if line.strip() and i not in skip)


def main() -> int:
    per = {p.name: code_lines(p) for p in sorted(ROOT.glob("*.py"))}
    total = sum(per.values())
    budget = int(BUDGET.read_text()) if BUDGET.exists() else None
    if "--write" in sys.argv:
        for name, n in sorted(per.items(), key=lambda kv: -kv[1]):
            print(f"{n:6d}  {name}")
    print(f"loc: services/api code lines {total} (modules {len(per)}), budget {budget}")
    if "--set" in sys.argv:
        if budget is None or total < budget:
            BUDGET.write_text(f"{total}\n")
            print(f"loc: budget set to {total}")
        else:
            print("loc: budget not lowered (current total is not below it)")
        return 0
    if budget is not None and total > budget:
        print(f"loc: FAIL, {total - budget} lines over budget; remove code or lower the budget with a reviewed reason")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
