#!/bin/sh
# Entrypoint: turn every mounted SA credentials file /var/run/nebius/<region>.json into a CLI profile
# `sa-<region>` (the file is re-read on every call, tokens are minted per call, nothing expires).
# The profile for $NEBIUS_REGION (else the first file found) becomes the default. Then exec the command.
set -eu
first=""
for f in /var/run/nebius/*.json; do
  [ -f "$f" ] || continue
  region=$(basename "$f" .json)
  nebius profile create "sa-$region" --endpoint api.nebius.cloud --service-account-file-path "$f" \
    ${NEBIUS_PARENT_ID:+--parent-id "$NEBIUS_PARENT_ID"} >/dev/null || echo "ops-login: profile sa-$region failed" >&2
  first=${first:-sa-$region}
done
[ -n "${NEBIUS_REGION:-}" ] && first="sa-$NEBIUS_REGION"
[ -n "$first" ] && nebius profile activate "$first" >/dev/null
# crane/docker: fresh IAM token per registry request through the credential helper
mkdir -p "$HOME/.docker"
cat > "$HOME/.docker/config.json" <<JSON
{"credHelpers": {"cr.eu-north1.nebius.cloud": "nebius-sa", "cr.eu-south1.nebius.cloud": "nebius-sa", "cr.eu-west1.nebius.cloud": "nebius-sa", "cr.us-central1.nebius.cloud": "nebius-sa"}}
JSON
exec "$@"
