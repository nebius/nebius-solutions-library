"""Kubernetes clients per region (in-cluster for this region, rotated kubeconfigs for the others)
and the KServe read helpers the catalog uses."""

import os
import time

from fastapi import HTTPException
from kubernetes import client
from kubernetes import config as kconfig
from kubernetes.client.rest import ApiException

from config import FLEET_CONFIGMAP, HUB_REGION, KUEUE_JOB_UID_LABEL, KUEUE_NAMESPACE, LABEL, REGION, REGION_KUBECONFIGS
from resilience import retry

_clients: dict[str, client.ApiClient] = {}
_kubeconfig_mtime: dict[str, float] = {}  # region -> mtime of the kubeconfig the client was built from


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
    # the kubeconfig Secret is rotated daily (48 h tokens, re-applied by the platform stage): rebuild the
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


_fleet_cache: dict = {"until": 0.0, "pools": {}, "clusters": {}, "tenants": {}, "assets": {}, "fit": {}}


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
        _fleet_cache["tenants"] = yaml.safe_load(data.get("tenants.yaml") or "") or {}
        _fleet_cache["clusters"] = yaml.safe_load(data.get("clusters.yaml") or "") or {}
        _fleet_cache["assets"] = yaml.safe_load(data.get("assets.yaml") or "") or {}  # docs/JOBS.md "Restricted assets"
        _fleet_cache["fit"] = yaml.safe_load(data.get("fit.yaml") or "") or {}  # docs/SCHEDULING.md "Per-node fit"
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
    """Cluster id (MultiKueueCluster name, the pools' `region`, catalog deployment keys; `hub`, `north`, `eu-south1`, ...)
    -> the Nebius region the API uses for kubeconfigs, gateway domains and prices. The map is the fleet document's
    `clusters.yaml` (charts/fleet, from the tfvars ids); before it exists only the hub alias was known, which is kept
    as the fallback, and an id that is its own region name maps to itself."""
    known = (fleet().get("clusters") or {}).get(cluster) or {}
    if known.get("region"):
        return known["region"]
    return HUB_REGION if cluster == "hub" else cluster


def cluster_id(region: str | None) -> str | None:
    """Nebius region -> the id of the worker cluster in it (the fleet document's clusters.yaml, control entries left
    out): the canonical cluster name on attempt records and operations, as placement, identities and the application
    key them. An id equal to the region wins; without the map the legacy `hub` alias, else the region itself."""
    if not region:
        return None
    known = {
        cid: c
        for cid, c in (fleet().get("clusters") or {}).items()
        if (c or {}).get("region") == region and not (c or {}).get("control")
    }
    if region in known:
        return region
    if len(known) == 1:
        return next(iter(known))
    if known:  # two worker clusters in one region: never guess an identity, the caller must know the cluster
        raise LookupError(
            f"ambiguous worker cluster for region {region}: {sorted(known)}; the Workload's admitting cluster is required"
        )
    return "hub" if region == HUB_REGION and region != REGION else region


def cluster_id_or_none(region: str | None) -> str | None:
    """cluster_id for status projections: unknown (None) rather than an error when the map is ambiguous."""
    try:
        return cluster_id(region)
    except LookupError:
        return None


def workload_inactive(ns: str, job_uid: str, region: str = REGION) -> bool:
    """Whether nothing of a run can run any more as far as Kueue is concerned: its Workload (label job-uid) is gone,
    deactivated (`spec.active = false`, deactivate_workload) or finished (condition Finished=True). An unreadable
    answer is not an absence (False)."""
    try:
        items = retry(
            api(region).list_namespaced_custom_object,
            "kueue.x-k8s.io",
            "v1beta2",
            ns,
            "workloads",
            label_selector=f"{KUEUE_JOB_UID_LABEL}={job_uid}",
        ).get("items", [])
    except ApiException:
        return False
    if not items:
        return True
    for w in items:
        if (w.get("spec") or {}).get("active") is False:
            continue
        if any(
            c.get("type") == "Finished" and c.get("status") == "True"
            for c in ((w.get("status") or {}).get("conditions") or [])
        ):
            continue
        return False
    return True


def deactivate_workload(ns: str, job_uid: str, region: str = REGION) -> int:
    """Kueue `spec.active = false` on the manager Workload of a run (label kueue.x-k8s.io/job-uid): Kueue evicts the
    run everywhere (the worker's copy goes, its pods with it) and never admits it again. A `suspend` patched onto
    the worker's JobSet alone is undone within seconds: Kueue keeps an admitted workload's JobSet running, and the
    JobSet controller then restarts every pod (a cancelled 16-GPU run came back on fresh pods).
    `region` is the cluster that holds the Workload: the manager for a dispatched run, the worker for a run pinned
    to a region (its Kueue admits the JobSet directly and would undo a suspend the same way).
    Returns the number of Workloads patched (0: none found, the run never reached the queue)."""
    try:
        items = retry(
            api(region).list_namespaced_custom_object,
            "kueue.x-k8s.io",
            "v1beta2",
            ns,
            "workloads",
            label_selector=f"{KUEUE_JOB_UID_LABEL}={job_uid}",
        ).get("items", [])
    except ApiException:
        return 0
    for w in items:
        retry(
            api(region).patch_namespaced_custom_object,
            "kueue.x-k8s.io",
            "v1beta2",
            ns,
            "workloads",
            w["metadata"]["name"],
            {"spec": {"active": False}},
        )
    return len(items)


def activate_workload(ns: str, job_uid: str, region: str = REGION) -> int:
    """Undo deactivate_workload: Kueue `spec.active = true` on the run's manager Workload (a fallback whose successor could
    not be created lets the original queue on). Returns the number of Workloads patched."""
    try:
        items = retry(
            api(region).list_namespaced_custom_object,
            "kueue.x-k8s.io",
            "v1beta2",
            ns,
            "workloads",
            label_selector=f"{KUEUE_JOB_UID_LABEL}={job_uid}",
        ).get("items", [])
    except ApiException:
        return 0
    for w in items:
        retry(
            api(region).patch_namespaced_custom_object,
            "kueue.x-k8s.io",
            "v1beta2",
            ns,
            "workloads",
            w["metadata"]["name"],
            {"spec": {"active": True}},
        )
    return len(items)


def workload_reasons(ns: str) -> dict[str, str]:
    """Job uid -> why its Workload is not admitted (the condition messages Kueue writes): what a QUEUED operation
    reports as `reason`."""
    try:
        items = retry(api().list_namespaced_custom_object, "kueue.x-k8s.io", "v1beta2", ns, "workloads").get(
            "items", []
        )
    except ApiException:
        return {}
    out = {}
    for w in items:
        uid = (w.get("metadata", {}).get("labels") or {}).get(KUEUE_JOB_UID_LABEL)
        conds = {c.get("type"): c for c in ((w.get("status") or {}).get("conditions") or [])}
        if not uid or (conds.get("Admitted") or {}).get("status") == "True":
            continue
        for t in ("Evicted", "QuotaReserved", "Admitted", "PodsReady"):
            c = conds.get(t)
            if c and c.get("status") != "True" and c.get("message"):
                out[uid] = c["message"][:300]
                break
    return out


def workload_clusters(ns: str) -> dict[str, str]:
    """Job uid -> worker cluster that ADMITTED it, for the manager's Jobs in a tenant namespace: the Workload's
    status.clusterName while the worker's copy exists, afterwards the record the dispatcher wrote on the Workload
    (`serverless2.nebius/admitted-cluster`); absent while the run is only nominated (it may still move)."""
    try:
        items = retry(api().list_namespaced_custom_object, "kueue.x-k8s.io", "v1beta2", ns, "workloads").get(
            "items", []
        )
    except ApiException:
        return {}
    out = {}
    for w in items:
        uid = (w.get("metadata", {}).get("labels") or {}).get(KUEUE_JOB_UID_LABEL)
        st = w.get("status") or {}
        placed = st.get("clusterName") or (w.get("metadata", {}).get("annotations") or {}).get(
            f"{LABEL}/admitted-cluster"
        )
        if uid and placed:
            out[uid] = placed
    return out


_isvc_cache: dict[str, tuple[float, dict]] = {}


def isvc(name: str, ns: str, region: str = REGION) -> dict | None:
    try:
        return retry(
            api(region).get_namespaced_custom_object, "serving.kserve.io", "v1beta1", ns, "inferenceservices", name
        )
    except ApiException as e:
        if e.status == 404:
            return None
        raise HTTPException(502, f"kserve: {e.reason}")


LWS = ("leaderworkerset.x-k8s.io", "v1", "leaderworkersets")
LAST_REQUEST_ANNOTATION = f"{LABEL}/last-request-at"


def _lws_get(region: str, ns: str, name: str) -> dict:
    """One LeaderWorkerSet; the client wants (group, version, namespace, plural, name)."""
    return retry(api(region).get_namespaced_custom_object, LWS[0], LWS[1], ns, LWS[2], name)


def lws(name: str, ns: str, region: str = REGION) -> dict | None:
    """The LeaderWorkerSet of a multi-node endpoint (services/api/models.py `nodes`), None when absent."""
    try:
        return _lws_get(region, ns, name)
    except ApiException as e:
        if e.status == 404:
            return None
        raise HTTPException(502, f"leaderworkerset: {e.reason}")


def _startup(pods: list) -> dict:
    """Start-up timing of the newest pod of an endpoint: created -> Ready (the node's boot, driver, image pull and
    model load are all inside it). Absent until a pod is Ready."""
    pods = [p for p in pods if getattr(getattr(p, "metadata", None), "creation_timestamp", None)]
    newest = max(pods, key=lambda p: p.metadata.creation_timestamp) if pods else None
    if not newest:
        return {}
    ready = next((c for c in (newest.status.conditions or []) if c.type == "Ready" and c.status == "True"), None)
    out = {"started_at": newest.metadata.creation_timestamp.isoformat().replace("+00:00", "Z")}
    if ready and ready.last_transition_time:
        out["ready_at"] = ready.last_transition_time.isoformat().replace("+00:00", "Z")
        out["startup_s"] = round((ready.last_transition_time - newest.metadata.creation_timestamp).total_seconds())
    return out


def _rollout(isvc: dict, pods: list, region: str = REGION, ns: str = "models") -> dict:
    """The revision state of a KServe endpoint (docs/API.md "Execution identity and rollout state"). Knative routes
    to the latest *ready* revision, so after a spec change the previous revision serves until the new one is ready.
    `converged` holds only when every one of these is observed, never from revision names alone: the controller has
    reconciled the current spec (`status.observedGeneration == metadata.generation`: the pre-reconcile window after a
    catalog change still shows the old revisions as one); the created, ready and rolled-out revisions are one; the
    latest ready Revision object's container image equals the InferenceService's desired image (stored template
    evidence: valid for a scaled-to-zero immutable revision with no pod); and every ready pod runs that image (by
    reference, or by the Revision's resolved digest). `serving_images`: what the ready pods run."""
    md, st = isvc.get("metadata") or {}, isvc.get("status") or {}
    pred = (st.get("components") or {}).get("predictor") or {}
    created, ready_rev, rolled = (
        pred.get("latestCreatedRevision"),
        pred.get("latestReadyRevision"),
        pred.get("latestRolledoutRevision"),
    )
    generation, observed = md.get("generation"), st.get("observedGeneration")
    desired = _desired_image(isvc)
    revision_image, revision_digest = None, None
    if ready_rev:
        try:
            rev = retry(
                api(region).get_namespaced_custom_object, "serving.knative.dev", "v1", ns, "revisions", ready_rev
            )
            containers = (rev.get("spec") or {}).get("containers") or [{}]
            revision_image = containers[0].get("image")
            statuses = (rev.get("status") or {}).get("containerStatuses") or [{}]
            revision_digest = statuses[0].get("imageDigest")
        except ApiException:
            pass
    images: list[str] = []
    for p in pods:
        pst = getattr(p, "status", None)
        if not pst or pst.phase != "Running" or not all(c.ready for c in (pst.container_statuses or [])):
            continue
        for c in pst.container_statuses or []:
            if getattr(c, "name", None) in ("kserve-container", "main") or len(pst.container_statuses) == 1:
                ref = getattr(c, "image_id", None) or getattr(c, "image", None)
                if ref and ref not in images:
                    images.append(ref)
    # all three must be present and one: KServe omits latestRolledoutRevision until the first rollout completed, and
    # "absent" is then "not converged", never "the other two agree"
    revisions_one = bool(created) and bool(ready_rev) and bool(rolled) and created == ready_rev == rolled
    reconciled = generation is not None and observed == generation
    image_matches = bool(desired) and revision_image == desired
    pods_match = all(_same_image(i, revision_image, revision_digest) for i in images)
    reasons = []
    if not reconciled:
        reasons.append(f"spec generation {generation} not reconciled (observed {observed})")
    if not revisions_one:
        reasons.append(f"revisions differ (created {created}, ready {ready_rev}, rolled out {rolled})")
    if not image_matches:
        reasons.append(f"ready revision image {revision_image} is not the desired {desired}")
    if not pods_match:
        reasons.append("a ready pod runs another image than the ready revision")
    return {
        "latest_created": created,
        "latest_ready": ready_rev,
        "latest_rolled_out": rolled,
        "generation": generation,
        "observed_generation": observed,
        "desired_image": desired,
        "revision_image": revision_image,
        "revision_image_digest": revision_digest,
        "serving_images": images,
        "converged": reconciled and revisions_one and image_matches and pods_match,
        "reason": "; ".join(reasons) or None,
    }


def _desired_image(isvc: dict) -> str | None:
    """The image the InferenceService's spec asks for: the predictor's first container (as the API renders it)."""
    pred = (isvc.get("spec") or {}).get("predictor") or {}
    containers = pred.get("containers") or []
    if containers and containers[0].get("image"):
        return containers[0]["image"]
    model = pred.get("model") or {}
    return model.get("image") or None


def _same_image(ref: str, image: str | None, digest: str | None) -> bool:
    """A ready pod's image reference (`image_id` with the registry's digest, or the tag reference) against the
    Revision's image and resolved digest: equal by digest when either side carries one, else by reference."""
    if not ref or not (image or digest):
        return False
    tails = {x.split("@", 1)[1] for x in (ref, image or "", digest or "") if "@" in x}
    if "@" in ref and (
        digest and digest.split("@", 1)[-1] in ref or (image and "@" in image and tails and len(tails) == 1)
    ):
        return True
    if digest and ref.endswith(digest.split("@", 1)[-1]):
        return True
    return ref == image


def endpoint_status(name: str, ns: str, region: str = REGION, nodes: int = 1) -> dict:
    """Live state for the catalog: ready / scaled-to-zero / deploying / unavailable, the ready replicas and the
    start-up timing of the newest pod. KServe InferenceService (one pod per replica) or, for `nodes` > 1, the
    LeaderWorkerSet whose leader pods serve (a replica is ready when its leader is)."""
    hit = _isvc_cache.get(f"{region}/{ns}/{name}")
    if hit and hit[0] > time.time():
        return hit[1]
    out = {"status": "unavailable", "replicas_ready": None}
    try:
        if nodes > 1:
            i = _lws_get(region, ns, name)
            pods = retry(
                core(region).list_namespaced_pod, ns, label_selector=f"leaderworkerset.sigs.k8s.io/name={name}"
            ).items
            leaders = [p for p in pods if p.metadata.labels.get("leaderworkerset.sigs.k8s.io/worker-index") == "0"]
            n = sum(
                1
                for p in leaders
                if p.status.phase == "Running" and all(c.ready for c in (p.status.container_statuses or []))
            )
            stopped = int(i.get("spec", {}).get("replicas", 0)) == 0
            out = {
                "status": "ready" if n else ("scaled-to-zero" if stopped and not pods else "deploying"),
                "replicas_ready": n,
                "nodes": nodes,
                "pods_ready": sum(
                    1
                    for p in pods
                    if p.status.phase == "Running" and all(c.ready for c in (p.status.container_statuses or []))
                ),
                **_startup(leaders),
            }
            if last := (i.get("metadata", {}).get("annotations") or {}).get(LAST_REQUEST_ANNOTATION):
                out["last_request_at"] = last
        else:
            i = retry(
                api(region).get_namespaced_custom_object, "serving.kserve.io", "v1beta1", ns, "inferenceservices", name
            )
            conds = i.get("status", {}).get("conditions", [])
            ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conds)
            pods = retry(
                core(region).list_namespaced_pod, ns, label_selector=f"serving.kserve.io/inferenceservice={name}"
            ).items
            n = sum(
                1
                for p in pods
                if p.status.phase == "Running" and all(c.ready for c in (p.status.container_statuses or []))
            )
            # Knative gave up on the newest revision (ProgressDeadlineExceeded -> RevisionFailed): nothing is starting any
            # more, so this is an error with Knative's message, not "deploying" (a Boltz-2 NIM whose Secret was gone sat in
            # "deploying" for two days with no pod, no logs and no metrics)
            failed = next(
                (
                    c
                    for c in conds
                    if c.get("type") == "Ready" and c.get("status") == "False" and "Failed" in (c.get("reason") or "")
                ),
                None,
            )
            if n:
                status = "ready"
            elif failed and not pods:
                status = "error"
            else:
                status = "scaled-to-zero" if ready and not pods else "deploying"
            out = {"status": status, "replicas_ready": n, **_startup(pods), "rollout": _rollout(i, pods, region, ns)}
            if status == "error":
                out["reason"] = f"{failed.get('reason')}: {(failed.get('message') or '').strip()}"[:300]
    except ApiException:
        pass
    _isvc_cache[f"{region}/{ns}/{name}"] = (time.time() + 10, out)
    return out


_last_stamp: dict[str, float] = {}


def stamp_last_request(name: str, ns: str = "models", region: str = REGION, min_interval_s: int = 60) -> None:
    """Record on a multi-node endpoint's LeaderWorkerSet that it was called now (annotation, at most once a minute):
    the control API's idle shutdown reads it (services/api/app.py idle_shutdown). Cheap and best-effort."""
    now = time.time()
    if _last_stamp.get(f"{region}/{name}", 0) + min_interval_s > now:
        return
    _last_stamp[f"{region}/{name}"] = now
    body = {
        "metadata": {"annotations": {LAST_REQUEST_ANNOTATION: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}}
    }
    try:
        api(region).patch_namespaced_custom_object(LWS[0], LWS[1], ns, LWS[2], name, body)
    except Exception:  # noqa: BLE001 - a missed stamp delays an idle shutdown by a minute; never a failed request
        pass


def pool_usage(cluster: str, pool: str) -> dict:
    """Ready nodes of a pool and the GPUs their pods hold (the fleet page): nodes by the pool label, GPU requests of
    the running pods on them (every namespace this identity may list; a tenant's pods are its runs, `models` the
    endpoints and the warm-spare placeholders). Unreachable: {"nodes_ready": None}."""
    region = cluster_region(cluster)
    try:
        nodes = retry(core(region).list_node, label_selector=f"{LABEL}/pool={pool}").items
    except Exception:  # noqa: BLE001 - a region that cannot be reached shows as unknown, never fails the page
        return {"nodes_ready": None, "gpus_total": None, "gpus_used": None}
    names = {n.metadata.name for n in nodes}
    ready = sum(1 for n in nodes if any(c.type == "Ready" and c.status == "True" for c in (n.status.conditions or [])))
    total = sum(int((n.status.allocatable or {}).get("nvidia.com/gpu", 0)) for n in nodes)
    used = 0
    try:
        for pod in retry(core(region).list_pod_for_all_namespaces, field_selector="status.phase=Running").items:
            if pod.spec.node_name in names:
                used += sum(
                    int((c.resources.requests or {}).get("nvidia.com/gpu", 0))
                    for c in pod.spec.containers
                    if c.resources
                )
    except Exception:  # noqa: BLE001 - without cluster-wide pod read the use stays unknown
        used = None
    return {"nodes_ready": ready, "gpus_total": total, "gpus_used": used}


def tenant_usage(tenant: str, region: str = REGION) -> dict:
    """What the tenant's Kueue queues hold on a cluster (charts/fleet `<tenant>-<profile>` ClusterQueues): GPUs in use per
    pool (flavor), admitted and pending workloads. Unreachable: empty."""
    out = {"gpus_by_pool": {}, "admitted": 0, "pending": 0}
    try:
        items = retry(api(region).list_cluster_custom_object, "kueue.x-k8s.io", "v1beta2", "clusterqueues").get(
            "items", []
        )
    except Exception:  # noqa: BLE001 - a region that cannot be reached shows as empty
        return out
    for q in items:
        if not q["metadata"]["name"].startswith(f"{tenant}-"):
            continue
        st = q.get("status") or {}
        out["admitted"] += int(st.get("admittedWorkloads") or 0)
        out["pending"] += int(st.get("pendingWorkloads") or 0)
        for fu in st.get("flavorsUsage") or []:
            for r in fu.get("resources") or []:
                if r.get("name") == "nvidia.com/gpu":
                    out["gpus_by_pool"][fu["name"]] = out["gpus_by_pool"].get(fu["name"], 0) + int(
                        float(str(r.get("total") or 0))
                    )
    return out


def cluster_summary(region: str) -> dict:
    """One cluster of the fleet for the fleet page: Kubernetes version, ready nodes (system and GPU), reachability."""
    try:
        nodes = retry(core(region).list_node).items
    except Exception:  # noqa: BLE001 - an unreachable cluster shows as such
        return {"reachable": False, "nodes_ready": None, "gpu_nodes_ready": None, "kubernetes_version": None}
    ready = [n for n in nodes if any(c.type == "Ready" and c.status == "True" for c in (n.status.conditions or []))]
    gpu = [
        n
        for n in ready
        if (n.metadata.labels or {}).get(f"{LABEL}/pool") and (n.metadata.labels or {}).get(f"{LABEL}/pool") != "system"
    ]
    version = next((n.status.node_info.kubelet_version for n in nodes if n.status and n.status.node_info), None)
    return {"reachable": True, "nodes_ready": len(ready), "gpu_nodes_ready": len(gpu), "kubernetes_version": version}
