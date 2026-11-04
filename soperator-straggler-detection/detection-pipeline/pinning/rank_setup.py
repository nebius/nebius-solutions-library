#!/usr/bin/env python3
"""rank_setup.py -- NEW, UNVALIDATED topology-aware core selection for the
thread-pinning interposer (written from scratch; NOT the original rank_setup.py).

Every rank on a node runs this with the same inputs and derives the same global
plan deterministically (no inter-rank communication):
  * GPU -> NUMA node: nvidia-smi pci.bus_id -> /sys/bus/pci/devices/<id>/numa_node
    (fallback when the kernel reports -1: split physical cores evenly by GPU index)
  * physical cores: /sys/devices/system/cpu/cpuN/topology/thread_siblings_list,
    restricted to the CPUs this process may actually use (sched_getaffinity)
  * per NUMA node, reserve --cores-per-role physical cores per role per local rank,
    taken from the TOP of the NUMA node's core list (never the core holding CPU 0)
  * the reserved physical cores (all SMT siblings) are removed from every rank's
    training CPU set; the role thread itself is pinned to the first logical CPU
    of its core (SMT sibling left idle = true core dedication).
Output (--emit-env): shell exports TRAIN_CPUS, PIN_DUMP_CPUS, PIN_PROFILER_CPUS,
PIN_PROFILER_RANGES. --plan prints the whole-node plan as JSON.
"""
import argparse, glob, json, os, re, struct, subprocess, sys


def rd(p):
    with open(p) as f:
        return f.read().strip()


def parse_list(s):
    out = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-")
            out += range(int(a), int(b) + 1)
        else:
            out.append(int(part))
    return out


def gpu_numa_nodes(n_gpus, nodes):
    res = {}
    try:
        q = subprocess.run(["nvidia-smi", "--query-gpu=index,pci.bus_id", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=30).stdout.strip().splitlines()
        for line in q:
            idx, bus = [x.strip() for x in line.split(",")]
            bus = bus.lower()
            bus = bus[-12:] if len(bus) > 12 else bus  # 00000000:18:00.0 -> 0000:18:00.0
            p = f"/sys/bus/pci/devices/{bus}/numa_node"
            if os.path.exists(p) and int(rd(p)) >= 0:
                res[int(idx)] = int(rd(p))
    except Exception:
        pass
    for g in range(n_gpus):  # fallback: even split
        if g not in res:
            res[g] = nodes[min(len(nodes) - 1, g * len(nodes) // max(n_gpus, 1))]
    return res


def cpu_topology(allowed):
    phys = {}  # first-sibling -> sorted allowed siblings
    for c in sorted(allowed):
        sib = parse_list(rd(f"/sys/devices/system/cpu/cpu{c}/topology/thread_siblings_list"))
        key = min(sib)
        phys.setdefault(key, sorted(s for s in sib if s in allowed))
    numa = {}
    for nd in glob.glob("/sys/devices/system/node/node[0-9]*"):
        n = int(nd.rsplit("node", 1)[1])
        numa[n] = set(parse_list(rd(nd + "/cpulist"))) & allowed
    return phys, {n: v for n, v in numa.items() if v}


def make_plan(local_world, cores_per_role, roles):
    allowed = set(os.sched_getaffinity(0)) if not os.environ.get("PIN_ALLOWED_CPUS") \
        else set(parse_list(os.environ["PIN_ALLOWED_CPUS"]))
    phys, numa = cpu_topology(allowed)
    g2n = gpu_numa_nodes(local_world, sorted(numa))
    plan = {"ranks": {}, "reserved_cpus": [], "numa": {str(k): sorted(v) for k, v in numa.items()},
            "gpu_numa": g2n, "cores_per_role": cores_per_role, "roles": roles}
    reserved_phys = {}  # numa -> list of phys keys already taken
    by_numa = {}
    for g in range(local_world):
        by_numa.setdefault(g2n[g], []).append(g)
    all_reserved = set()
    for n, gpus in by_numa.items():
        cand = [k for k in sorted(phys, reverse=True) if set(phys[k]) <= numa[n] and 0 not in phys[k]]
        need = len(gpus) * len(roles) * cores_per_role
        if need > len(cand):
            raise SystemExit(f"rank_setup: NUMA node {n} needs {need} physical cores for pinning but only {len(cand)} available")
        it = iter(cand)
        for g in sorted(gpus):
            r = {"numa": n}
            for role in roles:
                keys = [next(it) for _ in range(cores_per_role)]
                r[role] = {"phys_cores": keys, "pin_cpus": keys, "reserved_cpus": sorted(c for k in keys for c in phys[k])}
                all_reserved |= set(r[role]["reserved_cpus"])
            plan["ranks"][str(g)] = r
    for g in range(local_world):
        r = plan["ranks"][str(g)]
        r["train_cpus"] = sorted((numa[r["numa"]]) - all_reserved)
        if not r["train_cpus"]:
            raise SystemExit(f"rank_setup: no training CPUs left for local rank {g}")
    plan["reserved_cpus"] = sorted(all_reserved)
    return plan


def profiler_ranges(lib):
    """(module-basename, start, size) for every symtab entry of ncclProfilerThreadCreate
    (incl. .cold). Returns [] if lib is stripped or has no such symbol (NCCL < 2.32.3)."""
    out = []
    try:
        lib = os.path.realpath(lib)
        with open(lib, "rb") as f:
            d = f.read()
        shoff, shentsize, shnum = struct.unpack_from("<Q", d, 0x28)[0], *struct.unpack_from("<HH", d, 0x3A)
        secs = [struct.unpack_from("<IIQQQQIIQQ", d, shoff + i * shentsize) for i in range(shnum)]
        for s in secs:
            if s[1] != 2:  # SHT_SYMTAB
                continue
            strtab = secs[s[6]]
            for off in range(s[4], s[4] + s[5], 24):
                nm, info, oth, shndx, val, size = struct.unpack_from("<IBBHQQ", d, off)
                if shndx == 0 or (info & 0xF) != 2:
                    continue
                name = d[strtab[4] + nm: d.index(b"\0", strtab[4] + nm)].decode(errors="replace")
                if "ncclProfilerThreadCreate" in name:
                    out.append((os.path.basename(lib).split('.so')[0] + '.so', val, size))  # dladdr may report the libnccl.so.2 symlink name
    except Exception as e:
        print(f"# rank_setup: could not read symtab of {lib}: {e}", file=sys.stderr)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local-rank", type=int, default=int(os.environ.get("LOCAL_RANK", 0)))
    ap.add_argument("--local-world", type=int, default=int(os.environ.get("LOCAL_WORLD_SIZE", 8)))
    ap.add_argument("--cores-per-role", type=int, default=int(os.environ.get("PIN_CORES_PER_ROLE", 1)))
    ap.add_argument("--roles", default=os.environ.get("PIN_ROLES", "dump,profiler"))
    ap.add_argument("--nccl-lib", default=os.environ.get("PIN_NCCL_LIB", ""))
    ap.add_argument("--emit-env", action="store_true")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--housekeeping", action="store_true", help="print the CPU list pipeline helpers (aggregator, iowait logger, bpftrace) should be confined to: all usable CPUs minus every reserved (dedicated) core")
    a = ap.parse_args()
    roles = [r for r in a.roles.split(",") if r]
    plan = make_plan(a.local_world, a.cores_per_role, roles)
    if a.plan:
        print(json.dumps(plan, indent=1))
        return
    if a.housekeeping:
        allowed = set(os.sched_getaffinity(0)) - set(plan["reserved_cpus"])
        print(",".join(map(str, sorted(allowed))))
        return
    r = plan["ranks"][str(a.local_rank)]
    # the thread is pinned to the first logical CPU of each dedicated physical core
    ex = {"TRAIN_CPUS": ",".join(map(str, r["train_cpus"]))}
    ex["PIN_DUMP_CPUS"] = ",".join(map(str, r["dump"]["pin_cpus"])) if "dump" in r else ""
    ex["PIN_PROFILER_CPUS"] = ",".join(map(str, r["profiler"]["pin_cpus"])) if "profiler" in r else ""
    rng = profiler_ranges(a.nccl_lib) if a.nccl_lib and "profiler" in roles else []
    ex["PIN_PROFILER_RANGES"] = ",".join(f"{m}:{s:x}:{z:x}" for m, s, z in rng)
    ex["PIN_RESERVED_CPUS"] = ",".join(map(str, plan["reserved_cpus"]))
    for k, v in ex.items():
        print(f"export {k}='{v}'")


if __name__ == "__main__":
    main()
