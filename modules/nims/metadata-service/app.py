#!/usr/bin/env python3
"""
NIM Metadata Service - Provides information about running NIMs in the cluster.
"""

import json
import os
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from kubernetes import client, config
from pydantic import BaseModel

app = FastAPI(
    title="NIM Metadata Service",
    description="Provides information about NVIDIA NIM deployments running in the cluster",
    version="1.0.0",
    openapi_tags=[
        {"name": "nims", "description": "NIM deployment information"},
        {"name": "gpus", "description": "GPU metrics and information"},
        {"name": "health", "description": "Service health checks"},
    ]
)

# Prometheus configuration
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus-server.o11y.svc.cluster.local:80")
# Time window for peak memory calculation (default 7 days)
PEAK_MEMORY_TIME_WINDOW = os.getenv("PEAK_MEMORY_TIME_WINDOW", "7d")


def load_nim_ports() -> dict[str, int]:
    """Load Terraform-derived proxy ports."""
    raw_ports = os.getenv("NIM_PORTS_JSON", "{}")
    try:
        return {str(name): int(port) for name, port in json.loads(raw_ports).items()}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


NIM_PORTS = load_nim_ports()


class ResourceInfo(BaseModel):
    cpu_request: Optional[str] = None
    cpu_limit: Optional[str] = None
    memory_request: Optional[str] = None
    memory_limit: Optional[str] = None
    gpu_count: Optional[int] = None


class PodInfo(BaseModel):
    name: str
    status: str
    node: Optional[str] = None
    gpu_index: Optional[str] = None
    restart_count: int = 0

class NIMInfo(BaseModel):
    name: str
    image: str
    replicas_desired: int
    replicas_ready: int
    replicas_available: int
    port: Optional[int] = None
    resources: ResourceInfo
    pods: list[PodInfo]

class NIMSummary(BaseModel):
    total_nims: int
    running_nims: int
    total_gpus_requested: int
    nims: list[NIMInfo]

class HealthResponse(BaseModel):
    status: str
    namespace: str


class GPUMetrics(BaseModel):
    gpu_index: int
    node: str
    gpu_uuid: Optional[str] = None
    gpu_name: Optional[str] = None
    utilization_percent: Optional[float] = None
    memory_used_bytes: Optional[int] = None
    memory_free_bytes: Optional[int] = None
    memory_total_bytes: Optional[int] = None
    peak_memory_used_bytes: Optional[int] = None
    temperature_celsius: Optional[float] = None
    power_usage_watts: Optional[float] = None


class GPUSummary(BaseModel):
    total_gpus: int
    gpus: list[GPUMetrics]


class PodGPUMetrics(BaseModel):
    pod_name: str
    node: str
    gpu_metrics: list[GPUMetrics]


class NIMGPUMetrics(BaseModel):
    name: str
    namespace: str
    pods: list[PodGPUMetrics]


def query_prometheus(query: str) -> dict:
    """Query Prometheus and return the result."""
    try:
        response = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        raise HTTPException(status_code=503, detail=f"Prometheus query failed: {str(e)}")


def get_gpu_metrics_from_prometheus() -> dict[tuple[str, int], GPUMetrics]:
    """Fetch GPU metrics from Prometheus and return a dict keyed by (node, gpu_index)."""
    metrics_map = {}

    # Define the DCGM metrics we want to query
    dcgm_queries = {
        "utilization": "DCGM_FI_DEV_GPU_UTIL",
        "memory_used": "DCGM_FI_DEV_FB_USED",
        "memory_free": "DCGM_FI_DEV_FB_FREE",
        "peak_memory": f"max_over_time(DCGM_FI_DEV_FB_USED[{PEAK_MEMORY_TIME_WINDOW}])",
        "temperature": "DCGM_FI_DEV_GPU_TEMP",
        "power": "DCGM_FI_DEV_POWER_USAGE",
    }

    # First, get the list of GPUs with their basic info
    try:
        result = query_prometheus("DCGM_FI_DEV_GPU_UTIL")
        if result.get("status") == "success":
            for item in result.get("data", {}).get("result", []):
                metric = item.get("metric", {})
                node = metric.get("Hostname", metric.get("instance", "unknown"))
                gpu_uuid = metric.get("UUID", metric.get("gpu", ""))
                gpu_name = metric.get("modelName", metric.get("gpu_model", ""))

                # DCGM uses gpu index in the metric
                try:
                    gpu_index = int(metric.get("gpu", 0))
                except (ValueError, TypeError):
                    gpu_index = 0

                value = item.get("value", [None, None])
                utilization = float(value[1]) if value[1] else None

                key = (node, gpu_index)
                metrics_map[key] = GPUMetrics(
                    gpu_index=gpu_index,
                    node=node,
                    gpu_uuid=gpu_uuid,
                    gpu_name=gpu_name,
                    utilization_percent=utilization,
                )
    except HTTPException:
        # If Prometheus is not available, return empty dict
        return {}

    # Fetch additional metrics
    for metric_name, query in dcgm_queries.items():
        if metric_name == "utilization":
            continue  # Already fetched

        try:
            result = query_prometheus(query)
            if result.get("status") == "success":
                for item in result.get("data", {}).get("result", []):
                    metric = item.get("metric", {})
                    node = metric.get("Hostname", metric.get("instance", "unknown"))
                    try:
                        gpu_index = int(metric.get("gpu", 0))
                    except (ValueError, TypeError):
                        gpu_index = 0

                    key = (node, gpu_index)
                    value = item.get("value", [None, None])

                    if key in metrics_map and value[1]:
                        if metric_name == "memory_used":
                            # DCGM reports in MiB, convert to bytes
                            metrics_map[key].memory_used_bytes = int(float(value[1]) * 1024 * 1024)
                        elif metric_name == "memory_free":
                            metrics_map[key].memory_free_bytes = int(float(value[1]) * 1024 * 1024)
                        elif metric_name == "peak_memory":
                            # DCGM reports in MiB, convert to bytes
                            metrics_map[key].peak_memory_used_bytes = int(float(value[1]) * 1024 * 1024)
                        elif metric_name == "temperature":
                            metrics_map[key].temperature_celsius = float(value[1])
                        elif metric_name == "power":
                            metrics_map[key].power_usage_watts = float(value[1])
        except HTTPException:
            continue

    # Calculate total memory where we have both used and free
    for key, gpu in metrics_map.items():
        if gpu.memory_used_bytes is not None and gpu.memory_free_bytes is not None:
            gpu.memory_total_bytes = gpu.memory_used_bytes + gpu.memory_free_bytes

    return metrics_map


def get_k8s_client():
    """Initialize Kubernetes client."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.AppsV1Api(), client.CoreV1Api()


def parse_gpu_count(resources: dict) -> int:
    """Extract GPU count from resource limits."""
    if not resources or not resources.limits:
        return 0
    gpu_value = resources.limits.get("nvidia.com/gpu", "0")
    try:
        return int(gpu_value)
    except (ValueError, TypeError):
        return 0


@app.get("/health", response_model=HealthResponse, tags=["health"])
async def health_check():
    """Health check endpoint."""
    namespace = os.getenv("NAMESPACE", "nims")
    return HealthResponse(status="healthy", namespace=namespace)


@app.get("/api/v1/nims", response_model=NIMSummary, tags=["nims"])
async def list_nims():
    """
    List all NIM deployments with their status, resource allocation, and pod information.

    Returns information about:
    - Deployment name and image
    - Replica counts (desired, ready, available)
    - Resource requests/limits (CPU, memory, GPU)
    - Individual pod status and node placement
    """
    namespace = os.getenv("NAMESPACE", "nims")

    try:
        apps_v1, core_v1 = get_k8s_client()

        # Get all deployments in namespace
        deployments = apps_v1.list_namespaced_deployment(namespace=namespace)

        # Get all pods in namespace
        pods = core_v1.list_namespaced_pod(namespace=namespace)
        pod_map = {}
        for pod in pods.items:
            app_label = pod.metadata.labels.get("app", "")
            if app_label not in pod_map:
                pod_map[app_label] = []

            # Get GPU index from node labels or annotations if available
            gpu_index = None
            if pod.spec.node_name:
                try:
                    node = core_v1.read_node(pod.spec.node_name)
                    gpu_index = node.metadata.labels.get("nvidia.com/gpu.product", None)
                except:
                    pass

            restart_count = 0
            if pod.status.container_statuses:
                restart_count = sum(cs.restart_count for cs in pod.status.container_statuses)

            pod_map[app_label].append(PodInfo(
                name=pod.metadata.name,
                status=pod.status.phase,
                node=pod.spec.node_name,
                gpu_index=gpu_index,
                restart_count=restart_count
            ))

        nims = []
        total_gpus = 0
        running_count = 0

        for dep in deployments.items:
            # Skip non-NIM deployments (like the proxy)
            if dep.metadata.name in ["nims-proxy"]:
                continue

            app_label = dep.spec.selector.match_labels.get("app", dep.metadata.name)

            # Get container resources
            resources = ResourceInfo()
            image = ""
            if dep.spec.template.spec.containers:
                container = dep.spec.template.spec.containers[0]
                image = container.image
                if container.resources:
                    if container.resources.requests:
                        resources.cpu_request = container.resources.requests.get("cpu")
                        resources.memory_request = container.resources.requests.get("memory")
                    if container.resources.limits:
                        resources.cpu_limit = container.resources.limits.get("cpu")
                        resources.memory_limit = container.resources.limits.get("memory")
                        resources.gpu_count = parse_gpu_count(container.resources)

            replicas_desired = dep.spec.replicas or 0
            replicas_ready = dep.status.ready_replicas or 0
            replicas_available = dep.status.available_replicas or 0

            if replicas_ready > 0:
                running_count += 1

            total_gpus += (resources.gpu_count or 0) * replicas_desired

            nim_info = NIMInfo(
                name=dep.metadata.name,
                image=image,
                replicas_desired=replicas_desired,
                replicas_ready=replicas_ready,
                replicas_available=replicas_available,
                port=NIM_PORTS.get(dep.metadata.name),
                resources=resources,
                pods=pod_map.get(app_label, [])
            )
            nims.append(nim_info)

        # Sort by name
        nims.sort(key=lambda x: x.name)

        return NIMSummary(
            total_nims=len(nims),
            running_nims=running_count,
            total_gpus_requested=total_gpus,
            nims=nims
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/nims/{nim_name}", response_model=NIMInfo, tags=["nims"])
async def get_nim(nim_name: str):
    """
    Get detailed information about a specific NIM deployment.

    Args:
        nim_name: Name of the NIM deployment (e.g., 'openfold3', 'boltz2')
    """
    namespace = os.getenv("NAMESPACE", "nims")

    try:
        apps_v1, core_v1 = get_k8s_client()

        # Get specific deployment
        try:
            dep = apps_v1.read_namespaced_deployment(name=nim_name, namespace=namespace)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                raise HTTPException(status_code=404, detail=f"NIM '{nim_name}' not found")
            raise

        app_label = dep.spec.selector.match_labels.get("app", dep.metadata.name)

        # Get pods for this deployment
        pods = core_v1.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"app={app_label}"
        )

        pod_list = []
        for pod in pods.items:
            gpu_index = None
            if pod.spec.node_name:
                try:
                    node = core_v1.read_node(pod.spec.node_name)
                    gpu_index = node.metadata.labels.get("nvidia.com/gpu.product", None)
                except:
                    pass

            restart_count = 0
            if pod.status.container_statuses:
                restart_count = sum(cs.restart_count for cs in pod.status.container_statuses)

            pod_list.append(PodInfo(
                name=pod.metadata.name,
                status=pod.status.phase,
                node=pod.spec.node_name,
                gpu_index=gpu_index,
                restart_count=restart_count
            ))

        # Get container resources
        resources = ResourceInfo()
        image = ""
        if dep.spec.template.spec.containers:
            container = dep.spec.template.spec.containers[0]
            image = container.image
            if container.resources:
                if container.resources.requests:
                    resources.cpu_request = container.resources.requests.get("cpu")
                    resources.memory_request = container.resources.requests.get("memory")
                if container.resources.limits:
                    resources.cpu_limit = container.resources.limits.get("cpu")
                    resources.memory_limit = container.resources.limits.get("memory")
                    resources.gpu_count = parse_gpu_count(container.resources)

        return NIMInfo(
            name=dep.metadata.name,
            image=image,
            replicas_desired=dep.spec.replicas or 0,
            replicas_ready=dep.status.ready_replicas or 0,
            replicas_available=dep.status.available_replicas or 0,
            port=NIM_PORTS.get(dep.metadata.name),
            resources=resources,
            pods=pod_list
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/gpus", response_model=GPUSummary, tags=["gpus"])
async def list_gpus():
    """
    List all GPUs in the cluster with their current metrics.

    Returns real-time GPU metrics from DCGM Exporter via Prometheus:
    - GPU utilization percentage
    - Memory usage (used, free, total in bytes)
    - Temperature in Celsius
    - Power consumption in Watts
    """
    try:
        metrics_map = get_gpu_metrics_from_prometheus()

        gpus = list(metrics_map.values())
        # Sort by node, then by gpu_index
        gpus.sort(key=lambda x: (x.node, x.gpu_index))

        return GPUSummary(
            total_gpus=len(gpus),
            gpus=gpus
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/nims/{nim_name}/gpu-metrics", response_model=NIMGPUMetrics, tags=["gpus"])
async def get_nim_gpu_metrics(nim_name: str):
    """
    Get GPU metrics for a specific NIM deployment.

    Returns GPU metrics for each pod in the NIM deployment, correlating
    the pod's node placement with the GPU metrics from that node.

    Args:
        nim_name: Name of the NIM deployment (e.g., 'openfold3', 'boltz2')
    """
    namespace = os.getenv("NAMESPACE", "nims")

    try:
        apps_v1, core_v1 = get_k8s_client()

        # Get specific deployment
        try:
            dep = apps_v1.read_namespaced_deployment(name=nim_name, namespace=namespace)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                raise HTTPException(status_code=404, detail=f"NIM '{nim_name}' not found")
            raise

        app_label = dep.spec.selector.match_labels.get("app", dep.metadata.name)

        # Get pods for this deployment
        pods = core_v1.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"app={app_label}"
        )

        # Get GPU count requested by this deployment
        gpu_count_per_pod = 0
        if dep.spec.template.spec.containers:
            container = dep.spec.template.spec.containers[0]
            if container.resources and container.resources.limits:
                gpu_value = container.resources.limits.get("nvidia.com/gpu", "0")
                try:
                    gpu_count_per_pod = int(gpu_value)
                except (ValueError, TypeError):
                    gpu_count_per_pod = 0

        # Get GPU metrics from Prometheus
        metrics_map = get_gpu_metrics_from_prometheus()

        pod_gpu_metrics = []
        for pod in pods.items:
            node_name = pod.spec.node_name
            if not node_name:
                continue

            # Find all GPUs on this node
            node_gpus = [
                gpu for (node, gpu_idx), gpu in metrics_map.items()
                if node == node_name
            ]

            # Sort by GPU index
            node_gpus.sort(key=lambda x: x.gpu_index)

            # If we know how many GPUs this pod uses, limit to that count
            # (assuming sequential allocation starting from GPU 0)
            if gpu_count_per_pod > 0 and len(node_gpus) > gpu_count_per_pod:
                node_gpus = node_gpus[:gpu_count_per_pod]

            pod_gpu_metrics.append(PodGPUMetrics(
                pod_name=pod.metadata.name,
                node=node_name,
                gpu_metrics=node_gpus
            ))

        return NIMGPUMetrics(
            name=nim_name,
            namespace=namespace,
            pods=pod_gpu_metrics
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
