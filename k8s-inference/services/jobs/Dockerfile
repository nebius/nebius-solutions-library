# serverless2 job runner: the init container (fetch inputs) and the uploader container of every
# run-class Job, and the main container of endpoint-call jobs (docs/JOBS.md). Small and static:
# aws cli (bucket sync), curl + jq (Kubernetes API, endpoint calls), bash.
FROM alpine:3.20
RUN apk add --no-cache bash curl jq aws-cli ca-certificates && adduser -D -u 10001 runner
COPY bin/ /usr/local/bin/
RUN chmod +x /usr/local/bin/*.sh
USER 10001
WORKDIR /work
