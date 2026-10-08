"""Price feed for the dispatcher: live spot and on-demand prices per fleet pool.

Reads `pools.yaml` of the fleet-prices ConfigMap (charts/fleet renders it from fleet.yaml: region, pool,
project, platform, preset, capacity) and, for each spot or on-demand pool, asks the Nebius price calculator
(`nebius billing v1alpha1 calculator estimate`; `--preemptible-priority 1` returns the current spot price).
Writes `spot.json` = {"<region id>/<pool>": usd_per_gpu_hour} into the spot-prices ConfigMap (created on
first run; not in git, so Argo CD never fights it). Reserved pools are not priced (marginal cost in fleet.yaml).
"""
import json
import logging
import os
import subprocess

import yaml
from kubernetes import client, config

log = logging.getLogger("price-feed")
NS = os.environ.get("PRICES_NAMESPACE", "kueue-system")
FLEET_CM = os.environ.get("FLEET_CONFIGMAP", "fleet-prices")
SPOT_CM = os.environ.get("SPOT_CONFIGMAP", "spot-prices")


def estimate(project: str, platform: str, preset: str, spot: bool):
    cmd = ["nebius", "billing", "v1alpha1", "calculator", "estimate", "--format", "json",
           "--resource-spec-compute-instance-spec-parent-id", project,
           "--resource-spec-compute-instance-spec-resources-platform", platform,
           "--resource-spec-compute-instance-spec-resources-preset", preset]
    if spot:
        cmd += ["--resource-spec-compute-instance-spec-preemptible-priority", "1"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True).stdout
        return float(json.loads(out)["hourly_cost"]["general"]["total"]["cost"])
    except Exception as e:
        log.warning("estimate %s/%s/%s spot=%s failed: %s", project, platform, preset, spot, e)
        return None


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    core = client.CoreV1Api()
    pools = yaml.safe_load((core.read_namespaced_config_map(FLEET_CM, NS).data or {}).get("pools.yaml", "") or "") or {}
    prices = {}
    for _, p in pools.items():
        if p.get("capacity") == "reserved" or not all(k in p for k in ("project", "platform", "preset")):
            continue
        gpus = int(p.get("gpus_per_node", 1)) or 1
        usd = estimate(str(p["project"]), p["platform"], p["preset"], p.get("capacity") == "spot")
        if usd is not None:
            prices[f"{p['region']}/{p['pool']}"] = round(usd / gpus, 4)  # per GPU-hour
    body = {"metadata": {"name": SPOT_CM}, "data": {"spot.json": json.dumps(prices, sort_keys=True)}}
    try:
        core.patch_namespaced_config_map(SPOT_CM, NS, body)
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise
        core.create_namespaced_config_map(NS, body)
    log.info("wrote %d prices: %s", len(prices), prices)


if __name__ == "__main__":
    main()
