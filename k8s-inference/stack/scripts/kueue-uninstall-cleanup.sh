#!/usr/bin/env bash
# After the Kueue release is uninstalled: remove the two aggregated ClusterRoles the kube-controller-manager
# re-creates while Helm deletes the roles that feed them (clusterrole-aggregation-controller, server-side
# apply). Helm's uninstall --wait never sees them gone and times out after 15 minutes (measured 2026-10-08);
# the release is therefore uninstalled without waiting, and this script runs at destroy time right after it:
# it waits until no contributing role is left, then deletes the stale aggregated roles so a later
# `apply platform` on the same cluster does not hit "resource already exists".
#
#   KUBECONFIG_CONTENT=<yaml> stack/scripts/kueue-uninstall-cleanup.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
k() { "$here/kube.sh" "$@"; }
for i in $(seq 1 40); do
  left=$(k get clusterrole -l 'rbac.kueue.x-k8s.io/batch-admin=true' -o name 2> /dev/null | wc -l)
  left=$((left + $(k get clusterrole -l 'rbac.kueue.x-k8s.io/batch-user=true' -o name 2> /dev/null | wc -l)))
  [ "$left" = 0 ] && break
  sleep 5
done
k delete clusterrole kueue-batch-admin-role kueue-batch-user-role --ignore-not-found 2> /dev/null || true
echo "kueue-uninstall-cleanup: aggregated roles removed (contributing roles left: $left)"
