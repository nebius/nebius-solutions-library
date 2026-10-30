#!/bin/bash
# Keycloak requests always verify TLS. A private CA can be supplied explicitly.
keycloak_curl() {
    local tls_args=()
    if [[ "${OSMO_KC_SKIP_TLS_VERIFY:-false}" == "true" ]]; then
        echo "OSMO_KC_SKIP_TLS_VERIFY is no longer supported; set OSMO_KC_CA_BUNDLE for a private CA." >&2
        return 2
    fi
    if [[ -n "${OSMO_KC_CA_BUNDLE:-}" ]]; then
        if [[ ! -r "$OSMO_KC_CA_BUNDLE" || ! -s "$OSMO_KC_CA_BUNDLE" ]]; then
            echo "OSMO_KC_CA_BUNDLE must name a readable, nonempty PEM file." >&2
            return 2
        fi
        tls_args=(--cacert "$OSMO_KC_CA_BUNDLE")
    fi
    curl "${tls_args[@]}" "$@"
}
