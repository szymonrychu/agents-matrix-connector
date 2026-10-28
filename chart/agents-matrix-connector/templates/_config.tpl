{{/* Settings ConfigMap data; no secret values. Fails on a bad env key or a missing required value. */}}
{{- define "agents-matrix-connector.config" -}}
{{- $m := .Values.matrix }}
MATRIX_ORCHESTRATOR_URL: {{ required "orchestrator.url is required" .Values.orchestrator.url | quote }}
MATRIX_HOMESERVER_URL: {{ required "matrix.homeserverUrl is required" $m.homeserverUrl | quote }}
MATRIX_USER_ID: {{ required "matrix.userId is required" $m.userId | quote }}
MATRIX_DEVICE_ID: {{ $m.deviceId | quote }}
MATRIX_SELF_IDENTITIES: {{ join "," ($m.selfIdentities | default list) | quote }}
MATRIX_BRIDGE_BOTS: {{ join "," ($m.bridgeBots | default list) | quote }}
MATRIX_PUPPET_PATTERN: {{ $m.puppetPattern | quote }}
MATRIX_DEFAULT_POLICY: {{ .Values.policy.default | quote }}
MATRIX_GROUP_TRIGGER: {{ .Values.policy.groupTrigger | quote }}
MATRIX_MAX_NEW_SESSIONS: {{ .Values.limits.maxNewSessions | int | toString | quote }}
MATRIX_MAX_QUEUED_PER_ROOM: {{ .Values.limits.maxQueuedPerRoom | int | toString | quote }}
MATRIX_MAX_INBOUND_CHARS: {{ .Values.limits.maxInboundChars | int | toString | quote }}
MATRIX_REPLY_CHUNK_CHARS: {{ .Values.limits.replyChunkChars | int | toString | quote }}
MATRIX_RUN_TIMEOUT_SECONDS: {{ .Values.limits.runTimeoutSeconds | int | toString | quote }}
MATRIX_POLL_SECONDS: {{ .Values.limits.pollSeconds | toString | quote }}
MATRIX_SYNC_TIMEOUT_SECONDS: {{ .Values.limits.syncTimeoutSeconds | int | toString | quote }}
MATRIX_RETENTION_DAYS: {{ .Values.limits.retentionDays | int | toString | quote }}
MATRIX_AGENT_PORT: {{ .Values.agentApi.port | int | toString | quote }}
MATRIX_AGENT_RATE_PER_MINUTE: {{ .Values.limits.agentRatePerMinute | int | toString | quote }}
{{- range $k, $v := .Values.env }}
{{- if not (hasPrefix "MATRIX_" $k) }}
{{- fail (printf "agents-matrix-connector: env key %q must start with MATRIX_" $k) }}
{{- end }}
{{ $k }}: {{ $v | toString | quote }}
{{- end }}
{{- end }}
