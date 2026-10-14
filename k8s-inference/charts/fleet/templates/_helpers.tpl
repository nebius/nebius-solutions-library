{{/* The region entry whose id is .Values.cluster (empty dict on the control cluster). */}}
{{- define "fleet.region" -}}
{{- $out := dict -}}
{{- range $name, $r := .Values.fleet.regions -}}
{{- if eq $r.id $.Values.cluster }}{{ $_ := set $out "name" $name }}{{ $_ := set $out "r" $r }}{{ end -}}
{{- end -}}
{{- $out | toYaml -}}
{{- end -}}

{{- define "fleet.isControl" -}}
{{- if eq .Values.cluster .Values.fleet.control.id }}true{{ end -}}
{{- end -}}

{{/* "1gpu-16vcpu-200gb" -> {gpus, vcpu, memGi} */}}
{{- define "fleet.preset" -}}
{{- $parts := regexSplit "[^0-9]+" . -1 -}}
{{- if lt (len $parts) 3 }}{{ fail (printf "preset %q is not <n>gpu-<n>vcpu-<n>gb" .) }}{{ end -}}
{{- dict "gpus" (atoi (index $parts 0)) "vcpu" (atoi (index $parts 1)) "memGi" (atoi (index $parts 2)) | toYaml -}}
{{- end -}}

{{/* USD per GPU-hour of a pool: reserved -> reserved_marginal, on_demand -> list, spot -> max_price cap or list. args: dict "fleet" "pool" */}}
{{- define "fleet.price" -}}
{{- $f := .fleet }}{{ $p := .pool -}}
{{- $cap := default (dict "type" "on_demand") $p.capacity -}}
{{- $pl := index $f.prices $p.platform | default dict -}}
{{- if eq $cap.type "reserved" -}}{{ default 0 $f.prices.reserved_marginal | float64 | printf "%.4f" -}}
{{- else if eq $cap.type "spot" -}}{{ (default (default 0 $pl.spot) $cap.max_price) | float64 | printf "%.4f" -}}
{{- else -}}{{ default 0 $pl.on_demand | float64 | printf "%.4f" -}}
{{- end -}}
{{- end -}}

{{/* InfiniBand NICs a pod can claim per node of a pool through DRA (docs/JOBS.md "Multi-node runs"): 0 unless the pool is
     in a GPU cluster; H100/H200/B200/B300 full nodes expose 8 fabric NICs (ib0..7; B200/B300 mlx5_4..11), GB300 4
     (docs.nebius.com "DeepSeek recipe": NIC ranges per GPU type). Override per pool with `ib_devices_per_node`. */}}
{{- define "fleet.ibDevices" -}}
{{- if ne (default "none" .interconnect) "infiniband" }}0{{ else if .ib_devices_per_node }}{{ .ib_devices_per_node }}{{ else if hasPrefix "gpu-gb300" .platform }}4{{ else }}8{{ end -}}
{{- end -}}

{{/* Kueue quota of a pool: capacity minus the warm-endpoint floor and the per-node reserve. args: dict "fleet" "pool" */}}
{{- define "fleet.quota" -}}
{{- $f := .fleet }}{{ $p := .pool -}}
{{- $s := include "fleet.preset" $p.preset | fromYaml -}}
{{- $floor := int (default 0 $p.endpoint_floor_gpus) -}}
{{- $nodesForFloor := 0 -}}
{{- if gt $floor 0 }}{{ $nodesForFloor = div (add $floor (sub $s.gpus 1)) $s.gpus }}{{ end -}}
{{- $nodes := sub (int $p.max_nodes) $nodesForFloor -}}
{{- $res := default dict $f.node_reserve -}}
{{- $eph := include "fleet.ephemeralPerNode" (dict "fleet" $f "boot" (default 512 $p.boot_disk_gib)) | int -}}
{{- dict "gpu" (sub (mul $s.gpus (int $p.max_nodes)) $floor) "cpu" (mul (sub $s.vcpu (default 2 $res.cpu)) $nodes) "memoryGi" (mul (sub $s.memGi (default 20 $res.memory_gib)) $nodes) "ib" (mul (int (include "fleet.ibDevices" $p)) $nodes) "ephemeralGi" (mul $eph $nodes) | toYaml -}}
{{- end -}}

{{/* Ephemeral storage one node of a pool may admit (Gi): boot disk x node_reserve.ephemeral_fraction minus
     node_reserve.ephemeral_gib, never below zero: under the kubelet's allocatable (docs/FLEET.md "Ephemeral storage").
     args: dict "fleet" "boot" (GiB) */}}
{{- define "fleet.ephemeralPerNode" -}}
{{- $res := default dict .fleet.node_reserve -}}
{{- $fraction := 0.85 }}{{ if hasKey $res "ephemeral_fraction" }}{{ $fraction = $res.ephemeral_fraction }}{{ end -}}
{{- $reserve := 4 }}{{ if hasKey $res "ephemeral_gib" }}{{ $reserve = $res.ephemeral_gib }}{{ end -}}
{{- $gi := sub (int (floor (mulf (float64 .boot) ($fraction | float64)))) (int $reserve) -}}
{{- max 0 $gi -}}
{{- end -}}

{{/* Kueue quota of a region's system pool for the CPU-only `system` flavor: node_count x (preset minus the per-node
     system_reserve), never below zero. The preset is `<n>vcpu-<n>gb`. args: dict "fleet" "system_pool" */}}
{{- define "fleet.systemQuota" -}}
{{- $f := .fleet }}{{ $sp := default dict .system_pool -}}
{{- $parts := regexSplit "[^0-9]+" (default "8vcpu-32gb" $sp.preset) -1 -}}
{{- $nums := list }}{{ range $x := $parts }}{{ if ne $x "" }}{{ $nums = append $nums (atoi $x) }}{{ end }}{{ end -}}
{{- $vcpu := index $nums (sub (len $nums) 2) }}{{ $memGi := last $nums -}}
{{- $res := default (dict "cpu" 4 "memory_gib" 16) $f.system_reserve -}}
{{- $n := int (default 2 $sp.node_count) -}}
{{- $eph := include "fleet.ephemeralPerNode" (dict "fleet" $f "boot" (default 128 $f.system_boot_disk_gib)) | int -}}
{{- dict "cpu" (max 0 (mul (sub $vcpu (int $res.cpu)) $n)) "memoryGi" (max 0 (mul (sub $memGi (int $res.memory_gib)) $n)) "ephemeralGi" (mul $eph $n) | toYaml -}}
{{- end -}}

{{/* A CPU pool's quota (docs/FLEET.md "CPU pools"): max_nodes x preset `<n>vcpu-<n>gb` minus the per-node system_reserve,
     ephemeral storage per node from its boot disk. args: dict "fleet" "pool" */}}
{{- define "fleet.cpuPoolQuota" -}}
{{- $f := .fleet }}{{ $p := .pool -}}
{{- $parts := regexSplit "[^0-9]+" $p.preset -1 -}}
{{- $nums := list }}{{ range $x := $parts }}{{ if ne $x "" }}{{ $nums = append $nums (atoi $x) }}{{ end }}{{ end -}}
{{- if lt (len $nums) 2 }}{{ fail (printf "cpu pool preset %q is not <n>vcpu-<n>gb" $p.preset) }}{{ end -}}
{{- $vcpu := index $nums (sub (len $nums) 2) }}{{ $memGi := last $nums -}}
{{- $res := default (dict "cpu" 4 "memory_gib" 16) $f.system_reserve -}}
{{- $n := int $p.max_nodes -}}
{{- $eph := include "fleet.ephemeralPerNode" (dict "fleet" $f "boot" (default 128 $p.boot_disk_gib)) | int -}}
{{- dict "cpu" (max 0 (mul (sub $vcpu (int $res.cpu)) $n)) "memoryGi" (max 0 (mul (sub $memGi (int $res.memory_gib)) $n)) "ephemeralGi" (mul $eph $n) | toYaml -}}
{{- end -}}

{{/* The GPU-free flavor entries of a queue, first in its flavor list: `system` (the cluster's system nodes), then the
     CPU pools in name order (a workload that fits the system nodes stays there; a bigger one takes a CPU pool, which
     scales from zero); nominal 0 GPUs, so a GPU request moves on to the pool flavors. args: dict "systems" (flavor name
     -> quota) "order" (list of flavor names in order) "lender" (bool: nominal quota; else a tenant queue borrowing it) */}}
{{- define "fleet.systemFlavors" -}}
{{- $systems := .systems }}{{ $order := default (keys $systems | sortAlpha) .order }}
{{- range $name := $order }}{{ $q := index $systems $name }}
- name: {{ $name }}
  resources:
    {{- if $.lender }}
    - { name: nvidia.com/gpu, nominalQuota: 0 }
    - { name: cpu, nominalQuota: {{ $q.cpu }} }
    - { name: memory, nominalQuota: {{ $q.memoryGi }}Gi }
    - { name: ephemeral-storage, nominalQuota: {{ $q.ephemeralGi }}Gi }
    - { name: nebius.ai/infiniband, nominalQuota: 0 }
    {{- else }}
    - { name: nvidia.com/gpu, nominalQuota: 0, borrowingLimit: 0 }
    - { name: cpu, nominalQuota: 0, borrowingLimit: {{ $q.cpu }} }
    - { name: memory, nominalQuota: 0, borrowingLimit: {{ $q.memoryGi }}Gi }
    - { name: ephemeral-storage, nominalQuota: 0, borrowingLimit: {{ $q.ephemeralGi }}Gi }
    - { name: nebius.ai/infiniband, nominalQuota: 0, borrowingLimit: 0 }
    {{- end }}
{{- end }}
{{- end -}}

{{/* Index of a pool's capacity type in fleet.capacity_order (unknown types last). args: dict "fleet" "pool" */}}
{{- define "fleet.capIndex" -}}
{{- $t := (default (dict "type" "on_demand") .pool.capacity).type -}}
{{- $i := 9 }}{{ range $n, $c := default (list "reserved" "on_demand" "spot") .fleet.capacity_order }}{{ if eq $c $t }}{{ $i = $n }}{{ end }}{{ end -}}
{{- $i -}}
{{- end -}}

{{/* Ordered pool list for a preference. args: dict "fleet" "pools" (map name->pool, names may be region-prefixed) "prefer" (gpu class or "") "regionOf" (map name->region id, optional)
     Order: preferred class first; inside a class reserved -> on_demand -> spot (fleet.capacity_order), then the smallest preset (a 1-GPU run must not boot an 8-GPU node), then price; other classes by their cheapest price. Returns a YAML list of names. */}}
{{- define "fleet.orderedPools" -}}
{{- $f := .fleet }}{{ $pools := .pools }}{{ $prefer := .prefer -}}
{{- $minByClass := dict -}}
{{- range $n, $p := $pools -}}
{{- $pr := include "fleet.price" (dict "fleet" $f "pool" $p) -}}
{{- $c := default "unknown" $p.gpu_class -}}
{{- if or (not (hasKey $minByClass $c)) (lt ($pr | float64) ((index $minByClass $c) | float64)) }}{{ $_ := set $minByClass $c $pr }}{{ end -}}
{{- end -}}
{{- $keys := list -}}
{{- range $n, $p := $pools -}}
{{- $c := default "unknown" $p.gpu_class -}}
{{- $rank := 1 }}{{ if eq $c $prefer }}{{ $rank = 0 }}{{ end -}}
{{- $keys = append $keys (printf "%d|%010.4f|%s|%d|%03d|%010.4f|%s" $rank ((index $minByClass $c) | float64) $c (include "fleet.capIndex" (dict "fleet" $f "pool" $p) | atoi) (include "fleet.preset" $p.preset | fromYaml).gpus ((include "fleet.price" (dict "fleet" $f "pool" $p)) | float64) $n) -}}
{{- end -}}
{{- $names := list -}}
{{- range $k := sortAlpha $keys }}{{ $names = append $names (last (splitList "|" $k)) }}{{ end -}}
{{- $names | toYaml -}}
{{- end -}}

{{/* Sorted unique GPU classes of a pool map (alphabetical). args: pools map */}}
{{- define "fleet.classes" -}}
{{- $cs := list }}{{ range $n, $p := . }}{{ $cs = append $cs (default "unknown" $p.gpu_class) }}{{ end -}}
{{- $cs | uniq | sortAlpha | toYaml -}}
{{- end -}}

{{/* All pools of the fleet keyed "<region id>-<pool>" (manager view). args: fleet */}}
{{- define "fleet.allPools" -}}
{{- $out := dict -}}
{{- range $rn, $r := .regions }}{{ range $pn, $p := $r.pools }}{{ $_ := set $out (printf "%s-%s" $r.id $pn) (merge (dict "region" $r.id "pool" $pn "project" $r.project) $p) }}{{ end }}{{ end -}}
{{- $out | toYaml -}}
{{- end -}}

{{/* Region ids ordered for a preference (manager MultiKueueConfig): regions that have the preferred class first,
     by their cheapest pool of that class; then the other regions by their cheapest pool. args: dict "fleet" "prefer" */}}
{{- define "fleet.orderedRegions" -}}
{{- $f := .fleet }}{{ $prefer := .prefer -}}
{{- $keys := list -}}
{{- range $rn, $r := $f.regions -}}
{{- $best := 999999.0 }}{{ $has := 1 -}}
{{- range $pn, $p := $r.pools -}}
{{- $pr := include "fleet.price" (dict "fleet" $f "pool" $p) | float64 -}}
{{- if and $prefer (eq (default "unknown" $p.gpu_class) $prefer) -}}
{{- if eq $has 1 }}{{ $has = 0 }}{{ $best = $pr }}{{ else if lt $pr $best }}{{ $best = $pr }}{{ end -}}
{{- else if and (eq $has 1) (lt $pr $best) }}{{ $best = $pr }}{{ end -}}
{{- end -}}
{{- $keys = append $keys (printf "%d|%012.4f|%s" $has $best $r.id) -}}
{{- end -}}
{{- $ids := list -}}
{{- range $k := sortAlpha $keys }}{{ $ids = append $ids (last (splitList "|" $k)) }}{{ end -}}
{{- $ids | toYaml -}}
{{- end -}}

{{- define "fleet.labels" -}}
app.kubernetes.io/name: fleet
app.kubernetes.io/part-of: serverless2
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* The tenant a pool is dedicated to (fleet.tenants.<name>.dedicated_pools, bare pool names), or "". args: dict "fleet" "pool" (bare name) */}}
{{- define "fleet.poolTenant" -}}
{{- $t := "" }}{{ range $tn, $tc := default dict .fleet.tenants }}{{ if has $.pool (default list $tc.dedicated_pools) }}{{ $t = $tn }}{{ end }}{{ end -}}
{{- $t -}}
{{- end -}}

{{/* The flavor resources of one pool for a tenant's queue: a dedicated pool is the tenant's nominal quota (lent to nobody), a
     shared pool is borrowed from the fleet's `default` queue up to the pool's capacity, capped by the tenant's gpu_quota.
     args: dict "q" (fleet.quota) "dedicated" (bool) "cap" (gpu cap or 0) */}}
{{- define "fleet.tenantFlavorResources" -}}
{{- $q := .q }}{{ $gpu := int $q.gpu }}{{ if and (gt (int .cap) 0) (lt (int .cap) $gpu) }}{{ $gpu = int .cap }}{{ end }}
{{- if .dedicated }}
- { name: nvidia.com/gpu, nominalQuota: {{ $q.gpu }}, lendingLimit: 0 }
- { name: cpu, nominalQuota: {{ $q.cpu }}, lendingLimit: 0 }
- { name: memory, nominalQuota: {{ $q.memoryGi }}Gi, lendingLimit: 0 }
- { name: ephemeral-storage, nominalQuota: {{ $q.ephemeralGi }}Gi, lendingLimit: 0 }
- { name: nebius.ai/infiniband, nominalQuota: {{ $q.ib }}, lendingLimit: 0 }
{{- else }}
- { name: nvidia.com/gpu, nominalQuota: 0, borrowingLimit: {{ $gpu }} }
- { name: cpu, nominalQuota: 0, borrowingLimit: {{ $q.cpu }} }
- { name: memory, nominalQuota: 0, borrowingLimit: {{ $q.memoryGi }}Gi }
- { name: ephemeral-storage, nominalQuota: 0, borrowingLimit: {{ $q.ephemeralGi }}Gi }
- { name: nebius.ai/infiniband, nominalQuota: 0, borrowingLimit: {{ $q.ib }} }
{{- end }}
{{- end -}}

{{/* A tenant's ClusterQueue for one profile (docs/FLEET.md "Tenants"): nominal 0 on the shared pools, borrowing up to the pool
     (capped by gpu_quota), the tenant's dedicated pools as its own nominal quota, lent to nobody; the fair-share weight.
     Fair sharing replaces the classical borrowWithinCohort policy (Kueue ignores it under fair sharing); priority classes
     still order work inside the queue. args: dict "root" "fleet" "tenant" (name) "tc" (tenant config) "prefer" (class or "")
     "pools" (flavor name -> pool), "bare" (flavor name -> bare pool name), "admissionCheck" (name or "") */}}
{{- define "fleet.tenantQueue" -}}
{{- $f := .fleet }}{{ $tn := .tenant }}{{ $tc := .tc }}{{ $prefer := .prefer -}}
{{- $profile := "default" }}{{ if $prefer }}{{ $profile = printf "prefer-%s" $prefer }}{{ end }}
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: {{ $tn }}-{{ $profile }}
  labels:
    {{- include "fleet.labels" .root | nindent 4 }}
    serverless2.nebius/tenant: {{ $tn | quote }}
    {{- if $prefer }}
    serverless2.nebius/prefer: {{ $prefer | quote }}
    {{- end }}
spec:
  cohortName: {{ .root.Values.cohort }}
  namespaceSelector:
    matchLabels: { kubernetes.io/metadata.name: tenant-{{ $tn }} }
  queueingStrategy: BestEffortFIFO
  fairSharing:
    weight: {{ default 1 $tc.fair_share_weight | quote }}
  {{- with .admissionCheck }}
  admissionChecksStrategy:
    admissionChecks:
      - name: {{ . }}
  {{- end }}
  flavorFungibility:
    whenCanBorrow: MayStopSearch   # take the first flavor with free cohort capacity
    whenCanPreempt: TryNextFlavor  # ... before preempting on a preferred one
  preemption:
    withinClusterQueue: LowerPriority     # customer-batch evicts bulk-backfill
    reclaimWithinCohort: Any              # take back nominal quota (dedicated pools) lent out
  resourceGroups:
    - coveredResources: [nvidia.com/gpu, cpu, memory, ephemeral-storage, nebius.ai/infiniband]   # the last: DRA InfiniBand NICs (kueue values deviceClassMappings)
      flavors:
        {{- include "fleet.systemFlavors" (dict "systems" (default dict .systems) "order" .order "lender" false) | nindent 8 }}
        {{- range $name := include "fleet.orderedPools" (dict "fleet" $f "pools" .pools "prefer" $prefer) | fromYamlArray }}
        {{- $owner := include "fleet.poolTenant" (dict "fleet" $f "pool" (index $.bare $name)) }}
        {{- if or (not $owner) (eq $owner $tn) }}
        {{- $q := include "fleet.quota" (dict "fleet" $f "pool" (index $.pools $name)) | fromYaml }}
        - name: {{ $name }}
          resources:
            {{- include "fleet.tenantFlavorResources" (dict "q" $q "dedicated" (eq $owner $tn) "cap" (default 0 $tc.gpu_quota)) | nindent 12 }}
        {{- end }}
        {{- end }}
{{- end -}}

{{/* The fleet's lender: the nominal quota of every shared pool. The platform's own LocalQueue `models/endpoints` (fleet-placed
     endpoints) is the only one that queues here; the tenants' queues borrow from it. args: dict "root" "fleet" "pools" "bare" "orderPools" */}}
{{- define "fleet.lenderQueue" -}}
{{- $f := .fleet }}
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: default
  labels:
    {{- include "fleet.labels" .root | nindent 4 }}
spec:
  cohortName: {{ .root.Values.cohort }}
  namespaceSelector:
    matchLabels: { kubernetes.io/metadata.name: models }
  queueingStrategy: BestEffortFIFO
  resourceGroups:
    - coveredResources: [nvidia.com/gpu, cpu, memory, ephemeral-storage, nebius.ai/infiniband]
      flavors:
        {{- include "fleet.systemFlavors" (dict "systems" (default dict .systems) "order" .order "lender" true) | nindent 8 }}
        {{- range $name := include "fleet.orderedPools" (dict "fleet" $f "pools" .pools "prefer" "") | fromYamlArray }}
        {{- if not (include "fleet.poolTenant" (dict "fleet" $f "pool" (index $.bare $name))) }}
        {{- $q := include "fleet.quota" (dict "fleet" $f "pool" (index $.pools $name)) | fromYaml }}
        - name: {{ $name }}
          resources:
            - { name: nvidia.com/gpu, nominalQuota: {{ $q.gpu }} }
            - { name: cpu, nominalQuota: {{ $q.cpu }} }
            - { name: memory, nominalQuota: {{ $q.memoryGi }}Gi }
            - { name: ephemeral-storage, nominalQuota: {{ $q.ephemeralGi }}Gi }
            - { name: nebius.ai/infiniband, nominalQuota: {{ $q.ib }} }
        {{- end }}
        {{- end }}
{{- end -}}
