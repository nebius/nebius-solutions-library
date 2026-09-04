#!/bin/bash
# Unified NCCL test runner — auto-discovers IB devices and API version on
# whatever cluster/GPU type it's pointed at, instead of hardcoding either.
#
# Usage: ./nccl-runner-unified.sh

# Exit on error and surface failures mid-pipeline. We deliberately do NOT add -u:
# the script relies on conditionally-unset vars (e.g. MAP_BY_SPEC and empty query
# results), guarded with ${var:-} where it matters.
set -eo pipefail

# Prevent accidental double-runs on THIS workstation (they'd share the local
# results dir and lock). Cross-workstation safety does not come from this lock —
# it comes from the unique per-run resource names/labels below.
LOCKFILE="/tmp/nccl_runner_unified.lock"
if [ -f "$LOCKFILE" ] && kill -0 "$(cat "$LOCKFILE" 2>/dev/null)" 2>/dev/null; then
  echo "ERROR: another nccl_runner_unified.sh is already running on this machine (PID $(cat "$LOCKFILE"))."
  echo "Let it finish first, or if that PID is dead, remove $LOCKFILE and re-run."
  exit 1
fi
echo $$ > "$LOCKFILE"
trap 'rm -f "$LOCKFILE"' EXIT

# Unique per-run resource identity. A local lock can't stop a second engineer on
# a different workstation from driving the same namespace, so every run names and
# labels its own MPIJob/pods (nccl-test-<pid>-<epoch>, label nccl-runner/run=<id>).
# All lookups and cleanup below are scoped to these, so concurrent runs sharing a
# namespace never touch each other's resources.
RUN_SUFFIX="$$-$(date +%s)"
JOB_NAME="nccl-test-${RUN_SUFFIX}"
# Sanitize into a valid Kubernetes label value: lowercase, non-alphanumerics to
# '-', collapse repeats, and trim leading/trailing '-' (a label must start and
# end alphanumeric — e.g. id -un's trailing newline would otherwise leave "user-").
RUN_OWNER=$(id -un 2>/dev/null | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-' | sed -E 's/-+/-/g; s/^-+//; s/-+$//')
RUN_OWNER="${RUN_OWNER:0:40}"
RUN_OWNER="${RUN_OWNER:-unknown}"

# Clean up this run's cluster resources on exit or interrupt (Ctrl-C). Everything
# we create is uniquely named/labelled for this run, so this only ever removes our
# own MPIJob and discovery pod — never another engineer's. Idempotent, so it's
# harmless on a normal exit where per-iteration cleanup has already run.
cleanup() {
  rm -f "$LOCKFILE"
  [ -z "${NAMESPACE:-}" ] && return
  kubectl delete mpijob "$JOB_NAME" -n "$NAMESPACE" \
    --ignore-not-found --cascade=foreground --wait=false >/dev/null 2>&1 || true
  kubectl delete pod ib-discovery -n "$NAMESPACE" --ignore-not-found >/dev/null 2>&1 || true
  # On DRA/GB300, also tear down the ComputeDomain + GPU claim template this run
  # created, so no cross-node NVLink domain lingers. GPU_MODE is read at exit time.
  if [ "${GPU_MODE:-}" = "dra" ]; then
    kubectl delete computedomain nccl-cd -n "$NAMESPACE" --ignore-not-found >/dev/null 2>&1 || true
    kubectl delete resourceclaimtemplate nccl-gpus -n "$NAMESPACE" --ignore-not-found >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# ============ CONFIGURE FOR YOUR CLUSTER ============
NAMESPACE=nccl-tests
TEMPLATE=nccl-test-template.yaml          # x86/device-plugin; DRA path swaps to the -dra variant
TEMPLATE_DRA=nccl-test-template-dra.yaml  # GB300/DRA (GPU claims instead of nvidia.com/gpu limits)
# The image is multi-arch (amd64 + arm64/Grace), so the same tag runs on x86 and GB300.
IMAGE="cr.eu-north1.nebius.cloud/e00b94r7bkvywphmn6/nccl-tests:v2.18.3-cudav13.2.1-ncclv2.30.4-1-hpcxv2.26"

# GPU node group ID — SET THIS for your cluster (env-overridable). Get it via:
#   kubectl get nodes -o custom-columns='NAME:.metadata.name,GROUP:.metadata.labels.nebius\.com/node-group-id,GPU:.status.capacity.nvidia\.com/gpu'
NODE_GROUP_ID="${NODE_GROUP_ID:-<your-node-group-id>}"   # e.g. mk8snodegroup-xxxxxxxxxxxxxxxxxx

# Cross-node transport on DRA/GB300 only (ignored on device-plugin/x86). On GB300 the
# nodes fuse into one MNNVL (multi-node NVLink) domain, so cross-node NCCL defaults to
# NVLink. Set NCCL_TRANSPORT=ib to disable MNNVL/NVLS and measure the InfiniBand fabric.
NCCL_TRANSPORT="${NCCL_TRANSPORT:-auto}"

# Worker resource sizing — auto-detected below from actual node capacity.
# RESERVE values are headroom left for kubelet, device plugins, DaemonSets, etc.
RESERVE_CPU_CORES=4
RESERVE_MEM_GI=50

# The MPIJob launcher intermittently fails to start with an hwloc/topology
# init glitch ("opal_hwloc_base_open failed" / "binding policy not recognized")
# — a transient race that clears on a fresh launch. Each failed attempt fails
# fast (launcher crash-loops within ~1 min), so we just relaunch a few times.
MAX_LAUNCH_ATTEMPTS=6

# Host counts to sweep, and which collectives to run. Both overridable from the
# environment (space-separated) for a quick single run, e.g.:
#   NCCL_HOSTS="1 2" NCCL_TESTS="all_reduce" ./nccl_runner_unified.sh
HOSTS=(${NCCL_HOSTS:-1 2 3 4})
# The two tests that actually matter for validating a cluster: all_reduce
# (canonical bus-bandwidth number) and alltoall (stresses the fabric hardest).
# The other NCCL collectives are nice-to-haves — add them here if you want a
# fuller sweep: all_gather reduce_scatter reduce gather broadcast scatter
TESTS=(${NCCL_TESTS:-all_reduce alltoall})
# ======================================================

if [ -z "$NODE_GROUP_ID" ] || [[ "$NODE_GROUP_ID" == "<"* ]]; then
  echo "ERROR: set NODE_GROUP_ID at the top of this script to your GPU node group ID."
  echo "Find it with:"
  echo "  kubectl get nodes -o custom-columns='NAME:.metadata.name,GROUP:.metadata.labels.nebius\\.com/node-group-id'"
  exit 1
fi

echo "=== Detecting node capacity for node group $NODE_GROUP_ID ==="
# Count only nodes that can actually take work: Ready AND schedulable. A Cordoned
# or NotReady node still carries the node-group label, so counting it would size a
# host sweep the cluster can't place — the launcher never appears and watch_job
# then burns the full timeout waiting for it.
NODE_NAMES=()
while read -r name ready unsched; do
  [ -n "$name" ] || continue
  [ "$ready" = "True" ] || continue
  [ "$unsched" = "true" ] && continue
  NODE_NAMES+=("$name")
done < <(kubectl get nodes -l "nebius.com/node-group-id=$NODE_GROUP_ID" \
  -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.conditions[?(@.type=="Ready")].status}{" "}{.spec.unschedulable}{"\n"}{end}')

NODE_NAME="${NODE_NAMES[0]:-}"
if [ -z "$NODE_NAME" ]; then
  echo "ERROR: no Ready, schedulable nodes found for node-group-id=$NODE_GROUP_ID"
  echo "(Nodes may be Cordoned or NotReady — check: kubectl get nodes -l nebius.com/node-group-id=$NODE_GROUP_ID)"
  exit 1
fi
NODE_COUNT=${#NODE_NAMES[@]}

# Drop any requested host counts that exceed the nodes actually in this group,
# so the tool "just runs" on a 2-node cluster without hanging on unschedulable
# 3-/4-node jobs.
CAPPED_HOSTS=()
for h in "${HOSTS[@]}"; do
  if [ "$h" -le "$NODE_COUNT" ]; then CAPPED_HOSTS+=("$h"); fi
done
if [ "${#CAPPED_HOSTS[@]}" -lt "${#HOSTS[@]}" ]; then
  echo "NOTE: node group has $NODE_COUNT node(s); capping host counts to ${CAPPED_HOSTS[*]} (dropped: was ${HOSTS[*]})"
fi
HOSTS=("${CAPPED_HOSTS[@]}")

ALLOC_CPU_RAW=$(kubectl get node "$NODE_NAME" -o jsonpath='{.status.allocatable.cpu}')
ALLOC_MEM_RAW=$(kubectl get node "$NODE_NAME" -o jsonpath='{.status.allocatable.memory}')
# GPU request mechanism: device-plugin (nvidia.com/gpu) vs DRA (gpu.nvidia.com
# DeviceClass, GB300/Grace). DRA nodes advertise NO allocatable nvidia.com/gpu, so
# detect that and switch to claims + the DRA template. The x86 path is unchanged.
GPUS_PER_NODE=$(kubectl get node "$NODE_NAME" -o jsonpath='{.status.allocatable.nvidia\.com/gpu}')
if [ -n "$GPUS_PER_NODE" ] && [ "$GPUS_PER_NODE" -gt 0 ] 2>/dev/null; then
  GPU_MODE="device-plugin"
elif kubectl get deviceclass gpu.nvidia.com >/dev/null 2>&1; then
  GPU_MODE="dra"
  TEMPLATE="$TEMPLATE_DRA"
  # DRA advertises no allocatable nvidia.com/gpu; take GPUs/node from the GFD
  # label, falling back to counting this node's gpu.nvidia.com devices in the
  # published resourceslices (portable awk, no jq / grep -P).
  GPUS_PER_NODE=$(kubectl get node "$NODE_NAME" -o jsonpath='{.metadata.labels.nvidia\.com/gpu\.count}' 2>/dev/null)
  if [ -z "$GPUS_PER_NODE" ] || [ "$GPUS_PER_NODE" -le 0 ] 2>/dev/null; then
    GPUS_PER_NODE=$(kubectl get resourceslices \
      -o jsonpath='{range .items[*]}{.spec.driver}{"\t"}{range .spec.devices[*]}{.name}{","}{end}{"\n"}{end}' 2>/dev/null \
      | awk -F'\t' '$1=="gpu.nvidia.com" && !seen {n=gsub(/,/,",",$2); if(n>0){print n; seen=1}}')
  fi
else
  echo "ERROR: node '$NODE_NAME' reports no allocatable nvidia.com/gpu and no gpu.nvidia.com"
  echo "DeviceClass — is this a GPU node group? (device-plugin and DRA both absent)"
  exit 1
fi
if [ -z "$GPUS_PER_NODE" ] || [ "$GPUS_PER_NODE" -le 0 ] 2>/dev/null; then
  echo "ERROR: could not determine GPUs/node on '$NODE_NAME' (mode=$GPU_MODE)."
  exit 1
fi

# Transport knob only applies on DRA; map it to the NCCL env the DRA template carries.
if [ "$GPU_MODE" = "dra" ] && [ "$NCCL_TRANSPORT" = "ib" ]; then
  NCCL_MNNVL_ENABLE=0; NCCL_NVLS_ENABLE=0
else
  NCCL_MNNVL_ENABLE=1; NCCL_NVLS_ENABLE=1
fi
echo "GPU mode: $GPU_MODE$([ "$GPU_MODE" = dra ] && echo " (transport: $NCCL_TRANSPORT -> $([ "$NCCL_TRANSPORT" = ib ] && echo InfiniBand || echo MNNVL/NVLink))")"

# CPU may be reported as whole cores ("128") or millicores ("127900m") — normalize to millicores.
if [[ "$ALLOC_CPU_RAW" == *m ]]; then
  ALLOC_CPU_MILLI="${ALLOC_CPU_RAW%m}"
else
  ALLOC_CPU_MILLI=$((ALLOC_CPU_RAW * 1000))
fi
ALLOC_CPU_CORES=$((ALLOC_CPU_MILLI / 1000))

# Memory is typically reported in Ki — convert to Gi (1 Gi = 1,048,576 Ki).
ALLOC_MEM_KI="${ALLOC_MEM_RAW%Ki}"
ALLOC_MEM_GI=$((ALLOC_MEM_KI / 1048576))

WORKER_CPU_LIMIT=$((ALLOC_CPU_CORES - RESERVE_CPU_CORES))
WORKER_CPU=$((WORKER_CPU_LIMIT - RESERVE_CPU_CORES))  # small gap between request/limit, same pattern as before
WORKER_MEMORY_GI=$((ALLOC_MEM_GI - RESERVE_MEM_GI))
WORKER_MEMORY="${WORKER_MEMORY_GI}Gi"

if [ "$WORKER_CPU" -le 0 ] || [ "$WORKER_MEMORY_GI" -le 0 ]; then
  echo "ERROR: computed worker sizing is non-positive (CPU=$WORKER_CPU, MEM=${WORKER_MEMORY_GI}Gi)."
  echo "Node allocatable was: CPU=$ALLOC_CPU_CORES cores, MEM=${ALLOC_MEM_GI}Gi — check RESERVE_* values."
  exit 1
fi

echo "Node '$NODE_NAME' allocatable: ${ALLOC_CPU_CORES} CPU cores, ${ALLOC_MEM_GI}Gi memory, ${GPUS_PER_NODE} GPUs"
echo "Worker sizing: request=${WORKER_CPU} CPU / limit=${WORKER_CPU_LIMIT} CPU, memory=${WORKER_MEMORY}"
echo ""

echo "=== Discovering IB devices on node group $NODE_GROUP_ID ==="
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null

# Spin up a single throwaway pod on the target node group to inspect real IB hardware.
# This removes all guesswork about mlx5_X numbering per GPU type — it asks the
# actual node what devices it has, every time.
cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: ib-discovery
  namespace: $NAMESPACE
spec:
  restartPolicy: Never
  tolerations:
    - key: nvidia.com/gpu
      operator: Exists
      effect: NoSchedule
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
          - matchExpressions:
              - key: nebius.com/node-group-id
                operator: In
                values:
                  - $NODE_GROUP_ID
  containers:
    - name: ib-discovery
      image: $IMAGE
      command: ["sleep", "120"]
      securityContext:
        privileged: true
EOF

echo "Waiting for discovery pod to be ready..."
if ! kubectl wait --namespace "$NAMESPACE" --for=condition=Ready pod/ib-discovery --timeout=120s; then
  echo "ERROR: discovery pod never became ready — check GPU availability / node affinity."
  kubectl delete pod ib-discovery -n "$NAMESPACE" --ignore-not-found >/dev/null 2>&1
  exit 1
fi

# List all devices, then keep only ones that are actually InfiniBand and ACTIVE.
ALL_DEVICES=$(kubectl exec -n "$NAMESPACE" ib-discovery -- bash -c "ibv_devices | tail -n +3 | awk '{print \$1}'" 2>/dev/null)

if [ -z "$ALL_DEVICES" ]; then
  echo "ERROR: no IB devices found at all on this node (ibv_devices returned nothing)."
  kubectl delete pod ib-discovery -n "$NAMESPACE" --ignore-not-found >/dev/null 2>&1
  exit 1
fi

ACTIVE_DEVICES=""
for dev in $ALL_DEVICES; do
  INFO=$(kubectl exec -n "$NAMESPACE" ib-discovery -- ibv_devinfo -d "$dev" 2>/dev/null)
  # ibv_devinfo prints "link_layer: InfiniBand" and "state: PORT_ACTIVE (4)".
  # Parse with awk — grep -oP (PCRE / \K) is GNU-only and not available on macOS.
  LINK_LAYER=$(echo "$INFO" | awk '$1=="link_layer:"{print $2; exit}')
  STATE=$(echo "$INFO" | awk '$1=="state:"{sub(/^PORT_/,"",$2); print $2; exit}')
  if [ "$LINK_LAYER" == "InfiniBand" ] && [ "$STATE" == "ACTIVE" ]; then
    ACTIVE_DEVICES="${ACTIVE_DEVICES}${ACTIVE_DEVICES:+,}${dev}"
  fi
done

# While the discovery pod is still up, grab CPU topology from the SAME node so
# process binding can be computed instead of hardcoded. The old template used
# a fixed "ppr:4:numa:pe=24" tuned for B300's wide CPUs — that demands 96
# hwthreads/NUMA and aborts on narrower nodes (e.g. H200: 64 hwthreads/NUMA),
# which surfaces as the launcher's "ORTE has lost communication" crash.
CONTAINER_CPUS=$(kubectl exec -n "$NAMESPACE" ib-discovery -- nproc 2>/dev/null)
NUMA_NODES=$(kubectl exec -n "$NAMESPACE" ib-discovery -- bash -c 'ls -d /sys/devices/system/node/node[0-9]* 2>/dev/null | wc -l' 2>/dev/null)

kubectl delete pod ib-discovery -n "$NAMESPACE" --ignore-not-found >/dev/null 2>&1

if [ -z "$ACTIVE_DEVICES" ]; then
  echo "ERROR: no ACTIVE InfiniBand devices found among: $ALL_DEVICES"
  echo "Check node health, fabric config, or run ibv_devinfo manually to investigate."
  exit 1
fi

NCCL_IB_HCA="$ACTIVE_DEVICES"
FIRST_DEVICE=$(echo "$ACTIVE_DEVICES" | cut -d',' -f1)
UCX_NET_DEVICE="${FIRST_DEVICE}:1"

echo "Discovered active IB devices: $NCCL_IB_HCA"
echo "Using UCX_NET_DEVICES: $UCX_NET_DEVICE"
echo ""

# Compute process binding from the real CPU/NUMA/GPU topology.
# One rank per GPU, ranks spread evenly across NUMA nodes, each rank pinned to
# an equal share of that NUMA's hwthreads (pe). Filling exactly avoids ORTE's
# "binding more processes than cpus" overload abort. When GPUs don't divide
# evenly across NUMA nodes (or topology couldn't be read), fall back to a
# coarse NUMA binding that has no per-element requirement and can't overload.
if [ -n "$CONTAINER_CPUS" ] && [ -n "$NUMA_NODES" ] && [ "$NUMA_NODES" -gt 0 ] 2>/dev/null \
   && [ $((GPUS_PER_NODE % NUMA_NODES)) -eq 0 ]; then
  PROCS_PER_NUMA=$((GPUS_PER_NODE / NUMA_NODES))
  PE_PER_PROC=$((CONTAINER_CPUS / GPUS_PER_NODE))   # = hwthreads-per-NUMA / procs-per-NUMA
  if [ "$PE_PER_PROC" -ge 1 ]; then
    BIND_TO="hwthread"
    MAP_BY_SPEC="ppr:${PROCS_PER_NUMA}:numa:pe=${PE_PER_PROC}"
  fi
fi
if [ -z "${MAP_BY_SPEC:-}" ]; then
  echo "NOTE: falling back to coarse NUMA binding (CPUs=${CONTAINER_CPUS:-?}, NUMA=${NUMA_NODES:-?}, GPUs=$GPUS_PER_NODE)"
  BIND_TO="numa"
  MAP_BY_SPEC="ppr:${GPUS_PER_NODE}:node"
fi
echo "Detected topology: ${CONTAINER_CPUS:-?} hwthreads across ${NUMA_NODES:-?} NUMA node(s), $GPUS_PER_NODE GPUs"
echo "Process binding: -bind-to $BIND_TO --map-by $MAP_BY_SPEC"
echo ""

# Auto-detect the MPIJob API version actually registered on THIS cluster.
# Different clusters/operator installs register v1 or v2beta1 — hardcoding
# either one breaks on the other, so detect it fresh each run.
MPIJOB_API_VERSION=$(kubectl get crd mpijobs.kubeflow.org -o jsonpath='{.spec.versions[0].name}' 2>/dev/null)
if [ -z "$MPIJOB_API_VERSION" ]; then
  echo "ERROR: mpijobs.kubeflow.org CRD not found on this cluster."
  echo "Install the MPI Operator first, e.g.:"
  echo "  kubectl apply --server-side -k \"github.com/kubeflow/mpi-operator/manifests/overlays/standalone?ref=v0.6.0\""
  exit 1
fi
echo "Detected MPIJob API version: $MPIJOB_API_VERSION"

# launcherCreationPolicy only exists on v2beta1 — omit entirely on v1
if [ "$MPIJOB_API_VERSION" == "v2beta1" ]; then
  LAUNCHER_CREATION_POLICY_LINE="launcherCreationPolicy: WaitForWorkersReady"
else
  LAUNCHER_CREATION_POLICY_LINE=""
fi

# Watch a launched job and classify the outcome:
#   PASS    — MPIJob reached Succeeded (real results are in the launcher log)
#   FLAKE   — launcher crash-looped or the job failed early (the hwloc glitch); retry
#   TIMEOUT — job ran but never finished within the window; don't retry, just collect
watch_job() {
  local waited=0 timeout=1500 interval=5 lp rs failed
  while [ "$waited" -lt "$timeout" ]; do
    if kubectl get mpijob "$JOB_NAME" -n "$NAMESPACE" \
         -o jsonpath='{.status.conditions[?(@.type=="Succeeded")].status}' 2>/dev/null | grep -q True; then
      echo PASS; return
    fi
    failed=$(kubectl get mpijob "$JOB_NAME" -n "$NAMESPACE" \
         -o jsonpath='{.status.conditions[?(@.type=="Failed")].status}' 2>/dev/null)
    if [ "$failed" == "True" ]; then echo FLAKE; return; fi
    lp=$(kubectl get pods -n "$NAMESPACE" -l "nccl-runner/run=$RUN_SUFFIX,nccl-runner/role=launcher" -o name 2>/dev/null | head -1)
    rs=$(kubectl get -n "$NAMESPACE" "$lp" -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null)
    if [ "${rs:-0}" -ge 3 ] 2>/dev/null; then echo FLAKE; return; fi
    sleep "$interval"; waited=$((waited + interval))
  done
  echo TIMEOUT
}

# On DRA/GB300, create the GPU claim template and a ComputeDomain once, up front.
# The ComputeDomain is sized to the LARGEST host count in this run so a single
# domain covers every 1..N-host iteration; its controller auto-creates the channel
# claim template (nccl-channel) the worker pods attach to for cross-node NCCL. Both
# live in $NAMESPACE and are torn down by cleanup() on exit.
if [ "$GPU_MODE" = "dra" ]; then
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  MAX_HOSTS=0; for h in "${HOSTS[@]}"; do [ "$h" -gt "$MAX_HOSTS" ] && MAX_HOSTS="$h"; done
  echo "=== DRA: applying GPU claim template + ComputeDomain (numNodes=$MAX_HOSTS) ==="
  NAMESPACE="$NAMESPACE" GPUS_PER_NODE="$GPUS_PER_NODE" \
    envsubst < "$SCRIPT_DIR/dra/gpu-resourceclaim-template.yaml" | kubectl apply -f - >/dev/null
  NAMESPACE="$NAMESPACE" MAX_HOSTS="$MAX_HOSTS" \
    envsubst < "$SCRIPT_DIR/dra/compute-domain.yaml" | kubectl apply -f - >/dev/null
  echo "Waiting for the ComputeDomain channel template (nccl-channel)..."
  CH_OK=0
  for _ in $(seq 1 30); do
    if kubectl get resourceclaimtemplate nccl-channel -n "$NAMESPACE" --request-timeout=10s >/dev/null 2>&1; then
      CH_OK=1; break
    fi
    sleep 2
  done
  if [ "$CH_OK" -ne 1 ]; then
    echo "ERROR: ComputeDomain channel template (nccl-channel) never appeared."
    echo "Is the NVIDIA DRA / ComputeDomain controller running on this cluster?"
    exit 1
  fi
fi

OUTPUT_PATH=results/nccl-$(date +"%Y-%m-%d_%H-%M-%S")
mkdir -p "$OUTPUT_PATH"

# Count tests that never launched cleanly, so the script can exit non-zero at the
# end — a run where nothing launched must not look like success to a caller/CI.
FAILED_TESTS=0

for HOST_NUM in "${HOSTS[@]}"; do
  for TEST in "${TESTS[@]}"; do
    echo "=== Starting $TEST on $HOST_NUM host(s) ==="

    # Clean up this run's previous iteration before starting. Scoped to our own
    # uniquely-named MPIJob and cascaded gracefully (foreground) so we delete the
    # pods this MPIJob owns — never unrelated pods, and never a force-kill that
    # removes Pod objects before their processes have actually stopped.
    kubectl delete mpijob "$JOB_NAME" -n "$NAMESPACE" \
      --ignore-not-found --cascade=foreground --wait=true --timeout=60s || true

    RENDERED=$(mktemp)
    JOB_NAME="$JOB_NAME" \
    RUN_SUFFIX="$RUN_SUFFIX" \
    RUN_OWNER="$RUN_OWNER" \
    NAMESPACE="$NAMESPACE" \
    IMAGE="$IMAGE" \
    UCX_NET_DEVICE="$UCX_NET_DEVICE" \
    NCCL_IB_HCA="$NCCL_IB_HCA" \
    NODE_GROUP_ID="$NODE_GROUP_ID" \
    WORKER_REPLICAS="$HOST_NUM" \
    WORKER_CPU="$WORKER_CPU" \
    WORKER_CPU_LIMIT="$WORKER_CPU_LIMIT" \
    WORKER_MEMORY="$WORKER_MEMORY" \
    GPUS_PER_NODE="$GPUS_PER_NODE" \
    BIND_TO="$BIND_TO" \
    MAP_BY_SPEC="$MAP_BY_SPEC" \
    TEST_BINARY="$TEST" \
    MPIJOB_API_VERSION="$MPIJOB_API_VERSION" \
    LAUNCHER_CREATION_POLICY_LINE="$LAUNCHER_CREATION_POLICY_LINE" \
    NCCL_MNNVL_ENABLE="${NCCL_MNNVL_ENABLE:-1}" \
    NCCL_NVLS_ENABLE="${NCCL_NVLS_ENABLE:-1}" \
    envsubst < "$TEMPLATE" > "$RENDERED"

    # Launch, retrying on the transient launcher hwloc glitch (see MAX_LAUNCH_ATTEMPTS).
    OUTCOME=""
    for ATTEMPT in $(seq 1 "$MAX_LAUNCH_ATTEMPTS"); do
      echo "Launch attempt $ATTEMPT/$MAX_LAUNCH_ATTEMPTS..."
      kubectl apply -f "$RENDERED" >/dev/null
      OUTCOME=$(watch_job)
      if [ "$OUTCOME" == "PASS" ]; then
        echo "Job succeeded."
        break
      elif [ "$OUTCOME" == "TIMEOUT" ]; then
        echo "Job ran but did not finish within the window — collecting logs anyway."
        break
      fi
      # FLAKE — relaunch on a clean slate (scoped, graceful; no force-delete)
      echo "Launcher glitched before starting (transient hwloc init); relaunching..."
      kubectl delete mpijob "$JOB_NAME" -n "$NAMESPACE" \
        --ignore-not-found --cascade=foreground --wait=true --timeout=60s >/dev/null 2>&1 || true
      sleep 3
    done
    if [ "$OUTCOME" != "PASS" ] && [ "$OUTCOME" != "TIMEOUT" ]; then
      echo "WARNING: $TEST on $HOST_NUM host(s) never launched cleanly after $MAX_LAUNCH_ATTEMPTS attempts."
      FAILED_TESTS=$((FAILED_TESTS + 1))
    fi

    echo "Collecting logs..."
    # Launcher pod name varies by API version (v2beta1 appends a random suffix),
    # so resolve it by name match instead of assuming a fixed name.
    LAUNCHER_POD=$(kubectl get pods -n "$NAMESPACE" -l "nccl-runner/run=$RUN_SUFFIX,nccl-runner/role=launcher" -o name 2>/dev/null | head -1)
    if [ -n "$LAUNCHER_POD" ]; then
      kubectl logs --namespace "$NAMESPACE" "$LAUNCHER_POD" > "$OUTPUT_PATH/$TEST-$HOST_NUM.log" 2>&1 || true
    else
      echo "WARNING: no launcher pod found to collect logs from." | tee "$OUTPUT_PATH/$TEST-$HOST_NUM.log"
    fi

    echo "Cleaning up..."
    kubectl delete -f "$RENDERED" --ignore-not-found >/dev/null 2>&1
    rm -f "$RENDERED"
  done
done

echo ""
echo "All tests complete. Logs saved to: $OUTPUT_PATH"

# Auto-generate the shareable markdown report from the raw logs just collected.
REPORT_SCRIPT="$(dirname "$0")/generate-report.sh"
if [ -x "$REPORT_SCRIPT" ]; then
  echo "Generating report..."
  if "$REPORT_SCRIPT" "$OUTPUT_PATH"; then
    echo "Report: $OUTPUT_PATH/report.md"
  else
    echo "WARNING: report generation failed — raw logs are still in $OUTPUT_PATH"
  fi
else
  echo "NOTE: $REPORT_SCRIPT not found/executable — skipping report (raw logs are in $OUTPUT_PATH)."
fi

# Fail loudly if any test never launched, so callers/CI can tell a run went bad
# even though logs and a report were still produced.
if [ "$FAILED_TESTS" -gt 0 ]; then
  echo "ERROR: $FAILED_TESTS test(s) never launched cleanly — see WARNING lines above and $OUTPUT_PATH."
  exit 1
fi
