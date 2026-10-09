{{/*
Effective values. Two input layouts:
  plain:   the chart's values.yaml keys at the top level (name, image, pool, ...).
  catalog: a catalog entry (catalog/models/<id>.yaml) passed as a values file plus `cluster=<name>`:
           `runtime` merged with `deployments.<cluster>` (overrides win, lists replaced), name = id,
           labels task/mode/cluster from the entry. This is how the Terraform models stage renders every
           endpoint straight from the catalog, with no rendered copies in git.
*/}}
{{- define "endpoint.values" -}}
{{- $v := dict -}}
{{- if .Values.runtime -}}
  {{- $cluster := required "catalog mode needs --set cluster=<cluster id>" .Values.cluster -}}
  {{- $over := deepCopy (default (dict) (get (default (dict) .Values.deployments) $cluster)) -}}
  {{- $_ := unset $over "paused" -}}
  {{- $_ := unset $over "price_per_gpu_hour" -}}
  {{- $_ := unset $over "parameters" -}}
  {{- $v = mergeOverwrite (deepCopy (omit .Values "runtime" "deployments" "cluster" "id" "task" "mode" "displayName" "description" "source" "protocol" "port" "endpoints" "gpu" "concurrency" "coldStartClass" "price" "scaleToZero" "parameters" "workflow_template")) (deepCopy .Values.runtime) $over -}}
  {{- $_ := set $v "name" (required "catalog entry needs id" .Values.id) -}}
  {{- $_ := set $v "namespace" (default "models" .Values.namespace) -}}
  {{- $_ := set $v "labels" (merge (dict "serverless2.nebius/task" (default "" .Values.task) "serverless2.nebius/mode" (default "" .Values.mode) "serverless2.nebius/cluster" $cluster) (default (dict) $v.labels)) -}}
{{- else -}}
  {{- $v = .Values -}}
{{- end -}}
{{- $v | toYaml -}}
{{- end -}}

{{- define "endpoint.labels" -}}
{{- $v := include "endpoint.values" . | fromYaml -}}
app.kubernetes.io/name: {{ $v.name }}
app.kubernetes.io/part-of: serverless2
serverless2.nebius/model: {{ $v.name }}
serverless2.nebius/protocol: {{ $v.protocol }}
{{- with $v.queue.priorityClass }}
kueue.x-k8s.io/priority-class: {{ . }}
{{- end }}
{{- with $v.labels }}
{{ toYaml . }}
{{- end }}
{{- end }}
