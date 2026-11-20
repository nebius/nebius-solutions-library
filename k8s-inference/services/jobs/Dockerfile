# serverless2 job runner: the init container (fetch inputs) and the uploader container of every
# run-class Job, and the main container of endpoint-call jobs (docs/JOBS.md). Small and static:
# aws cli (bucket sync), curl + jq (Kubernetes API, endpoint calls), bash.
# 3.22 pinned by digest so that a rebuild gets the same base; the tag is the one resolved by `crane digest`
FROM alpine:3.22@sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8
RUN apk add --no-cache --upgrade bash curl jq aws-cli ca-certificates && adduser -D -u 10001 runner
# AWS CLI otherwise may use its own older bundled roots.
ENV AWS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
COPY bin/ /usr/local/bin/
RUN chmod +x /usr/local/bin/*.sh
USER 10001
WORKDIR /work
