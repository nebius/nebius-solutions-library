# Cost-aware MultiKueue dispatcher and spot price feed (services/dispatcher).
# The Nebius CLI (used by the price feed) is copied from the ops image, built first by tools/images.sh:
#   docker build --build-arg OPS_IMAGE=<registry>/serverless2/ops:<tag> ...
ARG OPS_IMAGE=serverless2/ops:latest
FROM ${OPS_IMAGE} AS ops

# 3.12-slim pinned by digest so that a rebuild gets the same base; the tag is the one resolved by `crane digest`
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
# Debian security updates of the base image (trivy in CI fails on a fixed CRITICAL)
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir kubernetes==36.0.3
COPY --from=ops /usr/local/bin/nebius /usr/local/bin/nebius
WORKDIR /app
COPY dispatcher.py price_feed.py /app/
COPY infiniband.py nebius_cli.py /app/
USER 65534
ENTRYPOINT ["python", "-u"]
CMD ["/app/dispatcher.py"]
