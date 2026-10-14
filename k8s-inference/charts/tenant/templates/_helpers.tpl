{{- define "tenant.ns" -}}tenant-{{ .Values.name }}{{- end -}}
{{- define "tenant.cluster" -}}
{{- $c := index .Values.clusters .Values.cluster -}}
{{- if not $c }}{{ fail (printf "no clusters.%s in the tenant values" .Values.cluster) }}{{ end -}}
{{- $c | toYaml -}}
{{- end -}}
{{/* Scheduling profiles = LocalQueue names: default, prefer-<gpu class> for every class of the fleet (same
     list charts/fleet renders as ClusterQueues on every cluster), plus .Values.profiles. */}}
{{- define "tenant.profiles" -}}
{{- $out := list "default" -}}
{{- $classes := list -}}
{{- range $rn, $r := (default dict .Values.fleet).regions }}{{ range $pn, $p := $r.pools }}{{ $classes = append $classes (default "unknown" $p.gpu_class) }}{{ end }}{{ end -}}
{{- range $c := $classes | uniq | sortAlpha }}{{ $out = append $out (printf "prefer-%s" $c) }}{{ end -}}
{{- range $p := .Values.profiles }}{{ $out = append $out $p }}{{ end -}}
{{- $out | uniq | toYaml -}}
{{- end -}}
{{/* Labels of every rendered object: the standard Helm set plus the tenant; selectors elsewhere use
     serverless2.nebius/tenant (the API lists tenant namespaces by it) and kubernetes.io/metadata.name, never these. */}}
{{- define "tenant.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/part-of: serverless2
serverless2.nebius/tenant: {{ .Values.name }}
{{- end -}}
