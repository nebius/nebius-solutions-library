#!/usr/bin/env python3
"""pin_resolve.py -- NEW, UNVALIDATED. Decide the effective pinning mode from the NCCL that will really be loaded.

Prints shell-evaluable `export K='V'` lines:
  PIN_MODE_RESOLVED  off|dump|full      PIN_NCCL_LIB_RESOLVED  path or ''
  PIN_NCCL_VERSION   e.g. 2.32.3 or ''  PIN_REASON             one human sentence

Requested mode (PIN_MODE, or --mode):
  off   -> off.
  dump  -> dump (Inspector's dump thread only; works on any NCCL).
  full  -> full iff the NCCL exposes ncclProfilerThreadCreate in a readable symtab, else DOWNGRADED to dump (loudly).
  auto  -> full if the capability is present, else dump. Never claims more than the NCCL can support.

Capability test = rank_setup.profiler_ranges(lib): the exact mechanism the interposer uses to find NCCL's profiler
thread, so this decision cannot disagree with what the pinning code can actually do. (NCCL < 2.32 lacks the symbol;
a stripped libnccl has no symtab -> both give 'dump'.)

NCCL resolution order: PIN_NCCL_LIB, NCCL_OVERRIDE_DIR/libnccl.so.2, each LD_LIBRARY_PATH dir, `ldconfig -p`,
pip-bundled nvidia/nccl in sys.path. This mirrors dynamic-loader precedence closely but is a prediction;
verify_pinning.py cross-checks it against the library actually mapped by each rank.
"""
import argparse, ctypes, os, re, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rank_setup import profiler_ranges  # noqa: E402


def find_nccl():
    cands = []
    if os.environ.get("PIN_NCCL_LIB"):
        cands.append(os.environ["PIN_NCCL_LIB"])
    if os.environ.get("NCCL_OVERRIDE_DIR"):
        cands.append(os.path.join(os.environ["NCCL_OVERRIDE_DIR"], "libnccl.so.2"))
    for d in os.environ.get("LD_LIBRARY_PATH", "").split(":"):
        if d:
            cands.append(os.path.join(d, "libnccl.so.2"))
    try:
        out = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True, timeout=10).stdout
        cands += re.findall(r"libnccl\.so\.2 .*=> (\S+)", out)
    except Exception:
        pass
    for p in sys.path:
        cands.append(os.path.join(p, "nvidia", "nccl", "lib", "libnccl.so.2"))
    for c in cands:
        if c and os.path.exists(c):
            return os.path.realpath(c)
    return ""


def nccl_version(lib):
    """Real version via ncclGetVersion() in a throwaway subprocess (so this process never loads NCCL)."""
    # NCCL >= 2.9 encodes major*10000+minor*100+patch
    code = ("import ctypes,sys\nl=ctypes.CDLL(sys.argv[1]);v=ctypes.c_int()\n"
            "l.ncclGetVersion(ctypes.byref(v));n=v.value\n"
            "print('%d.%d.%d'%(n//10000,(n//100)%100,n%100))\n")
    try:
        r = subprocess.run([sys.executable, "-I", "-c", code, lib], capture_output=True, text=True, timeout=30)
        return r.stdout.strip()
    except Exception:
        m = re.search(r"\.so\.(\d+\.\d+\.\d+)", lib)
        return m.group(1) if m else ""


def resolve(requested):
    lib = find_nccl()
    ver = nccl_version(lib) if lib else ""
    if requested == "off":
        return "off", lib, ver, "PIN_MODE=off requested"
    if requested == "dump":
        return "dump", lib, ver, "dump-only requested; dump thread pinning needs no particular NCCL"
    if requested not in ("full", "auto"):
        return "off", lib, ver, f"unknown PIN_MODE={requested!r}; running unpinned"
    if not lib:
        why = "could not locate libnccl.so.2 to probe"
        return "dump", lib, ver, f"{why}; falling back to dump-only ({requested} requested)"
    rng = profiler_ranges(lib)
    if rng:
        return "full", lib, ver, f"NCCL {ver or '?'} exposes ncclProfilerThreadCreate -> profiler thread can be pinned"
    why = (f"NCCL {ver or '?'} at {lib} has no readable ncclProfilerThreadCreate symbol "
           "(NCCL < 2.32, or a stripped build)")
    tag = "DOWNGRADED from full" if requested == "full" else "auto"
    return "dump", lib, ver, f"{why}; using dump-only ({tag}). Build NCCL >= 2.32.3 via pinning/nccl/fetch_build_nccl.sh for full dedication"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default=os.environ.get("PIN_MODE", "auto"))
    a = ap.parse_args()
    mode, lib, ver, why = resolve(a.mode)
    for k, v in (("PIN_MODE_RESOLVED", mode), ("PIN_NCCL_LIB_RESOLVED", lib), ("PIN_NCCL_VERSION", ver), ("PIN_REASON", why)):
        print(f"export {k}='{v.replace(chr(39), '')}'")


if __name__ == "__main__":
    main()
