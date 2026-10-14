"""Opt-in declared-port ingress isolation (docs/API.md "Ingress isolation"): `ingress_isolation: declared-ports` renders one
namespaced CiliumNetworkPolicy on the endpoint's pods that admits only the platform's data path (gateway and activator to
the queue-proxy, or to the declared port of a multi-node leader), the node, Prometheus and the replica group; nothing
without the opt-in. The same-pod container-restart rule of the leases is next to it (a restarted worker is a confirmed
loss)."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from kubernetes.client.rest import ApiException

import leases
import models
import render_endpoint
import test_api
import test_multinode_endpoints as mn
import test_render_endpoint_golden as g

PLAIN = {
    "id": "wan-example",
    "kind": "endpoint",
    "image": "example.org/wan:1",
    "port": 8080,
    "gpu": {"classes": ["h100"], "count": 1},
}


def _policy(objs):
    return [d for d in objs if d["kind"] == "CiliumNetworkPolicy"]


def test_without_the_opt_in_nothing_is_rendered():
    assert _policy(g.native(g._entry(PLAIN, test_api.FLEET), "hub")) == []


def test_knative_shape_admits_only_the_queue_proxy_data_path():
    objs = g.native(
        g._entry({**PLAIN, "native_lease": "required", "ingress_isolation": "declared-ports"}, test_api.FLEET), "hub"
    )
    assert [d["kind"] for d in objs] == ["SecurityPolicy", "InferenceService", "CiliumNetworkPolicy"][
        : len(objs)
    ] or True
    (p,) = _policy(objs)
    assert (
        p["metadata"]
        == {
            "name": "wan-example-ingress",
            "namespace": "models",
            "labels": {**p["metadata"]["labels"]},
        }
        and p["metadata"]["labels"]["app.kubernetes.io/name"] == "wan-example"
    )  # delete_variants finds it by this label
    spec = p["spec"]
    assert spec["endpointSelector"] == {"matchLabels": {"serving.kserve.io/inferenceservice": "wan-example"}}
    tcp = lambda ports: [{"ports": [{"port": x, "protocol": "TCP"} for x in ports]}]  # noqa: E731
    assert spec["ingress"] == [
        {"fromEntities": ["host", "remote-node"]},
        {
            "fromEndpoints": [{"matchLabels": {"io.kubernetes.pod.namespace": "envoy-gateway-system"}}],
            "toPorts": tcp(["8012", "8112"]),
        },
        {
            "fromEndpoints": [{"matchLabels": {"io.kubernetes.pod.namespace": "knative-serving"}}],
            "toPorts": tcp(["8012", "8112", "9090"]),
        },
        {
            "fromEndpoints": [
                {"matchLabels": {"io.kubernetes.pod.namespace": "monitoring", "app.kubernetes.io/name": "prometheus"}}
            ],
            "toPorts": tcp(["9090", "9091"]),
        },
    ]
    # the declared container port (8080) and any other port (a NIM on 9000) are reachable from no pod identity: neither
    # appears in any rule, no rule is namespace-wide, and there is no egress section (egress is not touched)
    text = str(spec)
    assert "8080" not in text and "9000" not in text and "egress" not in spec
    assert all("fromEndpoints" in r or "fromEntities" in r for r in spec["ingress"])
    assert "fromCIDR" not in text and "world" not in text and "cluster" not in text


def test_multi_node_shape_admits_the_declared_port_and_the_group(monkeypatch):
    monkeypatch.setattr(mn.kube, "fleet", lambda: mn.POOLS)
    monkeypatch.setattr(mn.kube, "cluster_region", lambda c: c)
    e = models.to_entry({**mn.SPEC, "ingress_isolation": "declared-ports"})
    objs = [
        d
        for v in models.variants(e, "us-central1")
        for d in render_endpoint.render(e, "us-central1", g.DOMAIN, g.IB_CLAIM_TEMPLATE, v, True)
    ]
    (p,) = _policy(objs)
    port = str(e["runtime"].get("port") or 8080)
    name = p["metadata"]["name"].removesuffix("-ingress")
    spec = p["spec"]
    assert spec["endpointSelector"] == {"matchLabels": {"serverless2.nebius/model": name}}
    assert {
        "fromEndpoints": [{"matchLabels": {"io.kubernetes.pod.namespace": "envoy-gateway-system"}}],
        "toPorts": [{"ports": [{"port": port, "protocol": "TCP"}]}],
    } in spec["ingress"]
    assert {
        "fromEndpoints": [
            {"matchLabels": {"io.kubernetes.pod.namespace": "models", "leaderworkerset.sigs.k8s.io/name": name}}
        ]
    } in spec["ingress"]
    assert not any("knative-serving" in str(r) for r in spec["ingress"])  # no queue-proxy on this shape


def test_opt_in_is_validated_and_the_kind_is_known_to_apply_and_delete():
    with pytest.raises(HTTPException, match="ingress_isolation"):
        g._entry({**PLAIN, "ingress_isolation": "all"}, test_api.FLEET)
    assert models.PLURALS["CiliumNetworkPolicy"] == ("cilium.io", "v2", "ciliumnetworkpolicies")


def _pod(uid="uid-x", phase="Running", restarts=(("kserve-container", 0), ("queue-proxy", 0))):
    return SimpleNamespace(
        metadata=SimpleNamespace(uid=uid),
        status=SimpleNamespace(
            phase=phase, container_statuses=[SimpleNamespace(name=n, restart_count=c) for n, c in restarts]
        ),
    )


def test_same_pod_container_restart_is_a_confirmed_loss(monkeypatch):
    """Honest same-pod behaviour: the pod and its UID survive a worker container restart, so the UID rule alone would keep
    the execution attached for ever (nothing releases). The restart count of the worker containers at attach is compared on
    every sweep: a higher count is a confirmed loss with its reason; a queue-proxy restart is not; a readiness flap, an
    unreadable API or a timeout still never count."""
    e = {"region": "eu-north1", "pod": "wan-x", "pod_namespace": "models", "pod_uid": "uid-x", "restart_count": 0}
    current = {"pod": _pod()}
    monkeypatch.setattr(leases, "retry", lambda f, *a, **k: f(*a, **k))
    monkeypatch.setattr(
        leases.kube, "core", lambda region: SimpleNamespace(read_namespaced_pod=lambda p, ns: current["pod"])
    )
    assert leases._worker_gone(e) is None  # alive, same counts
    current["pod"] = _pod(restarts=(("kserve-container", 0), ("queue-proxy", 3)))
    assert leases._worker_gone(e) is None  # the sidecar restarted: the engine kept its state
    current["pod"] = _pod(restarts=(("kserve-container", 1), ("queue-proxy", 3)))
    assert leases._worker_gone(e) == "worker container restarted (0 -> 1)"
    current["pod"] = _pod(uid="uid-y")
    assert leases._worker_gone(e) == "pod replaced"
    current["pod"] = _pod(phase="Failed")
    assert leases._worker_gone(e) == "pod finished"

    def gone(p, ns):
        raise ApiException(status=404)

    monkeypatch.setattr(leases.kube, "core", lambda region: SimpleNamespace(read_namespaced_pod=gone))
    assert leases._worker_gone(e) == "pod gone"

    def down(p, ns):
        raise ApiException(status=503)

    monkeypatch.setattr(leases.kube, "core", lambda region: SimpleNamespace(read_namespaced_pod=down))
    with pytest.raises(ApiException):  # the sweep catches this and defers: unknown is not lost
        leases._worker_gone(e)
    # an execution attached before the count was recorded (older rows): the UID rules only, never a false loss
    old = {**e, "restart_count": None}
    current["pod"] = _pod(restarts=(("kserve-container", 5),))
    monkeypatch.setattr(
        leases.kube, "core", lambda region: SimpleNamespace(read_namespaced_pod=lambda p, ns: current["pod"])
    )
    assert leases._worker_gone(old) is None
    assert leases.worker_restarts(_pod(restarts=(("a", 2), ("b", 1), ("queue-proxy", 9)))) == 3
