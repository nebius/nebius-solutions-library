# Cost-aware MultiKueue dispatcher and spot price feed (services/dispatcher).
# The Nebius CLI (used by the price feed) is copied from the ops image, built first by tools/images.sh:
#   docker build --build-arg OPS_IMAGE=<registry>/serverless2/ops:<tag> ...
ARG OPS_IMAGE=serverless2/ops:latest
FROM ${OPS_IMAGE} AS ops

FROM python:3.12-slim
RUN pip install --no-cache-dir kubernetes==36.0.3
COPY --from=ops /usr/local/bin/nebius /usr/local/bin/nebius
WORKDIR /app
COPY dispatcher.py price_feed.py /app/
USER 65534
ENTRYPOINT ["python", "-u"]
CMD ["/app/dispatcher.py"]
