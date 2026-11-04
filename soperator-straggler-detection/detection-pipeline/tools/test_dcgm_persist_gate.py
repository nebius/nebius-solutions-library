#!/usr/bin/env python3
"""Offline unit test for the standalone DCGM-fallback persistence gate
(alerting/alert_engine.py, DCGM_FALLBACK_STANDALONE_PERSIST_REQUIRED).
Extracts the gate block from the real source and replays sweep sequences."""
import re, sys, collections, os
src = open(os.path.join(os.path.dirname(__file__), "..", "alerting", "alert_engine.py")).read()
m = re.search(r"(                if result is None:\n.*?self\.was_firing\[key\] = fired\n)", src, re.S)
assert m, "gate block not found"
block = "\n".join(l[16:] for l in m.group(1).splitlines())
N = int(re.search(r'DCGM_FALLBACK_STANDALONE_PERSIST_REQUIRED", "(\d+)"', src).group(1))

def replay(seq):
    class S: pass
    s = S(); s._dcgm_fallback_streak = {}; s.was_firing = collections.defaultdict(bool)
    emitted = []
    class E:  # capture emission
        pass
    for i, r in enumerate(seq):
        result = None if r is None else (r, 0, {})
        key = ("k",)
        env = {"self": s, "result": result, "key": key,
               "DCGM_FALLBACK_STANDALONE_PERSIST_REQUIRED": N,
               "hostname": "h", "comm": "c", "slot": 0, "per_member": {}}
        env["self"]._emit_dcgm_fallback = lambda *a, i=i: emitted.append(i)
        exec(block, env)
    return emitted

cases = [
    ("single-snapshot blip (old behavior would page)", ["A", None, None, None], []),
    ("two-sweep blip", ["A", "A", None, "A", None], []),
    ("sustained 3 sweeps -> one page at 3rd", ["A", "A", "A", "A", "A"], [2]),
    ("member switches resets streak", ["A", "A", "B", "B", "B"], [4]),
    ("sustained, clears, recurs -> pages again", ["A","A","A",None,"A","A","A"], [2, 6]),
]
bad = 0
for name, seq, want in cases:
    got = replay(seq); ok = got == want; bad += not ok
    print(("PASS" if ok else "FAIL"), name, "emit_at_sweep=", got, "want=", want)
print(f"persist_required={N}")
_bad_standalone = bad

# ---- cascade re-sample loop (same file, second test) ----
def _cascade_main():
    m = re.search(r"(                result = self\._dcgm_fallback_evaluate\(h, dep_comm\)\n                if result is not None and not self\.was_firing\[key\]:\n.*?                fired = result is not None\n)", src, re.S)
    assert m, "cascade block not found"
    blk = "\n".join(l[16:] for l in m.group(1).splitlines())
    def run(readings):
        it = iter(readings)
        class S:
            was_firing = collections.defaultdict(bool)
            def _dcgm_fallback_evaluate(self, h, c):
                return next(it)
        class T:  # no real sleeping
            @staticmethod
            def sleep(x): pass
        env = {"self": S(), "h": "h", "dep_comm": "c", "key": "k", "time": T,
               "DCGM_FALLBACK_CASCADE_RESAMPLES": 2, "DCGM_FALLBACK_CASCADE_RESAMPLE_INTERVAL_S": 5}
        exec(blk, env); return env["fired"]
    R = lambda m: (m, 0, {})
    cases = [("transient (first positive, resample None)", [R("A"), None, None], False),
             ("transient (second resample clears)", [R("A"), R("A"), None], False),
             ("member flips on resample", [R("A"), R("B"), R("B")], False),
             ("sustained all samples same member", [R("A"), R("A"), R("A")], True),
             ("never positive", [None], False)]
    bad = 0
    for n, rd, want in cases:
        got = run(rd); bad += got != want
        print(("PASS" if got == want else "FAIL"), "cascade:", n, "fired=", got)
    return bad
sys.exit(1 if (_cascade_main() or _bad_standalone) else 0)
