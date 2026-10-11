{{/*
Effective values. Two input layouts:
  plain:   the chart's values.yaml keys at the top level (name, image, pool, ...).
  catalog: a catalog entry (catalog/models/<id>.yaml) passed as a values file plus `cluster=<name>`:
           `runtime` merged with `deployments.<cluster>` (overrides win, lists replaced), name = id,
           labels task/mode/cluster from the entry. This is how the API renders every
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

{{/* Labels of every rendered object: the standard Helm set (name = the endpoint, instance = the release, which the API
     names after the entry id) plus the serverless2 labels the API and the dispatcher select on (serverless2.nebius/*,
     the Kueue priority class) and the entry's own labels. Pod selectors (Service, LeaderWorkerSet) do not use these. */}}
{{- define "endpoint.labels" -}}
{{- $v := include "endpoint.values" . | fromYaml -}}
app.kubernetes.io/name: {{ $v.name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
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

{{/* The serving container's resource block, probes, volume mounts and the pod's volumes: shared by the InferenceService
     (one pod per replica) and the LeaderWorkerSet (one pod per node) renderings. args: the effective values ($v) */}}
{{- define "endpoint.resources" -}}
{{- $r := .resources }}
limits:
  nvidia.com/gpu: {{ $r.gpu | quote }}
  cpu: {{ $r.cpu | quote }}
  memory: {{ $r.memory }}
  {{- with $r.ephemeralStorage }}
  ephemeral-storage: {{ . }}
  {{- end }}
requests:
  nvidia.com/gpu: {{ $r.gpu | quote }}
  cpu: {{ default $r.cpu $r.cpuRequest | quote }}
  memory: {{ default $r.memory $r.memoryRequest }}
  {{- with $r.ephemeralStorage }}
  ephemeral-storage: {{ . }}
  {{- end }}
{{- end }}

{{- define "endpoint.probes" -}}
{{- with .readinessProbe }}
readinessProbe:
  httpGet:
    path: {{ .path }}
    port: {{ default $.port .port }}
  {{- with .periodSeconds }}
  periodSeconds: {{ . }}
  {{- end }}
  {{- with .failureThreshold }}
  failureThreshold: {{ . }}
  {{- end }}
{{- end }}
{{- with .livenessProbe }}
livenessProbe:
  httpGet:
    path: {{ .path }}
    port: {{ default $.port .port }}
  periodSeconds: {{ default 15 .periodSeconds }}
  failureThreshold: {{ default 4 .failureThreshold }}
  {{- with .initialDelaySeconds }}
  initialDelaySeconds: {{ . }}
  {{- end }}
{{- end }}
{{- end }}

{{- define "endpoint.volumeMounts" -}}
{{- $w := default (dict) (default (dict) .weights).sharedFilesystem -}}
{{- if .shm.enabled }}
- { name: shm, mountPath: /dev/shm }
{{- end }}
{{- if $w.enabled }}
- { name: weights, mountPath: {{ default "/weights" $w.mountPath }}, subPath: {{ default .name $w.path | quote }}, readOnly: {{ default false $w.readOnly }} }
{{- end }}
{{- range .volumes }}
- { name: {{ .name }}, mountPath: {{ .mountPath }} }
{{- end }}
{{- range .persistentVolumes }}
- { name: {{ .name }}, mountPath: {{ .mountPath }} }
{{- end }}
{{- end }}

{{- define "endpoint.volumes" -}}
{{- $w := default (dict) (default (dict) .weights).sharedFilesystem -}}
{{- if .shm.enabled }}
- name: shm
  emptyDir: { medium: Memory, sizeLimit: {{ .shm.size }} }
{{- end }}
{{- if $w.enabled }}
- name: weights              # region shared filesystem (Terraform weights_filesystem) through the cluster PVC; subPath <path>
  persistentVolumeClaim: { claimName: {{ default "weights-shared" $w.claimName | quote }} }
{{- end }}
{{- range .volumes }}
- name: {{ .name }}
  emptyDir:
    sizeLimit: {{ .sizeLimit }}
    {{- with .medium }}
    medium: {{ . }}
    {{- end }}
{{- end }}
{{- range .persistentVolumes }}
- name: {{ .name }}
  persistentVolumeClaim:
    claimName: {{ $.name }}-{{ .name }}
{{- end }}
{{- end }}

{{/* The init container that creates the model's directory on the shared filesystem (root-owned otherwise). args: $v */}}
{{- define "endpoint.weightsPrepare" -}}
{{- $w := default (dict) (default (dict) .weights).sharedFilesystem -}}
{{- if and $w.enabled $w.prepare }}
- name: weights-prepare
  image: {{ $w.prepareImage }}
  command: [sh, -c, "mkdir -p /weights-root/{{ default .name $w.path }} && chmod 1777 /weights-root/{{ default .name $w.path }}"]
  securityContext: { runAsUser: 0 }
  volumeMounts:
    - { name: weights, mountPath: /weights-root }
  resources:
    requests: { cpu: 10m, memory: 16Mi }
    limits: { cpu: 100m, memory: 64Mi }
{{- end }}
{{- end }}
