# 3.12-slim as of 2026-10: pinned by digest so that a rebuild gets the same base; the tag is the one resolved by `crane digest`
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
# Debian security updates of the base image (trivy in CI fails on a fixed CRITICAL)
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY services/api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
ARG HELM_VERSION=3.22.0
ARG HELM_SHA256=1e4ab49e429626cf6c6958d914248b78c9730803c2751b87627e171dc800e7bb
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL -o /tmp/helm.tgz "https://get.helm.sh/helm-v${HELM_VERSION}-linux-amd64.tar.gz" \
 && echo "${HELM_SHA256}  /tmp/helm.tgz" | sha256sum -c - \
 && tar -xz -C /tmp -f /tmp/helm.tgz && mv /tmp/linux-amd64/helm /usr/local/bin/helm && rm -rf /tmp/linux-amd64 /tmp/helm.tgz
COPY services/api/*.py ./
COPY catalog/models /app/catalog/models
COPY charts/endpoint /app/charts/endpoint
# Non-root (uid 10001); helm and the trust bundle write under /tmp only
ENV CATALOG_DIRS=/app/catalog/models CHART_DIR=/app/charts/endpoint HOME=/tmp HELM_CACHE_HOME=/tmp/helm HELM_CONFIG_HOME=/tmp/helm HELM_DATA_HOME=/tmp/helm
USER 10001
EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
