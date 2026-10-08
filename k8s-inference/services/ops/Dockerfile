# serverless2 ops image: Nebius CLI + crane + jq + curl. Used by the CronJobs/WorkflowTemplates in
# clusters/*/apps/manifests/ops. Keep it small and static.
FROM debian:bookworm-slim
ARG NEBIUS_CLI_VERSION=0.12.284
ARG CRANE_VERSION=0.21.5
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends ca-certificates curl jq \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL "https://storage.eu-north1.nebius.cloud/cli/release/${NEBIUS_CLI_VERSION}/linux/x86_64/nebius" -o /usr/local/bin/nebius && chmod +x /usr/local/bin/nebius \
 && curl -fsSL "https://github.com/google/go-containerregistry/releases/download/v${CRANE_VERSION}/go-containerregistry_Linux_x86_64.tar.gz" | tar -xz -C /usr/local/bin crane
COPY bin/ /usr/local/bin/
RUN chmod +x /usr/local/bin/* && useradd -u 10001 -m ops
USER 10001
WORKDIR /home/ops
ENTRYPOINT ["/usr/local/bin/ops-login.sh"]
CMD ["sh", "-c", "nebius version"]
