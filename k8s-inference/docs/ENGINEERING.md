# Engineering rules

How this solution stays small enough for one person to review. These rules bind every change, whether a person or
an agent writes it; `make check` enforces the measurable ones.

## Why

Between 13 and 17 October 2026 the API service grew from 23 modules and 11,000 lines to 31 modules and 21,000 lines
in 97 releases, without one deletion. Every release was correct and gated; the sum was unreviewable and had to be
refactored. The rules below are the lessons of that week.

## The boundary

Serverless 2.0 is the substrate: submit a run, admit it, place it, report its status and result, serve an endpoint,
meter a key. Workflow logic (what a run does next, how runs relate, what an application delivers to whom) belongs
to the application above it. A requirement that only one application has does not go into the substrate; the
substrate offers the primitive the application composes it from. When in doubt, the question is "would a second,
unrelated customer of this substrate need this?" If the answer is no, it is not substrate.

## Size budget (enforced)

- `services/api/LOC_BUDGET` is the maximum number of code lines (blank lines, comments and docstrings excluded) in
  `services/api/*.py`, tests excluded. `tools/loc.py` counts them; `make check` fails above the budget.
- The budget only goes down. A release that adds code removes at least as much. Raising the budget requires an
  owner-reviewed reason in the commit message and in CHANGELOG.md, and is the exception.
- No new module without retiring one. A new concept is folded into the concept it refines (a run has attempts; an
  attempt is not a new module).
- `tools/loc.py --write` prints the per-module table; every release's CHANGELOG entry states the delta.

## What may not be added

- **An option without a second caller.** A knob, mode, delivery variant or configuration attribute needs two
  independent consumers before it exists; one caller gets the behaviour unconditionally.
- **An incident workaround without an expiry.** A fix for a specific failure (a timeout bound, a projection
  override, a warmer) names the root cause and the release by which the root cause is fixed and the workaround
  removed. Workarounds older than ten releases are deleted by the next release.
- **A second implementation of a platform primitive.** Caches, informers, leader election, retries, queueing,
  dependencies, scheduling fit, rate limits, secrets distribution: check `clusters/common/apps/` and
  `docs/ARCHITECTURE.md` for what is installed, and use it. Hand-rolled copies are refused in review.
- **A second vocabulary.** One shape per concept across definition, storage, render and read-back. No translation
  layers "for compatibility" with something that no longer exists.
- **A helper that exists already.** Before writing `_ts`, `_secs`, a token verifier, a lineage walker or a TTL
  cache, grep for it. One copy, imported.
- **Legacy paths.** When a contract changes, the old path is removed in the same release or the release is not
  cut. "Legacy", "compat" and "transitional" do not appear in new code.

## Shape of the code

- Routes are thin: validate, call one function, return. Logic lives in the module that owns the concept.
- One state machine per concept, in one module, with the states enumerated in one place.
- Functions under 60 lines. A function over 100 lines is a review finding.
- A test tests a contract (what a caller observes), not a line of code. Tests of internals are deleted with the
  internals. Test fixtures are shared through `tests/conftest.py`, not rebuilt per file.
- Docs describe the current behaviour without release stamps, incident narration or dates; history is CHANGELOG.md.

## Release discipline

- One release per change, one change per release, and no release without a gate.
- Every receipt reports: lines before and after, modules before and after, routes before and after.
- A release that touches more than five modules is split, or it is a refactor and says so.
- Agents working on this tree read this file first and refuse changes that violate it, including their own.
