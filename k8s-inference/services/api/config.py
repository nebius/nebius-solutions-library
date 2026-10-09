"""Environment-driven settings for the customer API."""
import os

LITELLM_URL = os.environ.get("LITELLM_URL", "http://litellm.litellm.svc:4000")
LITELLM_MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
# The platform-internal LiteLLM key (Secret api/litellm-internal, control cluster): the api_key of the model
# groups the API registers for OpenAI endpoints, so LiteLLM passes the edge key check on their gateway
# hostnames. Optional: without it no group is registered (the endpoint still answers through the API).
LITELLM_INTERNAL_KEY = os.environ.get("LITELLM_INTERNAL_KEY", "")
CATALOG_DIRS = os.environ.get("CATALOG_DIRS", "/app/catalog/models").split(":")
REGION = os.environ.get("REGION", "eu-north1")                      # the region this API runs in (in-cluster client)
HUB_REGION = os.environ.get("HUB_REGION", REGION)                   # the region the catalog's `deployments.hub` means (control cluster: eu-north1)
# other regions: "<region>=<kubeconfig path>,..." (Secret mounted by the Deployment; missing files are skipped)
REGION_KUBECONFIGS = dict(kv.split("=", 1) for kv in os.environ.get("REGION_KUBECONFIGS", "eu-south1=/etc/kubeconfigs/eu-south1").split(",") if "=" in kv)
# regional API hosts: "<region>=https://api.<ip>.sslip.io,...". A sync/async invoke for another region is
# forwarded there (same key, same body) instead of answered with 400; the control cluster's API sets this.
REGION_API_URLS = dict(kv.split("=", 1) for kv in os.environ.get("REGION_API_URLS", "").split(",") if "=" in kv)
# public hostnames: the API's own URL (endpoint invoke links) and Grafana per region (operation log
# links into Loki Explore): "<region>=<url>,..."
PUBLIC_API_URL = os.environ.get("PUBLIC_API_URL", "").rstrip("/")
GRAFANA_URLS = dict(kv.split("=", 1) for kv in os.environ.get("GRAFANA_URLS", "").split(",") if "=" in kv)
MODELS_NAMESPACE = os.environ.get("MODELS_NAMESPACE", "models")
# The API's own namespace: runtime model definitions live there as ConfigMaps `catalog-<id>` (services/api/models.py)
API_NAMESPACE = os.environ.get("API_NAMESPACE") or (open("/var/run/secrets/kubernetes.io/serviceaccount/namespace").read().strip()
                                                     if os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/namespace") else "api")
CHART_DIR = os.environ.get("CHART_DIR", "/app/charts/endpoint")   # charts/endpoint, rendered by `helm template` for runtime endpoints
IMAGES_HOST = os.environ.get("IMAGES_HOST", "registry.serverless2.local")   # the logical registry host (terraform.tfvars images.host)
IMAGES_SOURCE = os.environ.get("IMAGES_SOURCE", "")                         # the fleet's own registry (images.source), alias `nebius`
# TLS of the model endpoints' hostnames: the fleet's API rewrites Certificate envoy-gateway-system/models of a cluster
# (clusters/common/manifests/gateway/models-tls.yaml) with the hostnames of the endpoints deployed there, issued by
# ACME_ISSUER (letsencrypt | letsencrypt-staging); without endpoints it goes back to the self-signed placeholder.
ACME_ISSUER = os.environ.get("ACME_ISSUER", "letsencrypt")
GATEWAY_NAMESPACE = os.environ.get("GATEWAY_NAMESPACE", "envoy-gateway-system")
MODELS_CERTIFICATE = "models"
# Gateway domain of every region ("<region>=<domain>,..."): the LiteLLM api_base of an endpoint deployed there (control API)
ENDPOINT_DOMAINS = dict(kv.split("=", 1) for kv in os.environ.get("ENDPOINT_DOMAINS", "").split(",") if "=" in kv)
TENANT_NS_PREFIX = os.environ.get("TENANT_NS_PREFIX", "tenant-")
STORAGE_SECRET = os.environ.get("STORAGE_SECRET", "tenant-storage")
S3_ENV_SECRET = os.environ.get("S3_ENV_SECRET", "s3")                # AWS_* env for the runner containers (tenant namespace)
EXECUTOR_SA = os.environ.get("EXECUTOR_SA", "job-runner")            # job ServiceAccount in the tenant namespace (charts/tenant)
# the runner image (services/jobs) through the fleet's logical registry host, pullable on every node of every
# region (terraform.tfvars `images`, docs/IMAGES.md); the same holds for every catalog image
RUNNER_IMAGE = os.environ.get("RUNNER_IMAGE", "registry.serverless2.local/nebius/serverless2/jobs:0.1.7")
# the cost dispatcher's ranking (services/dispatcher `GET /v1/rank`, Service kueue-system/dispatcher): asked at submission
# for a run class with per-GPU-class images (`job.images`), docs/SCHEDULING.md "Per-GPU images for runs"
DISPATCHER_URL = os.environ.get("DISPATCHER_URL", "http://dispatcher.kueue-system.svc:80").rstrip("/")
RANK_TIMEOUT_S = float(os.environ.get("RANK_TIMEOUT_S", "3"))
# External domain of this cluster's endpoints (the Knative domain, e.g. <gateway ip>.sslip.io): async endpoint calls
# from tenant Jobs go through the gateway (TLS, key check, rate limit) as https://<model>-predictor.<ns>.<domain>,
# never to the predictor Service (docs/SECURITY-PREREVIEW.md F2). Empty = no async calls to direct endpoints.
ENDPOINT_DOMAIN = os.environ.get("ENDPOINT_DOMAIN", "")
# curl --connect-to <host>:443:<this>:443 keeps the TLS name and routes the connection to the gateway's in-cluster
# Service instead of the public IP (measured 2026-10-08: both work from a tenant pod on mk8s; the Service avoids the
# NAT hop and keeps working if the public IP is firewalled). Empty = connect to the public IP.
ENDPOINT_CONNECT_TO = os.environ.get("ENDPOINT_CONNECT_TO", "knative-external.envoy-gateway-system.svc:443")
KUEUE_NAMESPACE = os.environ.get("KUEUE_NAMESPACE", "kueue-system")
FLEET_CONFIGMAP = os.environ.get("FLEET_CONFIGMAP", "fleet-prices")   # charts/fleet: pools.yaml (pool -> GPU class)
KUEUE_QUEUE = os.environ.get("KUEUE_QUEUE", "default")            # LocalQueue (= scheduling profile) a model without a GPU preference uses
# This API runs on the fleet's MultiKueue manager (the control cluster, docs/JOBS.md "Fleet placement"): runs are
# created here with `managedBy: kueue.x-k8s.io/multikueue`, placed on a worker by Kueue + the cost dispatcher, and
# read back through the mirrored Job (status) plus the worker's pods (REGION_KUBECONFIGS).
FLEET_MANAGER = os.environ.get("FLEET_MANAGER", "false").lower() == "true"
MULTIKUEUE_MANAGED_BY = "kueue.x-k8s.io/multikueue"
MULTIKUEUE_ORIGIN_LABEL = "kueue.x-k8s.io/multikueue-origin"      # on the worker's copy of a manager Job
KUEUE_JOB_UID_LABEL = "kueue.x-k8s.io/job-uid"                    # Workload -> Job
# Multi-node runs are JobSets (jobset.x-k8s.io, installed on every cluster; Kueue + MultiKueue integration on):
# one indexed Job of N pods, one pod per node, docs/JOBS.md "Multi-node runs".
JOBSET_GROUP, JOBSET_VERSION, JOBSET_PLURAL = "jobset.x-k8s.io", "v1alpha2", "jobsets"
JOBSET_NAME_LABEL = "jobset.sigs.k8s.io/jobset-name"              # on every pod of a JobSet
MULTINODE_MAX_NODES = int(os.environ.get("MULTINODE_MAX_NODES", "64"))
# NCCL/UCX environment for pods on InfiniBand nodes (docs.nebius.com/kubernetes/gpu/nccl-test; the fabric NICs are
# mlx5_*, the pod network stays eth0 for NCCL's bootstrap and UCX). Overridable per job class (`env`).
INFINIBAND_ENV = {"NCCL_IB_HCA": "mlx5", "NCCL_SOCKET_IFNAME": "eth0", "UCX_NET_DEVICES": "eth0",
                  "NCCL_COLLNET_ENABLE": "0", "SHARP_COLL_ENABLE_PCI_RELAXED_ORDERING": "1"}
# DRA ResourceClaimTemplates `<prefix>-<n>` (charts/tenant, tenants with `infiniband: true`) that hand a pod the n
# InfiniBand NICs of its node (ExactCount: what Kueue's DRA accounting supports; DeviceClass ib.networking.nebius.ai,
# published by DraNet on Nebius GPU node images). n = the pool's `ib_devices_per_node` (fleet-prices pools.yaml).
INFINIBAND_CLAIM_TEMPLATE = os.environ.get("INFINIBAND_CLAIM_TEMPLATE", "ib")
# Shared checkpoint claim for multi-node runs (charts/tenant renders it on clusters with the weights filesystem):
# RWX across nodes, so a restarted JobSet and a `:resume` find the checkpoints (`checkpoints: shared`).
SHARED_SCRATCH_PVC = os.environ.get("SHARED_SCRATCH_PVC", "scratch-shared")
KUEUE_PRIORITY = os.environ.get("KUEUE_PRIORITY", "customer-batch")
JOB_TTL_S = int(os.environ.get("JOB_TTL_S", str(90 * 86400)))      # finished Jobs stay this long (operation history)
JOB_BACKOFF_LIMIT = int(os.environ.get("JOB_BACKOFF_LIMIT", "3"))  # application failures before a Job is FAILED (disruptions do not count)
JOB_PVC_SIZE_GI = int(os.environ.get("JOB_PVC_SIZE_GI", "50"))
SYNC_TIMEOUT_S = int(os.environ.get("SYNC_TIMEOUT_S", "600"))
AUTH_CACHE_S = int(os.environ.get("AUTH_CACHE_S", "30"))
LABEL = "serverless2.nebius"
