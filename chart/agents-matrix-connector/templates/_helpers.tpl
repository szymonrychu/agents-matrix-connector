{{- define "agents-matrix-connector.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "agents-matrix-connector.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "agents-matrix-connector.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "agents-matrix-connector.selectorLabels" -}}
app.kubernetes.io/name: {{ include "agents-matrix-connector.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "agents-matrix-connector.labels" -}}
helm.sh/chart: {{ include "agents-matrix-connector.chart" . }}
{{ include "agents-matrix-connector.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/* Call with (dict "context" . "component" "api"). */}}
{{- define "agents-matrix-connector.componentLabels" -}}
{{ include "agents-matrix-connector.selectorLabels" .context }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/* imagePullSecrets from values plus global, deduplicated by name. */}}
{{- define "agents-matrix-connector.imagePullSecrets" -}}
{{- $g := ((.Values.global | default dict).imagePullSecrets) | default list }}
{{- $seen := dict }}
{{- $all := list }}
{{- range concat (.Values.imagePullSecrets | default list) $g }}
{{- if not (hasKey $seen .name) }}
{{- $_ := set $seen .name true }}
{{- $all = append $all . }}
{{- end }}
{{- end }}
{{- if $all }}
imagePullSecrets:
  {{- toYaml $all | nindent 2 }}
{{- end }}
{{- end }}

{{/* Name of the Secret holding the database URL; fails when no source is set. */}}
{{- define "agents-matrix-connector.databaseSecretName" -}}
{{- $p := .Values.postgres -}}
{{- if and $p.url $p.existingSecret -}}
{{- fail "agents-matrix-connector: set only one of postgres.url and postgres.existingSecret" -}}
{{- else if $p.existingSecret -}}
{{- $p.existingSecret -}}
{{- else if $p.url -}}
{{- include "agents-matrix-connector.fullname" . -}}
{{- else -}}
{{- fail "agents-matrix-connector: a Postgres connection is required: set postgres.url or postgres.existingSecret" -}}
{{- end -}}
{{- end }}

{{- define "agents-matrix-connector.databaseSecretKey" -}}
{{- if .Values.postgres.existingSecret -}}
{{- .Values.postgres.existingSecretKey | default "uri" -}}
{{- else -}}
database-url
{{- end -}}
{{- end }}

{{/* Name of the Secret holding the orchestrator token; fails when none is set. */}}
{{- define "agents-matrix-connector.tokenSecretName" -}}
{{- $o := .Values.orchestrator -}}
{{- if and $o.tokenSecret $o.tokenSecret.name -}}
{{- $o.tokenSecret.name -}}
{{- else if $o.token -}}
{{- include "agents-matrix-connector.fullname" . -}}
{{- else -}}
{{- fail "agents-matrix-connector: the orchestrator token is required: set orchestrator.token or orchestrator.tokenSecret.name" -}}
{{- end -}}
{{- end }}

{{- define "agents-matrix-connector.tokenSecretKey" -}}
{{- if and .Values.orchestrator.tokenSecret .Values.orchestrator.tokenSecret.name -}}
{{- .Values.orchestrator.tokenSecret.key | default "token" -}}
{{- else -}}
orchestrator-token
{{- end -}}
{{- end }}

{{/* Name of the Secret holding the Matrix access token; fails when none is set. */}}
{{- define "agents-matrix-connector.matrixSecretName" -}}
{{- $m := .Values.matrix -}}
{{- if and $m.accessTokenSecret $m.accessTokenSecret.name -}}
{{- $m.accessTokenSecret.name -}}
{{- else if $m.accessToken -}}
{{- include "agents-matrix-connector.fullname" . -}}
{{- else -}}
{{- fail "agents-matrix-connector: the Matrix access token is required: set matrix.accessToken or matrix.accessTokenSecret.name" -}}
{{- end -}}
{{- end }}

{{- define "agents-matrix-connector.matrixSecretKey" -}}
{{- if and .Values.matrix.accessTokenSecret .Values.matrix.accessTokenSecret.name -}}
{{- .Values.matrix.accessTokenSecret.key | default "token" -}}
{{- else -}}
matrix-access-token
{{- end -}}
{{- end }}

{{/*
Container env for a component. Call with (dict "context" . "component" "api"): the settings
ConfigMap and the database URL for every container; the orchestrator token for api and bot (not
migrate); the api allowlist for api only. The Matrix token is never an env var.
*/}}
{{- define "agents-matrix-connector.env" -}}
envFrom:
  - configMapRef:
      name: {{ include "agents-matrix-connector.fullname" .context }}-settings
env:
  - name: MATRIX_DATABASE_URL
    valueFrom:
      secretKeyRef:
        name: {{ include "agents-matrix-connector.databaseSecretName" .context }}
        key: {{ include "agents-matrix-connector.databaseSecretKey" .context }}
{{- if eq .component "api" }}
  - name: MATRIX_API_ALLOWED_CIDRS
    value: {{ join "," (.context.Values.api.allowedCidrs | default (list "127.0.0.1/32")) | quote }}
{{- end }}
{{- if has .component (list "api" "bot") }}
  - name: MATRIX_ORCHESTRATOR_TOKEN
    valueFrom:
      secretKeyRef:
        name: {{ include "agents-matrix-connector.tokenSecretName" .context }}
        key: {{ include "agents-matrix-connector.tokenSecretKey" .context }}
{{- end }}
{{- if eq .component "bot" }}
  - name: MATRIX_ACCESS_TOKEN_FILE
    value: /var/run/secrets/matrix/token
{{- end }}
{{- end }}
