"""Kubernetes clients per region (in-cluster for this region, rotated kubeconfigs for the others)
and the KServe read helpers the catalog uses."""
import os, time
from fastapi import HTTPException
from kubernetes import client, config as kconfig
from kubernetes.client.rest import ApiException
from config import FLEET_CONFIGMAP, HUB_REGION, KUEUE_JOB_UID_LABEL, KUEUE_NAMESPACE, LABEL, REGION, REGION_KUBECONFIGS
from resilience import retry

_clients: dict[str, client.ApiClient] = {}
_kubeconfig_mtime: dict[str, float] = {}      # region -> mtime of the kubeconfig the client was built from


def regions() -> list[str]:
    """Regions this API can reach: its own cluster plus every mounted kubeconfig."""
    return [REGION] + [r for r, path in REGION_KUBECONFIGS.items() if os.path.exists(path)]


def _mtime(path: str) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def _client(region: str) -> client.ApiClient:
    if region == REGION:
        if region not in _clients:
            try:
                kconfig.load_incluster_config()
            except Exception:
                kconfig.load_kube_config()
            _clients[region] = client.ApiClient()
        return _clients[region]
    path = REGION_KUBECONFIGS.get(region)
    if not path or not os.path.exists(path):
        raise HTTPException(400, f"region {region} is not served by this API (available: {regions()})")
    # the kubeconfig Secret is rotated daily (48 h tokens, ops/rotate-api-agent-token): rebuild the
    # client whenever the mounted file changed, so no restart is needed after a rotation
    mtime = _mtime(path)
    if region not in _clients or _kubeconfig_mtime.get(region) != mtime:
        _clients[region] = kconfig.new_client_from_config(path)
        _kubeconfig_mtime[region] = mtime
    return _clients[region]


def api(region: str = REGION) -> client.CustomObjectsApi:
    return client.CustomObjectsApi(_client(region))


def core(region: str = REGION) -> client.CoreV1Api:
    return client.CoreV1Api(_client(region))


def batch(region: str = REGION) -> client.BatchV1Api:
    return client.BatchV1Api(_client(region))


_fleet_cache: dict = {"until": 0.0, "pools": {}}


def fleet() -> dict:
    """The fleet's pools from the fleet-prices ConfigMap charts/fleet renders in kueue-system (every region on
    the control cluster, this region's on a worker): {"pools": {<name>: {region, pool, gpu_class, ...}}}.
    Empty when unreadable (then no class affinity)."""
    if _fleet_cache["until"] > time.time():
        return _fleet_cache
    try:
        import yaml
        data = retry(core().read_namespaced_config_map, FLEET_CONFIGMAP, KUEUE_NAMESPACE).data or {}
        _fleet_cache["pools"] = yaml.safe_load(data.get("pools.yaml") or "") or {}
    except Exception:  # noqa: BLE001 - unreadable fleet data is not fatal for a submission
        pass
    _fleet_cache["until"] = time.time() + 60
    return _fleet_cache


def class_pools(classes: list[str], gpus_per_node: int | None = None, interconnect: str | None = None) -> list[str]:
    """Pool names (fleet-wide) whose GPU class is one of `classes`: the nodeAffinity that keeps a
    preference queue from falling back to a GPU the image does not run on. Multi-node runs add two
    filters: `gpus_per_node` (a pod takes a whole node: only pools whose preset has that many GPUs) and
    `interconnect` (`infiniband`: only pools in a GPU cluster, docs/JOBS.md "Multi-node runs")."""
    out = set()
    for p in fleet()["pools"].values():
        if p.get("gpu_class") not in classes:
            continue
        if gpus_per_node and int(p.get("gpus_per_node") or 0) != int(gpus_per_node):
            continue
        if interconnect and (p.get("interconnect") or "none") != interconnect:
            continue
        out.add(p["pool"])
    return sorted(out)


def ib_devices_per_node(pools: list[str]) -> int:
    """InfiniBand NICs a pod claims per node on these pools (fleet-prices `ib_devices_per_node`, charts/fleet
    `fleet.ibDevices`: 8 on H100/H200/B200/B300 full nodes, 4 on GB300); 8 when unknown."""
    counts = {int(p.get("ib_devices_per_node") or 0) for p in fleet()["pools"].values() if p.get("pool") in pools}
    counts.discard(0)
    return max(counts) if counts else 8


def cluster_region(cluster: str) -> str:
    """MultiKueueCluster name (fleet.yaml region id: `hub`, `eu-south1`) -> region name the API uses
    (`eu-north1`, `eu-south1`). The hub is the one id that differs from its region."""
    return HUB_REGION if cluster == "hub" else cluster


def deactivate_workload(ns: str, job_uid: str, region: str = REGION) -> int:
    """Kueue `spec.active = false` on the manager Workload of a run (label kueue.x-k8s.io/job-uid): Kueue evicts the
    run everywhere (the worker's copy goes, its pods with it) and never admits it again. A `suspend` patched onto
    the worker's JobSet alone is undone within seconds: Kueue keeps an admitted workload's JobSet running, and the
    JobSet controller then restarts every pod (s2pr2, 2026-10-09: a cancelled 16-GPU run came back on fresh pods).
    `region` is the cluster that holds the Workload: the manager for a dispatched run, the worker for a run pinned
    to a region (its Kueue admits the JobSet directly and would undo a suspend the same way).
    Returns the number of Workloads patched (0: none found, the run never reached the queue)."""
    try:
        items = retry(api(region).list_namespaced_custom_object, "kueue.x-k8s.io", "v1beta2", ns, "workloads",
                      label_selector=f"{KUEUE_JOB_UID_LABEL}={job_uid}").get("items", [])
    except ApiException:
        return 0
    for w in items:
        retry(api(region).patch_namespaced_custom_object, "kueue.x-k8s.io", "v1beta2", ns, "workloads", w["metadata"]["name"],
              {"spec": {"active": False}})
    return len(items)


def workload_clusters(ns: str) -> dict[str, str]:
    """Job uid -> worker cluster that ADMITTED it, for the manager's Jobs in a tenant namespace: the Workload's
    status.clusterName while the worker's copy exists, afterwards the record the dispatcher wrote on the Workload
    (`serverless2.nebius/admitted-cluster`); absent while the run is only nominated (it may still move)."""
    try:
        items = retry(api().list_namespaced_custom_object, "kueue.x-k8s.io", "v1beta2", ns, "workloads").get("items", [])
    except ApiException:
        return {}
    out = {}
    for w in items:
        uid = (w.get("metadata", {}).get("labels") or {}).get(KUEUE_JOB_UID_LABEL)
        st = w.get("status") or {}
        placed = st.get("clusterName") or (w.get("metadata", {}).get("annotations") or {}).get(f"{LABEL}/admitted-cluster")
        if uid and placed:
            out[uid] = placed
    return out


_isvc_cache: dict[str, tuple[float, dict]] = {}


def invalidate_endpoint(name: str, ns: str, region: str = REGION) -> None:
    _isvc_cache.pop(f"{region}/{ns}/{name}", None)


def isvc(name: str, ns: str, region: str = REGION) -> dict | None:
    try:
        return retry(api(region).get_namespaced_custom_object, "serving.kserve.io", "v1beta1", ns, "inferenceservices", name)
    except ApiException as e:
        if e.status == 404:
            return None
        raise HTTPException(502, f"kserve: {e.reason}")


def endpoint_status(name: str, ns: str, region: str = REGION) -> dict:
    """Live KServe state for the catalog: ready / scaled-to-zero / deploying / unavailable."""
    hit = _isvc_cache.get(f"{region}/{ns}/{name}")
    if hit and hit[0] > time.time():
        return hit[1]
    out = {"status": "unavailable", "replicas_ready": None}
    try:
        i = retry(api(region).get_namespaced_custom_object, "serving.kserve.io", "v1beta1", ns, "inferenceservices", name)
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in i.get("status", {}).get("conditions", []))
        pods = retry(core(region).list_namespaced_pod, ns, label_selector=f"serving.kserve.io/inferenceservice={name}").items
        n = sum(1 for p in pods if p.status.phase == "Running" and all(c.ready for c in (p.status.container_statuses or [])))
        out = {"status": "ready" if n else ("scaled-to-zero" if ready and not pods else "deploying"), "replicas_ready": n}
    except ApiException:
        pass
    _isvc_cache[f"{region}/{ns}/{name}"] = (time.time() + 10, out)
    return out
