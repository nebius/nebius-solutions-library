"""Shared loader/lookup for observability/workload_reliability_reference.yaml
-- used by BOTH the live alert_engine.py (role-baseline UNVALIDATED gating,
see ROLE_BASELINE_UNVALIDATED_GATING_ACTIVE's own comment) and the offline,
read-only tools/incident_correlator.py. Factored out to one real module
instead of two copies, so there is exactly one place this lookup logic can
drift.

V1-beta-followup: this is a deliberate, disclosed exception to this
project's own long-standing "the live pipeline stays stdlib-only" design
(see MAINTENANCE.md's version-pinned-dependencies table) -- requires
PyYAML (`apt install python3-yaml`), now a real dependency of the live
pipeline, not just the offline tool, because the live check genuinely
needs this same data to decide its own tier/presentation. Not undone
quietly; see MAINTENANCE.md's own updated entry."""
import os

import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(_HERE, "workload_reliability_reference.yaml")

CHECK_KEYS = ("alert_positive_deviation", "wait_induced", "role_baseline")


def load_reference(path=DEFAULT_PATH):
    with open(path) as f:
        return yaml.safe_load(f)


def lookup_reference(ref, workload_sig):
    """Exact-string match against one of a shape's real workload_sigs --
    never fuzzy (a near-miss sig is a DIFFERENT real workload, not a typo
    to paper over). workload_sigs is a LIST per entry -- the same real
    workload can produce more than one real sig variant across different
    jobs (confirmed live). Falls back to the file's own explicit
    defaults.unknown_sig_* for anything unresolved or unrecognized --
    hard default is UNVALIDATED, never a guess borrowed from another
    entry."""
    if workload_sig:
        for entry in ref.get("workload_shapes", []):
            if workload_sig in entry.get("workload_sigs", []):
                return entry
    d = ref.get("defaults", {})
    return {
        "workload_sig": workload_sig,
        "human_name": "unknown workload shape",
        "checks": {
            name: {"status": d.get("unknown_sig_status", "UNVALIDATED"),
                   "note": d.get("unknown_sig_note", "").strip(),
                   "evidence_ref": None}
            for name in CHECK_KEYS
        },
    }


def check_status(ref, workload_sig, check_key):
    """Convenience: just the status string (e.g. 'KNOWN_RELIABLE',
    'UNVALIDATED') for one check on one real workload_sig, with the same
    honest UNVALIDATED default lookup_reference already applies."""
    entry = lookup_reference(ref, workload_sig)
    return entry.get("checks", {}).get(check_key, {}).get("status", "UNVALIDATED")
