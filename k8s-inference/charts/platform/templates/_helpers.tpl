
{{- /* The gateways' access log line: Envoy Gateway's default text format with the request path logged WITHOUT its query
string (REQ_WITHOUT_QUERY) and no request header that could carry a credential; the gate (tools/check.sh) refuses a
format that logs PATH with its query, Authorization, Cookie or an api_key. */ -}}
{{- define "platform.accessLogFormat" -}}
[%START_TIME%] "%REQ(:METHOD)% %REQ_WITHOUT_QUERY(X-ENVOY-ORIGINAL-PATH?:PATH)% %PROTOCOL%" %RESPONSE_CODE% %RESPONSE_FLAGS% %BYTES_RECEIVED% %BYTES_SENT% %DURATION% "%REQ(X-FORWARDED-FOR)%" "%REQ(USER-AGENT)%" "%REQ(X-REQUEST-ID)%" "%REQ(:AUTHORITY)%" "%UPSTREAM_HOST%" "%ROUTE_NAME%"
{{ end -}}
