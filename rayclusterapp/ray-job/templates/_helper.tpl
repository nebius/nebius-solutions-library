{{/* Define the chart name */}}
{{- define "rayjob.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{/* Define the chart fullname including release name */}}
{{- define "rayjob.fullname" -}}
{{ .Release.Name }}-{{ include "rayjob.name" . }}
{{- end }}
