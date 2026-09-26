#!/usr/bin/env python3
"""Step 4 -- wire the suspect rank into EXISTING telemetry checks (no new
telemetry-gathering built here). Discovers the suspect rank's real host +
gpu_slot_index directly from its own dump-file metadata (the same real
field per_rank_series_with_ts/discover_rank_hosts already read elsewhere
in this project), then calls the project's own DCGM (Path B live query),
host-contention, and NVLink/network checks against exactly that host
(and that GPU index for DCGM)."""
import glob
import json
import sys

sys.path.insert(0, '/root/P18k_classifier')
from cause_metrics import query_dcgm_all_gpus, query_nvlink_snapshot, query_network_snapshot
from classifier import live_host_load_ratios


def discover_suspect_identity(dump_dirs, suspect_rank):
    """Real host + gpu_slot_index for suspect_rank, read directly from its
    own dump file's real metadata (break on first record -- constant per
    file, same convention discover_rank_hosts already uses elsewhere)."""
    for d in dump_dirs:
        for fp in glob.glob(d + "/*.log"):
            with open(fp) as f:
                first_line = f.readline()
            if not first_line.strip():
                continue
            rec = json.loads(first_line)
            if rec["header"]["rank"] == suspect_rank:
                return rec["metadata"]["hostname"], rec["metadata"]["gpu_slot_index"]
    return None, None


def run_telemetry_check(dump_dirs, suspect_rank, all_cluster_hosts=("worker-0", "worker-1")):
    host, gpu_slot = discover_suspect_identity(dump_dirs, suspect_rank)
    if host is None:
        return {"error": f"rank {suspect_rank} not found in any dump file -- cannot discover real host/gpu_slot_index"}

    result = {"suspect_rank": suspect_rank, "host": host, "gpu_slot_index": gpu_slot}

    dcgm_all, dcgm_err = query_dcgm_all_gpus(host)
    if dcgm_err:
        result["dcgm"] = {"error": dcgm_err}
    else:
        result["dcgm"] = dcgm_all.get(gpu_slot, {"error": f"gpu_slot_index {gpu_slot} not in dcgm_all_gpus result"})

    try:
        # needs >=2 real hosts to compute a ratio (this-host load vs the
        # OTHER hosts' mean) -- pass the whole real cluster, not just the
        # suspect's own host, then pick its own entry out of the result.
        ratios = live_host_load_ratios(list(all_cluster_hosts))
        result["host_load_ratio"] = ratios.get(host, {"note": "host not in ratios result (query may have failed or host had no other real peer data)"})
    except Exception as e:
        result["host_load_ratio"] = {"error": f"{type(e).__name__}: {e}"}

    try:
        result["nvlink"] = query_nvlink_snapshot([host])
    except Exception as e:
        result["nvlink"] = {"error": f"{type(e).__name__}: {e}"}

    try:
        result["network"] = query_network_snapshot([host])
    except Exception as e:
        result["network"] = {"error": f"{type(e).__name__}: {e}"}

    return result


if __name__ == "__main__":
    suspect_rank = int(sys.argv[1])
    dump_dirs = sys.argv[2:]
    result = run_telemetry_check(dump_dirs, suspect_rank)
    print(json.dumps(result, indent=2, default=str))
