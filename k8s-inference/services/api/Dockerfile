# 3.12-slim pinned by digest so that a rebuild gets the same base; the tag is the one resolved by `crane digest`
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
# Debian security updates of the base image (trivy in CI fails on a fixed CRITICAL)
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY services/api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# endpoints are rendered natively (render_endpoint.py): no helm, no chart in the image
COPY services/api/*.py ./
COPY catalog/models /app/catalog/models
# Non-root (uid 10001); the trust bundle is written under /tmp only
ENV CATALOG_DIRS=/app/catalog/models HOME=/tmp
USER 10001
EXPOSE 8080
# on SIGTERM uvicorn stops accepting and finishes the requests it holds (a proxied synchronous call runs up to
# SYNC_TIMEOUT_S): the graceful timeout stays under the Deployment's terminationGracePeriodSeconds (charts/platform)
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--timeout-graceful-shutdown", "640"]
