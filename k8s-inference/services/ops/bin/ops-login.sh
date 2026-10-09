#!/bin/sh
# Entrypoint: turn every mounted SA credentials file /var/run/nebius/<region>.json into a CLI profile
# `sa-<region>` (the file is re-read on every call, tokens are minted per call, nothing expires).
# The profile for $NEBIUS_REGION (else the first file found) becomes the default. Then exec the command.
# crane and docker get a credential helper for the Container Registry of every region the pod holds a key
# for, every region named in $NEBIUS_REGIONS (space- or comma-separated) and the four long-standing ones.
set -eu
first=""
regions="$(printf '%s' "${NEBIUS_REGIONS:-}" | tr ',' ' ')"
for f in /var/run/nebius/*.json; do
  [ -f "$f" ] || continue
  region=$(basename "$f" .json)
  regions="$regions $region"
  nebius profile create "sa-$region" --endpoint api.nebius.cloud --service-account-file-path "$f" \
    ${NEBIUS_PARENT_ID:+--parent-id "$NEBIUS_PARENT_ID"} >/dev/null || echo "ops-login: profile sa-$region failed" >&2
  first=${first:-sa-$region}
done
[ -n "${NEBIUS_REGION:-}" ] && first="sa-$NEBIUS_REGION"
[ -n "$first" ] && nebius profile activate "$first" >/dev/null
# crane/docker: fresh IAM token per registry request through the credential helper, one entry per region
helpers=""
for r in $(printf '%s\n' $regions eu-north1 eu-south1 eu-west1 us-central1 | sort -u); do
  helpers="$helpers${helpers:+, }\"cr.$r.nebius.cloud\": \"nebius-sa\""
done
mkdir -p "$HOME/.docker"
printf '{"credHelpers": {%s}}\n' "$helpers" > "$HOME/.docker/config.json"
exec "$@"
