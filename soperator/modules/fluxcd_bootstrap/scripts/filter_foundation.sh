#!/usr/bin/env bash
set -euo pipefail

: "${FOUNDATION_RELEASE_NAME:?}"

# Use a YAML parser so comments, document ordering and quoting do not affect
# the allowlist. Helm calls this for the actual selected chart before applying it.
yq eval-all -o=json -I=0 '. as $doc ireduce ([]; . + [$doc])' - |
  jq -er --arg prefix "$FOUNDATION_RELEASE_NAME-" '
    . as $docs |
    (["ns", "cert-manager", "storageclasses", "kruise"] | map($prefix + .)) as $releases |
    (["soperator", "kruise", "cert-manager", "bedag"] | map($prefix + .)) as $repositories |
    [$docs[] | select(. != null) | . as $doc | select(
      (.kind == "HelmRelease" and ($releases | index($doc.metadata.name)) != null) or
      (.kind == "HelmRepository" and ($repositories | index($doc.metadata.name)) != null)
    )] as $kept |
    if ([$kept[] | select(.kind == "HelmRelease") | .metadata.name] | sort) != ($releases | sort)
      or ([$kept[] | select(.kind == "HelmRepository") | .metadata.name] | sort) != ($repositories | sort)
    then error("chart does not render all required foundation HelmReleases and repositories")
    elif all($kept[] | select(.kind == "HelmRelease");
      . as $hr |
      ($repositories | index($hr.spec.chart.spec.sourceRef.name)) != null and
      all($hr.spec.dependsOn[]?; .name as $name | ($releases | index($name)) != null)
    ) | not
    then error("foundation HelmRelease references a dependency outside the allowlist")
    else $kept[] | "---\n" + tojson
    end
  '
