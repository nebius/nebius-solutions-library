"""Mechanism-based exclusion helpers -- NOT hardcoded rank numbers.

Two independent mechanisms are excluded from straggler/fail-stop alerting,
each identified by the real underlying cause rather than a fixed rank:

1. The rendezvous-coordinator process: in this torchrun/c10d setup, the
   process launched with (node_rank=0, local_rank=0) hosts the rendezvous
   store and carries real, structural coordination overhead. This is a
   ROLE, derived from job topology, not a number -- it evaluates to global
   rank 0 in this specific 2-node/8-per-node layout, but the rule itself
   ("node_rank 0's local_rank 0") is what generalizes, not "rank == 0".
   Confirmed as the RAS MISMATCH lag's primary cause in the P20c-prerequisite
   investigation (consistent -3 to -8 operation lag across 4 independent
   healthy runs, magnitude far beyond +-1 ordinary async jitter), and
   independently corroborated by detection.py's own long-standing comment
   ("rank0's master-process overhead") for the CV statistic.

2. Known-degraded GPUs: identified LIVE from the standing TFLOPS
   health-check (the same mechanism used throughout this whole project,
   e.g. run_health_check.sh's >10%-below-node-median flag), not a fixed
   rank/GPU number. Whichever GPU is flagged today is excluded today; if
   GPU3 were repaired or a different GPU degraded, this follows the real
   hardware state automatically. Confirmed as the RAS MISMATCH lag's
   secondary cause (smaller, ~1-2 operation lag, consistently the same
   GPU the health-check already flags).
"""
import json
import subprocess


def rendezvous_coordinator_rank(node_rank_of_host, local_ranks_per_host, coordinator_host):
    """Returns the global rank that is this job's rendezvous coordinator:
    local_rank 0 on whichever host is node_rank 0 (the --rdzv_endpoint
    host). Derived from topology, not hardcoded -- in this project's
    standard 2-node/8-per-node torchrun launch that's worker-0's local
    rank 0, which this function still *computes* rather than assumes.
    """
    for global_rank, (host, local_rank) in local_ranks_per_host.items():
        if host == coordinator_host and local_rank == 0:
            return global_rank
    return None


def degraded_gpus_live(hosts=("worker-0", "worker-1"), margin_pct=10.0,
                        bench_script="/root/P4d_clean/health/bench_all_gpus.py",
                        image="nvcr.io#nvidia/pytorch:25.01-py3"):
    """Runs the same isolated-matmul TFLOPS benchmark run_health_check.sh
    uses, live, and returns {host: set(gpu_indices)} for GPUs more than
    margin_pct below that host's own median -- the exact flag rule
    run_health_check.sh already applies. No cached/stale list: this is a
    fresh, real query each call, matching how every fault/health
    determination has been made throughout this project.
    """
    mounts = ("/usr/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu,"
              "/usr/lib64:/usr/lib64,/root:/root")
    result = {}
    for host in hosts:
        # Cluster-topology-agnostic fix (this session): --gpus-per-node
        # used to be hardcoded 8 -- silently requesting the wrong real
        # GPU count on a differently-shaped node. Discovered live via a
        # direct `nvidia-smi -L` count (same real mechanism cause_metrics.
        # discover_gpu_count uses in the P18k_classifier package -- kept
        # as a small, self-contained duplicate here rather than adding a
        # cross-package import for one helper call).
        gpu_count_out = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                                         host, "nvidia-smi -L 2>/dev/null | wc -l"],
                                        capture_output=True, text=True, timeout=15)
        try:
            gpu_count = int(gpu_count_out.stdout.strip())
        except ValueError:
            gpu_count = 0
        if gpu_count <= 0:
            result[host] = None
            continue
        cmd = [
            "srun", "-N1", "-w", host, f"--gpus-per-node={gpu_count}",
            f"--container-image={image}", f"--container-mounts={mounts}",
            "python3", bench_script,
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            result[host] = None
            continue
        flagged = set()
        vals = {}
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                d = json.loads(line)
                vals = {int(k): v["tflops"] for k, v in d.items()}
                break
        if vals:
            sorted_vals = sorted(vals.values())
            n = len(sorted_vals)
            median = (sorted_vals[n // 2] if n % 2 else
                      (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2)
            for gpu, tf in vals.items():
                if median > 0 and (median - tf) / median * 100 > margin_pct:
                    flagged.add(gpu)
        result[host] = flagged
    return result
