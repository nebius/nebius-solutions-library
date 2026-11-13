#!/usr/bin/env bash
# Check the effective OS, Python, pip, AWS CLI and merged API stores in built images.
# Usage: tools/check-image-trust.sh [tag] (defaults to ci). No cloud credentials needed.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="${1:-ci}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
check() { python3 "$ROOT/tools/check-ca-trust.py" "$1"; }
for component in ops api dispatcher jobs; do
    image="serverless2/$component:$TAG"
    bundle="$TMP/$component-system.pem"
    docker run --rm --entrypoint cat "$image" /etc/ssl/certs/ca-certificates.crt > "$bundle"
    check "$bundle"
done
for component in api dispatcher jobs; do
    modules=(certifi)
    if [ "$component" != jobs ]; then modules+=(pip._vendor.certifi); fi
    for module in "${modules[@]}"; do
        bundle="$TMP/$component-$module.pem"
        docker run --rm --entrypoint python3 "serverless2/$component:$TAG" -c \
            'import importlib,sys; print(open(importlib.import_module(sys.argv[1]).where()).read())' "$module" > "$bundle"
        check "$bundle"
    done
done
docker run --rm --entrypoint sh "serverless2/jobs:$TAG" -c 'test -n "$AWS_CA_BUNDLE" && cat "$AWS_CA_BUNDLE"' > "$TMP/aws.pem"
check "$TMP/aws.pem"
# Exercise the actual API merger with an additional private CA.
mkdir "$TMP/private"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=trust-regression-private-ca \
    -keyout "$TMP/private.key" -out "$TMP/private/fleet-ca.pem" >/dev/null 2>&1
chmod 755 "$TMP" "$TMP/private"
chmod 644 "$TMP/private/fleet-ca.pem"
docker run --rm --entrypoint python -v "$TMP/private:/etc/ssl/msp:ro" "serverless2/api:$TAG" -c \
    'import os,tls_trust; tls_trust.configure_trust(); print(open(os.environ["SSL_CERT_FILE"]).read())' > "$TMP/api-merged.pem"
check "$TMP/api-merged.pem"
