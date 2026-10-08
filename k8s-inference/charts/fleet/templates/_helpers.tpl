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
{{- dict "gpu" (sub (mul $s.gpus (int $p.max_nodes)) $floor) "cpu" (mul (sub $s.vcpu (default 2 $res.cpu)) $nodes) "memoryGi" (mul (sub $s.memGi (default 20 $res.memory_gib)) $nodes) "ib" (mul (int (include "fleet.ibDevices" $p)) $nodes) | toYaml -}}
{{- end -}}

{{/* Index of a pool's capacity type in fleet.capacity_order (unknown types last). args: dict "fleet" "pool" */}}
{{- define "fleet.capIndex" -}}
{{- $t := (default (dict "type" "on_demand") .pool.capacity).type -}}
{{- $i := 9 }}{{ range $n, $c := default (list "reserved" "on_demand" "spot") .fleet.capacity_order }}{{ if eq $c $t }}{{ $i = $n }}{{ end }}{{ end -}}
{{- $i -}}
{{- end -}}

{{/* Ordered pool list for a preference. args: dict "fleet" "pools" (map name->pool, names may be region-prefixed) "prefer" (gpu class or "") "regionOf" (map name->region id, optional)
     Order: preferred class first; inside a class reserved -> on_demand -> spot (fleet.capacity_order) then price; other classes by their cheapest price. Returns a YAML list of names. */}}
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
{{- $keys = append $keys (printf "%d|%010.4f|%s|%d|%010.4f|%s" $rank ((index $minByClass $c) | float64) $c (include "fleet.capIndex" (dict "fleet" $f "pool" $p) | atoi) ((include "fleet.price" (dict "fleet" $f "pool" $p)) | float64) $n) -}}
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
