{{/*
ROS-specific Helm helpers
*/}}

{{/*
CSV hostname allowlist for presigned S3 URLs (ROS_CSV_ALLOWED_HOSTS).
Uses ros.csvAllowedHosts when set; otherwise derives from objectStorage.endpoint.
*/}}
{{- define "cost-onprem.ros.csvAllowedHosts" -}}
{{- if .Values.ros.csvAllowedHosts -}}
{{- .Values.ros.csvAllowedHosts -}}
{{- else -}}
{{- $endpoint := .Values.objectStorage.endpoint -}}
{{- $endpoint = trimPrefix "https://" $endpoint -}}
{{- $endpoint = trimPrefix "http://" $endpoint -}}
{{- /* Strip user:pass@ auth prefix (e.g. https://user:pass@host) before hostname extraction. */ -}}
{{- $endpoint = regexReplaceAll "^[^@]*@" $endpoint "" -}}
{{- /* Bracketed IPv6 literals like [::1]:443 are uncommon in objectStorage.endpoint; port/path
     stripping below may not normalize them — prefer hostname-only values in values.yaml. */ -}}
{{- $endpoint = regexReplaceAll ":[0-9]+$" $endpoint "" -}}
{{- $endpoint = regexReplaceAll "/.*$" $endpoint "" -}}
{{- $endpoint -}}
{{- end -}}
{{- end -}}

{{/*
Comma-separated K8s service account names allowed to call ROS /internal/tags/*.
Prefers ros.internalAuth.allowedServiceAccounts, then ros.api.tagsAllowedServiceAccounts,
then defaults to the Koku service account (tag sync caller).
*/}}
{{- define "cost-onprem.ros.tagsAllowedServiceAccounts" -}}
{{- if .Values.ros.internalAuth.allowedServiceAccounts -}}
{{- .Values.ros.internalAuth.allowedServiceAccounts -}}
{{- else if .Values.ros.api.tagsAllowedServiceAccounts -}}
{{- .Values.ros.api.tagsAllowedServiceAccounts -}}
{{- else -}}
{{- .Values.costManagement.serviceAccount.name -}}
{{- end -}}
{{- end -}}
