FROM python:3.12-slim
# Debian security updates of the base image (trivy in CI fails on a fixed CRITICAL)
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY services/api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
ARG HELM_VERSION=3.16.4
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL "https://get.helm.sh/helm-v${HELM_VERSION}-linux-amd64.tar.gz" | tar -xz -C /tmp \
 && mv /tmp/linux-amd64/helm /usr/local/bin/helm && rm -rf /tmp/linux-amd64
COPY services/api/*.py ./
COPY catalog/models /app/catalog/models
COPY charts/endpoint /app/charts/endpoint
ENV CATALOG_DIRS=/app/catalog/models CHART_DIR=/app/charts/endpoint
EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
