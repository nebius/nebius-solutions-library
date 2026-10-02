#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# File: 06-run-fio-performance-test.sh
# Purpose:
#   Measure PER-HOST storage performance of a Kubernetes-mounted filesystem with
#   FIO (Flexible I/O Tester). Runs four measured tests on one node at a time:
#     1. Sequential read   (rw=read,      bs=1M)  -> primary: throughput
#     2. Sequential write  (rw=write,     bs=1M)  -> primary: throughput
#     3. Random read       (rw=randread,  bs=4K)  -> primary: IOPS
#     4. Random write      (rw=randwrite, bs=4K)  -> primary: IOPS
#   Each test also reports the other two dimensions (throughput/IOPS/latency).
#
#   Initial target: Nebius Shared Filesystem (SFS / data-fs). Works with other
#   Kubernetes-mounted filesystems too, but the reference numbers below are
#   SFS-specific and are NOT universal guarantees. Functional correctness
#   (scripts 01-05) and performance (this script) are separate concerns — this
#   complements, and does not replace, the CSI smoke / RWX / checkpoint / IOR /
#   MDTEST tests.
#
# Canonical preset (matches the validated SFS POC methodology):
#   fio 3.36, ioengine=libaio (repo convention), direct=1, numjobs=64,
#   iodepth=32, size=10G/job, runtime=120s, time_based, group_reporting,
#   multi-file (one 10 GiB file per job => 640 GiB dataset), seq bs=1M, rand 4K,
#   dataset fully preconditioned before measurement, one host at a time.
#
# Two modes:
#   canonical  -> fixed parameters above; comparable to the POC reference only if
#                 the storage shape + environment are also comparable.
#   custom     -> any canonical parameter changed; results are labeled
#                 non-comparable and NOT compared to the reference values.
#
# Usage:   ./06-run-fio-performance-test.sh [options]
#          ./06-run-fio-performance-test.sh --help
#
# Created By: Adam Sabry (Nebius MSA)
# Version: 1.0.0
# -----------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# -----------------------------------------------------------------------------
# Canonical defaults (the validated POC preset). Overridable via env/flags; any
# deviation flips the run to "custom" (non-comparable) — see classify_mode().
# -----------------------------------------------------------------------------
CANON_FIO_VERSION="3.36"
CANON_ENGINE="libaio"      # established repo convention; async, honours iodepth
CANON_NUMJOBS=64
CANON_IODEPTH=32
CANON_DIRECT=1
CANON_SIZE="10G"           # per job (fio G = GiB)
CANON_RUNTIME=120          # seconds per measured test
CANON_SEQ_BS="1M"
CANON_RAND_BS="4K"
CANON_FILE_MODE="multi"

# Effective config (env overrides, then flags below).
FIO_IMAGE="${FIO_IMAGE:-ubuntu:24.04}"        # pinned release; apt fio == 3.36. NOT :latest.
FIO_ENGINE="${FIO_ENGINE:-$CANON_ENGINE}"
FIO_NUMJOBS="${FIO_NUMJOBS:-$CANON_NUMJOBS}"
FIO_IODEPTH="${FIO_IODEPTH:-$CANON_IODEPTH}"
FIO_DIRECT="${FIO_DIRECT:-$CANON_DIRECT}"
FIO_SIZE="${FIO_SIZE:-$CANON_SIZE}"
FIO_RUNTIME="${FIO_RUNTIME:-$CANON_RUNTIME}"
FIO_SEQ_BS="${FIO_SEQ_BS:-$CANON_SEQ_BS}"
FIO_RAND_BS="${FIO_RAND_BS:-$CANON_RAND_BS}"
FIO_FILE_MODE="${FIO_FILE_MODE:-$CANON_FILE_MODE}"   # multi | single
FIO_MODE="${FIO_MODE:-canonical}"                     # canonical | custom (auto-downgrades)

# STORAGE_CLASS env wins over the suite default; --storage-class flag wins over both.
STORAGE_CLASS="${STORAGE_CLASS:-${FILESYSTEM_DEFAULT_STORAGE_CLASS_NAME}}"
EXISTING_PVC="${EXISTING_PVC:-}"            # if set, use this PVC; don't create one
CREATE_PVC="${CREATE_PVC:-auto}"            # auto|true|false (auto => create unless EXISTING_PVC)
PVC_CAPACITY="${PVC_CAPACITY:-}"            # e.g. 800Gi; default computed from dataset+margin
CAPACITY_MARGIN_PCT="${CAPACITY_MARGIN_PCT:-10}"   # free-space safety margin (%)
MOUNT_PATH="${MOUNT_PATH:-/data}"           # where the PVC mounts inside the pod
BENCH_SUBDIR="${BENCH_SUBDIR:-fio-bench}"   # parent dir under the mount for run dirs
TARGET_NODE="${TARGET_NODE:-}"              # explicit node(s), space-separated; else auto
NODE_SELECTOR="${NODE_SELECTOR:-}"          # optional label selector to pick the node pool
RESULT_DIR="${RESULT_DIR:-${SCRIPT_DIR}/fio-results}"
RETAIN_DATA="${RETAIN_DATA:-false}"         # keep the dataset + pod/PVC after the run
CONFIRM_RUN="${CONFIRM_RUN:-}"              # true => skip the interactive prompt
SMOKE="${SMOKE:-false}"                     # reduced, non-canonical quick pipeline check
# Is the target the Nebius Shared Filesystem? The SFS reference comparison is shown
# ONLY for an SFS target (comparing non-SFS results to it would be misleading).
# auto => infer from the StorageClass name; override with true|false.
IS_SFS="${IS_SFS:-auto}"

RUN_ID="$$-$(date +%s)"
RUN_LABEL="fio-perf/run=${RUN_ID}"
POD_NAME="fio-perf-${RUN_ID}"
PVC_NAME="fio-perf-pvc-${RUN_ID}"
FIO_JOBNAME="fiobench"                       # one job name across precondition+measured so files are reused
NONCOMPARABLE_REASONS=()

usage() {
  cat <<'EOF'
06-run-fio-performance-test.sh — per-host FIO storage benchmark for the CSI suite

Runs four measured FIO tests (sequential read/write 1M, random read/write 4K) on
ONE Kubernetes node at a time against a mounted filesystem, preconditioning the
dataset first. Canonical preset reproduces the validated Nebius SFS POC method.

USAGE:
  ./06-run-fio-performance-test.sh [options]

MODE:
  --mode canonical|custom      Default: canonical. Any changed canonical parameter
                               auto-downgrades to custom (results non-comparable).
  --smoke                      Reduced dataset/runtime quick check. NON-CANONICAL.

STORAGE / PVC:
  --namespace NS               Kubernetes namespace (default: context/default)
  --storage-class SC           StorageClass for a created PVC
  --existing-pvc NAME          Use an existing PVC instead of creating one
  --create-pvc auto|true|false Whether to create a temporary PVC (default: auto)
  --pvc-capacity SIZE          PVC request size (default: dataset + margin, e.g. 800Gi)
  --capacity-margin PCT        Free-space safety margin percent (default: 10)
  --mount-path PATH            Mount path inside the pod (default: /data)

NODE SCOPE (per-host):
  --target-node "N [N2 ...]"   Explicit node(s); each benchmarked SEQUENTIALLY
  --node-selector SELECTOR     Label selector to choose the node pool

FIO PARAMETERS (changing any canonical value => custom):
  --image IMG                  FIO container image (default: ubuntu:24.04, fio 3.36)
  --engine ENGINE              I/O engine (default: libaio)
  --numjobs N                  Jobs (default: 64)
  --iodepth N                  Queue depth (default: 32)
  --direct 0|1                 Direct I/O (default: 1; canonical requires 1)
  --size SIZE                  Size per job (default: 10G)
  --runtime SEC                Runtime per measured test (default: 120)
  --seq-bs BS                  Sequential block size (default: 1M)
  --rand-bs BS                 Random block size (default: 4K)
  --file-mode multi|single     File layout (default: multi; single is non-comparable)

OUTPUT / LIFECYCLE:
  --result-dir DIR             Local results dir (default: ./fio-results)
  --retain-data                Keep dataset + pod/PVC after the run (default: delete)
  --yes                        Skip the interactive confirmation (noninteractive)
  --help                       This help

ENVIRONMENT VARIABLES mirror the flags (TEST_NAMESPACE, FIO_IMAGE, FIO_ENGINE,
FIO_NUMJOBS, FIO_IODEPTH, FIO_DIRECT, FIO_SIZE, FIO_RUNTIME, FIO_SEQ_BS,
FIO_RAND_BS, FIO_FILE_MODE, STORAGE_CLASS/FILESYSTEM_DEFAULT_STORAGE_CLASS_NAME,
EXISTING_PVC, CREATE_PVC, PVC_CAPACITY, CAPACITY_MARGIN_PCT, MOUNT_PATH,
TARGET_NODE, NODE_SELECTOR, RESULT_DIR, RETAIN_DATA, CONFIRM_RUN, SMOKE).

EXAMPLES:
  # 1. Interactive canonical run (prompts before writing ~640 GiB)
  ./06-run-fio-performance-test.sh

  # 2. Noninteractive canonical run
  ./06-run-fio-performance-test.sh --yes

  # 3. Select a StorageClass
  ./06-run-fio-performance-test.sh --storage-class csi-mounted-fs-path-sc --yes

  # 4. Use an existing PVC
  ./06-run-fio-performance-test.sh --existing-pvc my-sfs-pvc --yes

  # 5. Target a specific node
  ./06-run-fio-performance-test.sh --target-node computeinstance-xxxx --yes

  # 6. Custom parameters (auto-labeled non-comparable)
  ./06-run-fio-performance-test.sh --numjobs 16 --runtime 60 --yes

  # 7. Retain the dataset for inspection
  ./06-run-fio-performance-test.sh --retain-data --yes

  # 8. Quick smoke (NON-CANONICAL — small dataset, short runtime)
  ./06-run-fio-performance-test.sh --smoke --yes

SFS POC REFERENCE (per host, multi-file, fio 3.36, 64 jobs, qd 32, direct I/O,
10 GiB/job, 640 GiB, 120s/test — observed figures, NOT guarantees):
  seq read 1M: 25.61 GB/s | seq write 1M: 19.82 GB/s
  rand read 4K: 157.4k IOPS | rand write 4K: 110.9k IOPS
EOF
}

# ----------------------------------------------------------------------------- Argument parsing
while [ "$#" -gt 0 ]; do
  case "$1" in
    --mode)            FIO_MODE="$2"; shift 2 ;;
    --smoke)           SMOKE="true"; shift ;;
    --namespace)       TEST_NAMESPACE="$2"; shift 2 ;;
    --storage-class)   STORAGE_CLASS="$2"; shift 2 ;;
    --existing-pvc)    EXISTING_PVC="$2"; shift 2 ;;
    --create-pvc)      CREATE_PVC="$2"; shift 2 ;;
    --pvc-capacity)    PVC_CAPACITY="$2"; shift 2 ;;
    --capacity-margin) CAPACITY_MARGIN_PCT="$2"; shift 2 ;;
    --mount-path)      MOUNT_PATH="$2"; shift 2 ;;
    --target-node)     TARGET_NODE="$2"; shift 2 ;;
    --node-selector)   NODE_SELECTOR="$2"; shift 2 ;;
    --image)           FIO_IMAGE="$2"; shift 2 ;;
    --engine)          FIO_ENGINE="$2"; shift 2 ;;
    --numjobs)         FIO_NUMJOBS="$2"; shift 2 ;;
    --iodepth)         FIO_IODEPTH="$2"; shift 2 ;;
    --direct)          FIO_DIRECT="$2"; shift 2 ;;
    --size)            FIO_SIZE="$2"; shift 2 ;;
    --runtime)         FIO_RUNTIME="$2"; shift 2 ;;
    --seq-bs)          FIO_SEQ_BS="$2"; shift 2 ;;
    --rand-bs)         FIO_RAND_BS="$2"; shift 2 ;;
    --file-mode)       FIO_FILE_MODE="$2"; shift 2 ;;
    --result-dir)      RESULT_DIR="$2"; shift 2 ;;
    --retain-data)     RETAIN_DATA="true"; shift ;;
    --yes|--confirm)   CONFIRM_RUN="true"; shift ;;
    --help|-h)         usage; exit 0 ;;
    *) log_fail "Unknown option: $1 (see --help)"; exit 2 ;;
  esac
done

# Smoke mode: tiny dataset + short runtime, explicitly non-canonical, so the full
# pipeline (pod, mount, fio, precondition, 4 tests, parse, report) can be checked
# without allocating 640 GiB.
if [ "$SMOKE" = "true" ]; then
  FIO_NUMJOBS="${FIO_NUMJOBS_SMOKE:-4}"
  FIO_SIZE="${FIO_SIZE_SMOKE:-1G}"
  FIO_RUNTIME="${FIO_RUNTIME_SMOKE:-15}"
fi

# ----------------------------------------------------------------------------- Helpers
# size_to_bytes: parse fio-style size (binary units: K/M/G/T = KiB/MiB/GiB/TiB).
size_to_bytes() {
  local s="$1" num unit
  num="$(printf '%s' "$s" | sed -E 's/[^0-9.].*$//')"
  unit="$(printf '%s' "$s" | sed -E 's/^[0-9.]*//' | tr '[:lower:]' '[:upper:]')"
  case "$unit" in
    ""|B)        awk -v n="$num" 'BEGIN{printf "%.0f", n}' ;;
    K|KI|KIB)    awk -v n="$num" 'BEGIN{printf "%.0f", n*1024}' ;;
    M|MI|MIB)    awk -v n="$num" 'BEGIN{printf "%.0f", n*1024*1024}' ;;
    G|GI|GIB)    awk -v n="$num" 'BEGIN{printf "%.0f", n*1024*1024*1024}' ;;
    T|TI|TIB)    awk -v n="$num" 'BEGIN{printf "%.0f", n*1024*1024*1024*1024}' ;;
    *) echo "0" ;;
  esac
}

# classify_mode: decide canonical vs custom and record WHY it's non-comparable.
classify_mode() {
  [ "$SMOKE" = "true" ]                      && NONCOMPARABLE_REASONS+=("smoke mode (reduced dataset/runtime)")
  [ "$FIO_ENGINE" != "$CANON_ENGINE" ]       && NONCOMPARABLE_REASONS+=("engine=$FIO_ENGINE (canonical $CANON_ENGINE)")
  [ "$FIO_NUMJOBS" != "$CANON_NUMJOBS" ]     && NONCOMPARABLE_REASONS+=("numjobs=$FIO_NUMJOBS (canonical $CANON_NUMJOBS)")
  [ "$FIO_IODEPTH" != "$CANON_IODEPTH" ]     && NONCOMPARABLE_REASONS+=("iodepth=$FIO_IODEPTH (canonical $CANON_IODEPTH)")
  [ "$FIO_DIRECT" != "$CANON_DIRECT" ]       && NONCOMPARABLE_REASONS+=("direct=$FIO_DIRECT (canonical $CANON_DIRECT)")
  [ "$FIO_SIZE" != "$CANON_SIZE" ]           && NONCOMPARABLE_REASONS+=("size=$FIO_SIZE (canonical $CANON_SIZE)")
  [ "$FIO_RUNTIME" != "$CANON_RUNTIME" ]     && NONCOMPARABLE_REASONS+=("runtime=$FIO_RUNTIME (canonical $CANON_RUNTIME)")
  [ "$FIO_SEQ_BS" != "$CANON_SEQ_BS" ]       && NONCOMPARABLE_REASONS+=("seq_bs=$FIO_SEQ_BS (canonical $CANON_SEQ_BS)")
  [ "$FIO_RAND_BS" != "$CANON_RAND_BS" ]     && NONCOMPARABLE_REASONS+=("rand_bs=$FIO_RAND_BS (canonical $CANON_RAND_BS)")
  [ "$FIO_FILE_MODE" != "$CANON_FILE_MODE" ] && NONCOMPARABLE_REASONS+=("file_mode=$FIO_FILE_MODE (canonical $CANON_FILE_MODE)")
  if [ "${#NONCOMPARABLE_REASONS[@]}" -gt 0 ] && [ "$FIO_MODE" = "canonical" ]; then
    FIO_MODE="custom"
  fi
}

# exec_in_pod: run a command inside the fio pod with a request timeout.
exec_in_pod() {
  kubectl exec --request-timeout=60s -n "${TEST_NAMESPACE}" "${POD_NAME}" -- bash -lc "$1"
}

CLEANED_UP=""
cleanup() {
  [ -n "${CLEANED_UP}" ] && return
  CLEANED_UP=1
  if [ "$RETAIN_DATA" = "true" ]; then
    log_step "Retaining resources (--retain-data): pod ${POD_NAME}${CREATED_PVC:+, PVC ${PVC_NAME}}, dataset at ${RUN_TEST_DIR:-<not created>}"
    log_info "Delete later with: kubectl delete pod,pvc -n ${TEST_NAMESPACE} -l ${RUN_LABEL}"
    return
  fi
  log_step "Cleaning up this run's resources"
  # Scoped to THIS run's unique label only — never touches another run's pods/PVCs,
  # never --all / --force. Dataset dir (if any) is removed from inside the pod by
  # exact path before the pod goes away.
  if [ -n "${RUN_TEST_DIR:-}" ]; then
    log_info "Removing dataset dir (exact path): ${RUN_TEST_DIR}"
    # Guarded rm: only the resolved run dir under the benchmark subdir.
    exec_in_pod "case '${RUN_TEST_DIR}' in ${MOUNT_PATH}/${BENCH_SUBDIR}/${BENCH_SUBDIR}-*) rm -rf -- '${RUN_TEST_DIR}' ;; *) echo 'refusing unexpected path'; exit 1 ;; esac" >/dev/null 2>&1 || true
  fi
  log_info "Deleting pod/PVC labelled ${RUN_LABEL}"
  kubectl delete pod,pvc -n "${TEST_NAMESPACE}" -l "${RUN_LABEL}" \
    --ignore-not-found=true --wait=false --request-timeout=30s 2>/dev/null || true
  log_pass "Cleanup complete"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# wait_for_pod_ready: block until the pod is Ready (bounded).
wait_for_pod_ready() {
  local timeout="${1:-300}" elapsed=0 phase=""
  while [ "$elapsed" -lt "$timeout" ]; do
    phase=$(kubectl get pod --request-timeout=30s -n "${TEST_NAMESPACE}" "${POD_NAME}" \
      -o jsonpath='{.status.phase}' 2>/dev/null || echo "")
    if [ "$phase" = "Running" ]; then
      if kubectl get pod --request-timeout=30s -n "${TEST_NAMESPACE}" "${POD_NAME}" \
        -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null | grep -q True; then
        return 0
      fi
    fi
    [ "$phase" = "Failed" ] && return 1
    sleep 5; elapsed=$(( elapsed + 5 ))
  done
  return 1
}

# select_nodes: resolve the list of nodes to benchmark (Ready + schedulable).
select_nodes() {
  if [ -n "$TARGET_NODE" ]; then
    printf '%s\n' $TARGET_NODE
    return
  fi
  local sel_args=()
  [ -n "$NODE_SELECTOR" ] && sel_args=(-l "$NODE_SELECTOR")
  # Ready AND schedulable (skip NotReady / cordoned) — same convention as 05.
  kubectl get nodes "${sel_args[@]}" --no-headers --request-timeout=30s \
    -o custom-columns='NAME:.metadata.name,READY:.status.conditions[?(@.type=="Ready")].status,SCHED:.spec.unschedulable' \
    2>/dev/null | awk '$2=="True" && $3!="true" {print $1}'
}

# ----------------------------------------------------------------------------- Pre-flight
require_command kubectl

classify_mode
DATASET_BYTES=$(( $(size_to_bytes "$FIO_SIZE") * FIO_NUMJOBS ))
DATASET_GIB=$(awk -v b="$DATASET_BYTES" 'BEGIN{printf "%.1f", b/1073741824}')
REQUIRED_BYTES=$(awk -v b="$DATASET_BYTES" -v m="$CAPACITY_MARGIN_PCT" 'BEGIN{printf "%.0f", b*(1+m/100)}')
REQUIRED_GIB=$(awk -v b="$REQUIRED_BYTES" 'BEGIN{printf "%.1f", b/1073741824}')
if [ -z "$PVC_CAPACITY" ]; then
  PVC_CAPACITY="$(awk -v b="$REQUIRED_BYTES" 'BEGIN{printf "%.0fGi", (b/1073741824)+1}')"
fi
# Measured-test floor: 4 tests x runtime (preconditioning/pulls/scheduling extra).
MEASURE_MIN=$(( FIO_RUNTIME * 4 ))

if [ "$FIO_FILE_MODE" != "multi" ] && [ "$FIO_FILE_MODE" != "single" ]; then
  log_fail "--file-mode must be 'multi' or 'single' (got '$FIO_FILE_MODE')"; exit 2
fi
if [ "$FIO_FILE_MODE" = "single" ]; then
  # Single-file is supported but reported separately and never compared to the
  # multi-file reference. Jobs use non-overlapping offsets (offset_increment).
  log_info "single-file mode selected — results are reported separately and are NOT comparable to the multi-file reference."
fi

# Resolve + validate the benchmark path (defense-in-depth; never root/home/mount-root).
BENCH_PARENT="${MOUNT_PATH%/}/${BENCH_SUBDIR}"
RUN_TEST_DIR="${BENCH_PARENT}/${BENCH_SUBDIR}-${RUN_ID}"
case "$RUN_TEST_DIR" in
  /|"$MOUNT_PATH"|"$MOUNT_PATH"/|*/root|*/home|*/home/*)
    log_fail "Refusing unsafe benchmark path: ${RUN_TEST_DIR}"; exit 1 ;;
esac
if [[ "$RUN_TEST_DIR" != "${BENCH_PARENT}/${BENCH_SUBDIR}-"* ]]; then
  log_fail "Resolved test dir is not under the benchmark subdir: ${RUN_TEST_DIR}"; exit 1
fi

# ----------------------------------------------------------------------------- Effective settings banner
log_step "FIO storage performance validation — effective settings"
log_info "Run ID:            ${RUN_ID}"
log_info "Mode:              ${FIO_MODE}$( [ "${#NONCOMPARABLE_REASONS[@]}" -gt 0 ] && echo "  (NON-COMPARABLE)" )"
if [ "${#NONCOMPARABLE_REASONS[@]}" -gt 0 ]; then
  for r in "${NONCOMPARABLE_REASONS[@]}"; do log_info "  non-comparable: $r"; done
fi
log_info "Namespace:         ${TEST_NAMESPACE}"
log_info "FIO image:         ${FIO_IMAGE}"
log_info "I/O engine:        ${FIO_ENGINE}   (must be async for queue depth ${FIO_IODEPTH} to be effective)"
log_info "Direct I/O:        ${FIO_DIRECT}"
log_info "Jobs x size:       ${FIO_NUMJOBS} x ${FIO_SIZE}  => dataset ${DATASET_GIB} GiB"
log_info "Queue depth:       ${FIO_IODEPTH}"
log_info "Runtime/test:      ${FIO_RUNTIME}s   (4 measured tests => >= ${MEASURE_MIN}s of measurement alone)"
log_info "Block sizes:       seq ${FIO_SEQ_BS} / rand ${FIO_RAND_BS}"
log_info "File mode:         ${FIO_FILE_MODE}"
log_info "Mount path:        ${MOUNT_PATH}"
log_info "Benchmark dir:     ${RUN_TEST_DIR}"
log_info "Storage class:     ${STORAGE_CLASS}${EXISTING_PVC:+  (overridden by existing PVC ${EXISTING_PVC})}"
log_info "Required capacity: >= ${REQUIRED_GIB} GiB (dataset + ${CAPACITY_MARGIN_PCT}% margin)"
log_info "Result dir:        ${RESULT_DIR}"
log_info "Retain data:       ${RETAIN_DATA}"
echo ""
log_info "WARNING: this writes the full dataset (${DATASET_GIB} GiB) during preconditioning,"
log_info "         then performs ADDITIONAL measured writes. Measured tests alone take"
log_info "         >= ${MEASURE_MIN}s (excluding image pull, scheduling, preconditioning, cleanup)."
if [ "$FIO_MODE" = "canonical" ]; then
  log_info "         Canonical mode requires direct I/O and fio ${CANON_FIO_VERSION}."
fi

# ----------------------------------------------------------------------------- Confirmation
if [ "$CONFIRM_RUN" != "true" ]; then
  if [ -t 0 ]; then
    read -r -p "Proceed and create the dataset? Type 'yes' to continue: " _ans || _ans=""
    [ "$_ans" = "yes" ] || { log_fail "Aborted by user."; exit 1; }
  else
    log_fail "Noninteractive shell and no confirmation given. Re-run with --yes or CONFIRM_RUN=true."
    exit 1
  fi
fi

# ----------------------------------------------------------------------------- Node scope
NODES=()
while IFS= read -r n; do [ -n "$n" ] && NODES+=("$n"); done < <(select_nodes)
if [ "${#NODES[@]}" -eq 0 ]; then
  log_fail "No Ready, schedulable node found (target='${TARGET_NODE}', selector='${NODE_SELECTOR}')."
  exit 1
fi
log_step "Node scope: ${#NODES[@]} host(s) — benchmarked ONE AT A TIME (per-host): ${NODES[*]}"
if [ "${#NODES[@]}" -gt 1 ]; then
  log_info "Each node runs the full benchmark sequentially; results are per-host (NOT an aggregate/contention test)."
fi

mkdir -p "${RESULT_DIR}"
KCTX="$(kubectl config current-context 2>/dev/null || echo unknown)"
OVERALL_RC=0

# ----------------------------------------------------------------------------- Per-host run
run_on_node() {
  local NODE="$1" idx="$2" total="$3"
  local node_tag; node_tag="$(printf '%s' "$NODE" | tr -c 'A-Za-z0-9._-' '_')"
  local outdir="${RESULT_DIR}/${RUN_ID}/${node_tag}"
  mkdir -p "$outdir"
  log_step "[$idx/$total] Benchmarking host: ${NODE}"

  # Decide PVC: existing or created-for-this-run.
  CREATED_PVC=""
  local claim="$EXISTING_PVC"
  if [ -z "$claim" ] && [ "$CREATE_PVC" != "false" ]; then
    claim="$PVC_NAME"; CREATED_PVC="yes"
    log_info "Creating temporary PVC ${PVC_NAME} (${PVC_CAPACITY}, ${STORAGE_CLASS})"
    kubectl apply -n "${TEST_NAMESPACE}" -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${PVC_NAME}
  namespace: ${TEST_NAMESPACE}
  labels:
    app.kubernetes.io/part-of: filesystem-csi-validation
    fio-perf/run: "${RUN_ID}"
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: ${STORAGE_CLASS}
  resources:
    requests:
      storage: ${PVC_CAPACITY}
EOF
  elif [ -z "$claim" ]; then
    log_fail "--create-pvc false but no --existing-pvc supplied"; return 1
  fi

  # Sleeper pod pinned to this node; installs fio, then we drive it via exec.
  log_info "Launching FIO pod ${POD_NAME} on ${NODE} (image ${FIO_IMAGE})"
  kubectl apply -n "${TEST_NAMESPACE}" -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: ${POD_NAME}
  namespace: ${TEST_NAMESPACE}
  labels:
    app.kubernetes.io/part-of: filesystem-csi-validation
    fio-perf/run: "${RUN_ID}"
spec:
  restartPolicy: Never
  nodeSelector:
    kubernetes.io/hostname: ${NODE}
  tolerations:
  - key: nvidia.com/gpu
    operator: Exists
    effect: NoSchedule
  containers:
  - name: fio
    image: ${FIO_IMAGE}
    command: ["/bin/sh","-c","sleep 100000"]
    volumeMounts:
    - name: bench
      mountPath: ${MOUNT_PATH}
    resources:
      requests:
        cpu: "4"
        memory: "8Gi"
  volumes:
  - name: bench
    persistentVolumeClaim:
      claimName: ${claim}
EOF

  if ! wait_for_pod_ready 600; then
    log_fail "FIO pod did not become Ready on ${NODE}"
    kubectl get pod --request-timeout=30s -n "${TEST_NAMESPACE}" "${POD_NAME}" 2>/dev/null || true
    kubectl describe pod --request-timeout=30s -n "${TEST_NAMESPACE}" "${POD_NAME}" 2>/dev/null | grep -A20 "Events:" || true
    return 1
  fi

  # Install fio + confirm version/engine.
  log_info "Installing fio in the pod and checking version..."
  exec_in_pod "command -v fio >/dev/null 2>&1 || { apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq fio >/dev/null; }" || {
    log_fail "Could not install fio in the pod"; return 1; }
  local fio_ver node_arch
  fio_ver="$(exec_in_pod 'fio --version' | tr -d '\r' | sed 's/^fio-//')"
  node_arch="$(exec_in_pod 'uname -m' | tr -d '\r')"
  log_info "fio version: ${fio_ver}   node arch: ${node_arch}"
  if [ "$FIO_MODE" = "canonical" ] && [ "$fio_ver" != "$CANON_FIO_VERSION" ]; then
    log_fail "Canonical mode requires fio ${CANON_FIO_VERSION} but image has ${fio_ver}. Use --image with fio ${CANON_FIO_VERSION}, or run --mode custom (version recorded)."
    return 1
  fi

  # Confirm mount is present + writable, and check free capacity from INSIDE the fs.
  exec_in_pod "mountpoint -q '${MOUNT_PATH}' || mount | grep -q ' on ${MOUNT_PATH} '" >/dev/null 2>&1 \
    || log_info "note: ${MOUNT_PATH} not reported as a distinct mountpoint (continuing; it is the PVC mount)"
  exec_in_pod "test -w '${MOUNT_PATH}'" || { log_fail "${MOUNT_PATH} is not writable"; return 1; }
  local fstype avail_bytes
  fstype="$(exec_in_pod "stat -f -c %T '${MOUNT_PATH}' 2>/dev/null || echo unknown" | tr -d '\r')"
  avail_bytes="$(exec_in_pod "df -B1 --output=avail '${MOUNT_PATH}' 2>/dev/null | tail -1 | tr -d ' '" | tr -d '\r')"
  local avail_gib; avail_gib="$(awk -v b="${avail_bytes:-0}" 'BEGIN{printf "%.1f", b/1073741824}')"
  log_info "Filesystem type: ${fstype}   available: ${avail_gib} GiB   required: ${REQUIRED_GIB} GiB"
  if [ "${avail_bytes:-0}" -lt "$REQUIRED_BYTES" ] 2>/dev/null; then
    log_fail "Insufficient free space on ${MOUNT_PATH}: ${avail_gib} GiB < ${REQUIRED_GIB} GiB required. Aborting (no dataset written)."
    return 1
  fi

  # Create the unique run dir (exact, validated path).
  exec_in_pod "mkdir -p '${RUN_TEST_DIR}'" || { log_fail "could not create ${RUN_TEST_DIR}"; return 1; }

  # Direct-I/O support probe for canonical mode.
  if [ "$FIO_MODE" = "canonical" ] && [ "$FIO_DIRECT" = "1" ]; then
    if ! exec_in_pod "fio --name=diocheck --filename='${RUN_TEST_DIR}/.diocheck' --ioengine=${FIO_ENGINE} --direct=1 --rw=write --bs=1M --size=8M --runtime=2 --time_based=0 >/dev/null 2>&1 && rm -f '${RUN_TEST_DIR}/.diocheck'"; then
      log_fail "Direct I/O is not usable on ${MOUNT_PATH}; canonical mode requires it. Aborting (not falling back to buffered)."
      return 1
    fi
  fi

  # Shared fio args. One job name across all passes so the preconditioned files
  # are REUSED by the measured tests (seq-write overwrites them, not sparse create).
  local common="--name=${FIO_JOBNAME} --directory=${RUN_TEST_DIR} --ioengine=${FIO_ENGINE} --direct=${FIO_DIRECT} --iodepth=${FIO_IODEPTH} --numjobs=${FIO_NUMJOBS} --size=${FIO_SIZE} --group_reporting=1 --thread"
  if [ "$FIO_FILE_MODE" = "single" ]; then
    # One shared file; non-overlapping regions via offset_increment.
    common="--name=${FIO_JOBNAME} --filename=${RUN_TEST_DIR}/${FIO_JOBNAME}.shared --ioengine=${FIO_ENGINE} --direct=${FIO_DIRECT} --iodepth=${FIO_IODEPTH} --numjobs=${FIO_NUMJOBS} --size=${FIO_SIZE} --offset_increment=${FIO_SIZE} --group_reporting=1 --thread"
  fi

  # 1) Preconditioning — fully write the dataset (NOT time_based, NOT a measured result).
  log_info "Preconditioning: writing ${DATASET_GIB} GiB dataset (this is NOT the measured write result)..."
  if ! exec_in_pod "fio ${common} --rw=write --bs=${FIO_SEQ_BS} --time_based=0 --output-format=json" > "${outdir}/precondition.json" 2>"${outdir}/precondition.err"; then
    log_fail "Preconditioning failed — see ${outdir}/precondition.err"
    cat "${outdir}/precondition.err" || true
    return 1
  fi
  # Verify files + dataset size are actually present.
  local nfiles dsize
  nfiles="$(exec_in_pod "ls -1 '${RUN_TEST_DIR}' | grep -c '^${FIO_JOBNAME}' || true" | tr -d '\r')"
  dsize="$(exec_in_pod "du -sb '${RUN_TEST_DIR}' 2>/dev/null | cut -f1" | tr -d '\r')"
  log_info "Preconditioned: $(awk -v b="${dsize:-0}" 'BEGIN{printf "%.1f GiB", b/1073741824}') across ${nfiles} file(s)"
  if [ "${dsize:-0}" -lt "$(awk -v b="$DATASET_BYTES" 'BEGIN{printf "%.0f", b*0.98}')" ] 2>/dev/null; then
    log_fail "Preconditioned dataset smaller than expected (${dsize} < ${DATASET_BYTES}); refusing to measure."
    return 1
  fi
  log_pass "Preconditioning complete"

  # 2-5) Measured tests.
  local -a TESTS=("seqread:read:${FIO_SEQ_BS}" "seqwrite:write:${FIO_SEQ_BS}" "randread:randread:${FIO_RAND_BS}" "randwrite:randwrite:${FIO_RAND_BS}")
  local t name rw bs
  for t in "${TESTS[@]}"; do
    name="${t%%:*}"; rw="$(echo "$t" | cut -d: -f2)"; bs="${t##*:}"
    log_info "Measured test: ${name} (rw=${rw}, bs=${bs}, ${FIO_RUNTIME}s)..."
    if ! exec_in_pod "fio ${common} --rw=${rw} --bs=${bs} --time_based=1 --runtime=${FIO_RUNTIME} --output-format=json" > "${outdir}/${name}.json" 2>"${outdir}/${name}.err"; then
      log_fail "Measured ${name} failed — see ${outdir}/${name}.err"; OVERALL_RC=1
    fi
  done

  # Collect pod logs (best-effort) for evidence.
  kubectl logs --request-timeout=30s -n "${TEST_NAMESPACE}" "${POD_NAME}" >"${outdir}/pod.log" 2>/dev/null || true

  # Build the per-host report from the JSON.
  generate_report "$NODE" "$node_arch" "$claim" "$fstype" "$avail_gib" "$fio_ver" "$outdir"
  log_pass "Host ${NODE} complete — report: ${outdir}/report.md"
}

# parse_metric: pull a value from a fio JSON file via python3 (read or write side).
# prints: bw_bytes iops clat_mean_ns p50_ns p95_ns p99_ns
parse_metric() {
  local json="$1" op="$2"
  python3 - "$json" "$op" <<'PY'
import json,sys
try:
    d=json.load(open(sys.argv[1])); op=sys.argv[2]
    j=d["jobs"][0][op]
    c=j.get("clat_ns", j.get("lat_ns", {}))
    pct=c.get("percentile", {})
    def g(k):
        return pct.get(k, 0)
    print("%d %f %f %f %f %f" % (
        j.get("bw_bytes",0), j.get("iops",0.0),
        c.get("mean",0.0), g("50.000000"), g("95.000000"), g("99.000000")))
except Exception as e:
    print("0 0 0 0 0 0")
PY
}

# fmt_lat: ns -> human (ns/us/ms).
fmt_lat() { awk -v n="$1" 'BEGIN{ if(n>=1e6) printf "%.2f ms", n/1e6; else if(n>=1e3) printf "%.1f us", n/1e3; else printf "%.0f ns", n; }'; }

generate_report() {
  local NODE="$1" ARCH="$2" CLAIM="$3" FSTYPE="$4" AVAIL="$5" FIOVER="$6" outdir="$7"
  local report="${outdir}/report.md"
  # Primary metrics per test.
  read -r sr_bw sr_iops sr_mean sr_p50 sr_p95 sr_p99 < <(parse_metric "${outdir}/seqread.json" read)
  read -r sw_bw sw_iops sw_mean sw_p50 sw_p95 sw_p99 < <(parse_metric "${outdir}/seqwrite.json" write)
  read -r rr_bw rr_iops rr_mean rr_p50 rr_p95 rr_p99 < <(parse_metric "${outdir}/randread.json" read)
  read -r rw_bw rw_iops rw_mean rw_p50 rw_p95 rw_p99 < <(parse_metric "${outdir}/randwrite.json" write)

  gbs() { awk -v b="$1" 'BEGIN{printf "%.2f", b/1000000000}'; }       # decimal GB/s
  gibs() { awk -v b="$1" 'BEGIN{printf "%.2f", b/1073741824}'; }      # binary GiB/s
  kiops() { awk -v i="$1" 'BEGIN{printf "%.1f", i/1000}'; }           # decimal kIOPS

  local comparable="yes"; [ "${#NONCOMPARABLE_REASONS[@]}" -gt 0 ] && comparable="NO"
  # Only compare to the SFS reference when the target actually IS SFS.
  local sfs_target="no"
  case "${IS_SFS}" in
    true)  sfs_target="yes" ;;
    false) sfs_target="no" ;;
    *) case "$STORAGE_CLASS" in *mounted-fs-path*|*sfs*|*shared-fs*) sfs_target="yes" ;; esac ;;
  esac
  {
    echo "# FIO Storage Performance — ${NODE}"
    echo ""
    echo "- Timestamp: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    echo "- Run ID: ${RUN_ID}"
    echo "- Kubernetes context: ${KCTX}"
    echo "- Namespace: ${TEST_NAMESPACE}"
    echo "- Node: ${NODE}  (arch ${ARCH})"
    echo "- PVC: ${CLAIM}   StorageClass: ${STORAGE_CLASS}"
    echo "- Filesystem type: ${FSTYPE}   Mount: ${MOUNT_PATH}   Available before run: ${AVAIL} GiB"
    echo "- FIO image: ${FIO_IMAGE}   FIO version: ${FIOVER}   Engine: ${FIO_ENGINE}   Direct I/O: ${FIO_DIRECT}"
    echo "- Jobs: ${FIO_NUMJOBS}   Queue depth: ${FIO_IODEPTH}   Size/job: ${FIO_SIZE}   Dataset: ${DATASET_GIB} GiB   Runtime: ${FIO_RUNTIME}s"
    echo "- Block sizes: seq ${FIO_SEQ_BS} / rand ${FIO_RAND_BS}   File mode: ${FIO_FILE_MODE}"
    echo "- Mode: **${FIO_MODE}**   Target is SFS: **${sfs_target}**   Comparable to SFS reference: **$( [ "$comparable" = yes ] && [ "$sfs_target" = yes ] && echo yes || echo NO )**"
    if [ "${#NONCOMPARABLE_REASONS[@]}" -gt 0 ]; then
      echo "- Non-comparable because:"; for r in "${NONCOMPARABLE_REASONS[@]}"; do echo "    - $r"; done
    fi
    echo "- Preconditioning: completed (${DATASET_GIB} GiB written before measurement)"
    echo "- Execution: $( [ "$OVERALL_RC" -eq 0 ] && echo success || echo "completed with errors (see *.err)" )"
    echo "- Test data: $( [ "$RETAIN_DATA" = true ] && echo retained || echo "cleaned up" )"
    echo "- Raw output + pod log: ${outdir}/"
    echo ""
    echo "## Results (per host)"
    echo ""
    echo "| Test | Primary | Throughput | IOPS | avg lat | p50 | p95 | p99 |"
    echo "|---|---|---|---|---|---|---|---|"
    echo "| Sequential read (${FIO_SEQ_BS}) | **$(gbs "$sr_bw") GB/s** ($(gibs "$sr_bw") GiB/s) | $(gbs "$sr_bw") GB/s | $(kiops "$sr_iops")k | $(fmt_lat "$sr_mean") | $(fmt_lat "$sr_p50") | $(fmt_lat "$sr_p95") | $(fmt_lat "$sr_p99") |"
    echo "| Sequential write (${FIO_SEQ_BS}) | **$(gbs "$sw_bw") GB/s** ($(gibs "$sw_bw") GiB/s) | $(gbs "$sw_bw") GB/s | $(kiops "$sw_iops")k | $(fmt_lat "$sw_mean") | $(fmt_lat "$sw_p50") | $(fmt_lat "$sw_p95") | $(fmt_lat "$sw_p99") |"
    echo "| Random read (${FIO_RAND_BS}) | **$(kiops "$rr_iops")k IOPS** | $(gbs "$rr_bw") GB/s | $(kiops "$rr_iops")k | $(fmt_lat "$rr_mean") | $(fmt_lat "$rr_p50") | $(fmt_lat "$rr_p95") | $(fmt_lat "$rr_p99") |"
    echo "| Random write (${FIO_RAND_BS}) | **$(kiops "$rw_iops")k IOPS** | $(gbs "$rw_bw") GB/s | $(kiops "$rw_iops")k | $(fmt_lat "$rw_mean") | $(fmt_lat "$rw_p50") | $(fmt_lat "$rw_p95") | $(fmt_lat "$rw_p99") |"
    echo ""
    echo "Throughput shown as decimal GB/s (bytes/1e9) and binary GiB/s (bytes/2^30). IOPS in decimal kIOPS."
    echo ""
    if [ "$comparable" = "yes" ] && [ "$sfs_target" = "yes" ]; then
      echo "## vs SFS POC reference (observed, not a guarantee)"
      echo ""
      echo "| Metric | This host | SFS reference | Δ |"
      echo "|---|---|---|---|"
      echo "| seq read 1M | $(gbs "$sr_bw") GB/s | 25.61 GB/s | $(awk -v a="$(gbs "$sr_bw")" 'BEGIN{printf "%+.1f%%", (a-25.61)/25.61*100}') |"
      echo "| seq write 1M | $(gbs "$sw_bw") GB/s | 19.82 GB/s | $(awk -v a="$(gbs "$sw_bw")" 'BEGIN{printf "%+.1f%%", (a-19.82)/19.82*100}') |"
      echo "| rand read 4K | $(kiops "$rr_iops")k IOPS | 157.4k IOPS | $(awk -v a="$(kiops "$rr_iops")" 'BEGIN{printf "%+.1f%%", (a-157.4)/157.4*100}') |"
      echo "| rand write 4K | $(kiops "$rw_iops")k IOPS | 110.9k IOPS | $(awk -v a="$(kiops "$rw_iops")" 'BEGIN{printf "%+.1f%%", (a-110.9)/110.9*100}') |"
      echo ""
      echo "_Reference is Nebius SFS/data-fs, per host, multi-file, fio 3.36, 64 jobs, qd 32, direct I/O,"
      echo "10 GiB/job, 640 GiB, 120s/test. Below-reference is NOT a failure — correctness and performance are separate._"
    else
      if [ "$sfs_target" != "yes" ]; then
        echo "_Target is not Nebius SFS (StorageClass: ${STORAGE_CLASS}); SFS reference comparison intentionally omitted._"
      else
        echo "_Mode is non-comparable (custom/smoke/single-file); SFS reference comparison intentionally omitted._"
      fi
    fi
  } > "$report"
  echo ""
  cat "$report"
}

# ----------------------------------------------------------------------------- Drive per-host (sequential)
i=0
for NODE in "${NODES[@]}"; do
  i=$(( i + 1 ))
  if ! run_on_node "$NODE" "$i" "${#NODES[@]}"; then
    log_fail "Benchmark failed on ${NODE}"
    OVERALL_RC=1
  fi
  # Between hosts: clean this run's pod (+PVC if created) unless retaining, so the
  # next host starts fresh. Dataset dir removed by exact path inside the pod first.
  if [ "$RETAIN_DATA" != "true" ] && [ "$i" -lt "${#NODES[@]}" ]; then
    exec_in_pod "case '${RUN_TEST_DIR}' in ${MOUNT_PATH}/${BENCH_SUBDIR}/${BENCH_SUBDIR}-*) rm -rf -- '${RUN_TEST_DIR}' ;; esac" >/dev/null 2>&1 || true
    kubectl delete pod,pvc -n "${TEST_NAMESPACE}" -l "${RUN_LABEL}" --ignore-not-found=true --wait=true --timeout=120s --request-timeout=30s 2>/dev/null || true
  fi
done

log_step "FIO performance validation finished"
log_info "Hosts tested: ${#NODES[@]}   Result root: ${RESULT_DIR}/${RUN_ID}/"
[ "$OVERALL_RC" -eq 0 ] && log_pass "All measured tests executed" || log_fail "One or more measured tests reported errors (see *.err)"
exit "$OVERALL_RC"
