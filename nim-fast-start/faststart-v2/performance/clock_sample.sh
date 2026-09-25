#!/usr/bin/env bash

# Build the v3 pre-T0 clock contract.  A Ready, digest-pinned Python artifact
# holder on the target node supplies one CLOCK_BOOTTIME anchor.  Controller UTC
# is retained for the historical metric, while controller CLOCK_MONOTONIC proves
# admission/anchor/T0 ordering without trusting wall-clock continuity.

readonly CONTROLLER_CLOCK_BOUNDARY_SCHEMA="archvteams.nebius.ai/controller-clock-boundary/v1"
readonly BOOT_TIME_ANCHOR_SCHEMA="archvteams.nebius.ai/node-boot-time-anchor/v1"
readonly BOOT_TIME_ANCHOR_HOLDER_IMAGE="docker.io/library/python@sha256:356b0d18f9385f4bdcc673af60e1e64c9d1504952e4ec36ee32044c722a6bc4e"

controller_clock_observation() {
  python3 -c '
import json
import time
from datetime import UTC, datetime
print(json.dumps({
    "utc": datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
    "monotonic_ns": time.monotonic_ns(),
}, sort_keys=True, separators=(",", ":")))
'
}

capture_controller_clock_boundary() {
  local phase=$1 output=$2 partial observation
  partial="${output}.partial"
  observation=$(controller_clock_observation) || return 1
  jq -n \
    --arg schema "$CONTROLLER_CLOCK_BOUNDARY_SCHEMA" \
    --arg phase "$phase" \
    --argjson observation "$observation" \
    '{schema:$schema,phase:$phase,utc:$observation.utc,
      monotonic_ns:$observation.monotonic_ns}' > "$partial" || return 1
  mv -- "$partial" "$output"
}

capture_boot_time_anchor() {
  local kubectl_array_name=$1 sampled_pod_name=$2 sampled_pod_uid=$3
  local target_node=$4 sampled_container=$5 output=$6
  local -n anchor_kubectl=$kubectl_array_name
  local controller_before controller_after node_observed partial
  local node_program
  partial="${output}.partial"
  node_program='import json, math, pathlib, re, time
lines=pathlib.Path("/proc/self/timens_offsets").read_text(encoding="ascii").splitlines()
offsets=[]
for line in lines:
    fields=line.split()
    if len(fields)!=3 or fields[0] not in {"monotonic","boottime"}:
        raise SystemExit("malformed timens_offsets")
    offsets.append({"clock":fields[0],"seconds":int(fields[1]),"nanoseconds":int(fields[2])})
if [item["clock"] for item in offsets] != ["monotonic","boottime"]:
    raise SystemExit("incomplete timens_offsets")
boot_id=pathlib.Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",boot_id) is None:
    raise SystemExit("invalid boot_id")
resolution_ns=math.ceil(time.clock_getres(time.CLOCK_BOOTTIME)*1000000000)
if not 1 <= resolution_ns <= 1000000:
    raise SystemExit("invalid CLOCK_BOOTTIME resolution")
print(json.dumps({"schema":"archvteams.nebius.ai/semantic-node-boottime/v1","clock_id":"CLOCK_BOOTTIME","boottime_ns":time.clock_gettime_ns(time.CLOCK_BOOTTIME),"clock_resolution_ns":resolution_ns,"boot_id":boot_id,"timens_offsets":offsets},sort_keys=True,separators=(",",":")))'

  controller_before=$(controller_clock_observation) || return 1
  node_observed=$(
    "${anchor_kubectl[@]}" exec "pod/$sampled_pod_name" \
      -c "$sampled_container" -- python3 -c "$node_program"
  ) || return 1
  controller_after=$(controller_clock_observation) || return 1
  jq -n \
    --arg schema "$BOOT_TIME_ANCHOR_SCHEMA" \
    --arg sampled_pod_name "$sampled_pod_name" \
    --arg sampled_pod_uid "$sampled_pod_uid" \
    --arg target_node "$target_node" \
    --arg sampled_container "$sampled_container" \
    --arg expected_holder_image "$BOOT_TIME_ANCHOR_HOLDER_IMAGE" \
    --argjson controller_before "$controller_before" \
    --argjson node_observed "$node_observed" \
    --argjson controller_after "$controller_after" \
    '{schema:$schema,phase:"pre-t0-anchor",sampled_pod_name:$sampled_pod_name,
      sampled_pod_uid:$sampled_pod_uid,target_node:$target_node,
      sampled_container:$sampled_container,
      expected_holder_image:$expected_holder_image,
      controller_before:$controller_before,node_observed:$node_observed,
      controller_after:$controller_after}' > "$partial" || return 1
  mv -- "$partial" "$output"
}
