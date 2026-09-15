#!/usr/bin/env bash
set -euo pipefail

module_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
helm_bin=${FOUNDATION_HELM_BIN:-helm}
version=$("$helm_bin" version --template '{{.Version}}')

case "$version" in
  v3.*)
    "$helm_bin" "$@" --post-renderer "$module_dir/scripts/filter_foundation.sh"
    ;;
  v4.*)
    # Helm 4 discovers post-renderers as plugins. Use a process-local plugin
    # directory; leave the runner's installed plugins and configuration untouched.
    work_dir=$(mktemp -d)
    trap 'rm -rf -- "$work_dir"' EXIT
    export HELM_PLUGINS="$work_dir/plugins"
    plugin_dir="$HELM_PLUGINS/soperator-foundation"
    mkdir -p "$plugin_dir"
    cp "$module_dir/templates/postrenderer-plugin.yaml" "$plugin_dir/plugin.yaml"
    cp "$module_dir/scripts/filter_foundation.sh" "$plugin_dir/filter_foundation.sh"
    "$helm_bin" "$@" --post-renderer soperator-foundation
    ;;
  *)
    echo "Unsupported Helm version: $version (expected Helm 3 or 4)." >&2
    exit 1
    ;;
esac
