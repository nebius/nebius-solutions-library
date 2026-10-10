# serverless2 ops image: Nebius CLI + crane + jq + curl. Used by the CronJobs in clusters/common/manifests/ops
# (and the dispatcher image copies the CLI from it). Keep it small and static.
#
# Bumping a tool: set the new version AND its SHA-256 below (or pass both as --build-arg). The CLI's sum is the
# sha256sum of <NEBIUS_CLI_BASE_URL>/release/<version>/linux/x86_64/nebius; crane's is the Linux_x86_64 line of
# checksums.txt on the go-containerregistry release page. The build fails when a download does not match.
# NEBIUS_CLI_BASE_URL defaults to the host the official installer uses (artifacts.nebius.cloud); any mirror that
# serves the same release tree works (storage.<region>.nebius.cloud/cli), the checksum guards the content.
FROM debian:bookworm-slim
ARG NEBIUS_CLI_VERSION=0.12.284
ARG NEBIUS_CLI_SHA256=76dd1c525b211aa5e23207663efd8431768d3e5db9c5e1bded73a556718ac86c
ARG NEBIUS_CLI_BASE_URL=https://artifacts.nebius.cloud/cli
ARG CRANE_VERSION=0.21.5
ARG CRANE_SHA256=9f823ae5ee25803161110f957b5fd4538f714d40cdf25dacb4914fefafd246bf
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends ca-certificates curl jq \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL "${NEBIUS_CLI_BASE_URL}/release/${NEBIUS_CLI_VERSION}/linux/x86_64/nebius" -o /usr/local/bin/nebius \
 && echo "${NEBIUS_CLI_SHA256}  /usr/local/bin/nebius" | sha256sum -c - && chmod +x /usr/local/bin/nebius \
 && curl -fsSL "https://github.com/google/go-containerregistry/releases/download/v${CRANE_VERSION}/go-containerregistry_Linux_x86_64.tar.gz" -o /tmp/crane.tgz \
 && echo "${CRANE_SHA256}  /tmp/crane.tgz" | sha256sum -c - \
 && tar -xz -C /usr/local/bin -f /tmp/crane.tgz crane && rm /tmp/crane.tgz
COPY bin/ /usr/local/bin/
RUN chmod +x /usr/local/bin/* && useradd -u 10001 -m ops
USER 10001
WORKDIR /home/ops
ENTRYPOINT ["/usr/local/bin/ops-login.sh"]
CMD ["sh", "-c", "nebius version"]
