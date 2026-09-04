#!/bin/bash
set -euo pipefail

retry_script="$(dirname "$0")/../../scripts/retry.sh"

# Fail closed: only run destructive k8s cleanup against a context we have just
# refreshed from the recorded cluster ID. A stale local context with the same
# name could belong to a DIFFERENT cluster.
if [ -z "${K8S_CLUSTER_ID:-}" ]; then
  echo "No recorded k8s cluster ID: cannot verify context ownership, skipping k8s cleanup"
  exit 0
fi
if ! "$retry_script" -- nebius mk8s cluster get-credentials \
  --context-name "$K8S_CLUSTER_CONTEXT" \
  --external \
  --force \
  --id "$K8S_CLUSTER_ID"; then
  echo "Cannot refresh credentials for cluster $K8S_CLUSTER_ID (likely already deleted), skipping k8s cleanup"
  exit 0
fi

# Delete the k8s secret only if the context is usable. IAM cleanup (access key, service
# account) is handled by Terraform itself since those are native provider resources.
if kubectl config get-contexts "$K8S_CLUSTER_CONTEXT" &>/dev/null &&
  kubectl --context "$K8S_CLUSTER_CONTEXT" get namespace "$NAMESPACE" &>/dev/null; then
  kubectl delete --context "$K8S_CLUSTER_CONTEXT" -n "$NAMESPACE" job jail-checkpoints-env --ignore-not-found 2>/dev/null || true

  # Remove the rendered credentials and any interrupted-render temp files from
  # the jail. The jail can outlive this module, e.g. when checkpoint storage is
  # disabled on a running cluster. Best-effort: if the jail PVC is already gone,
  # there is nothing to clean.
  kubectl delete --context "$K8S_CLUSTER_CONTEXT" -n "$NAMESPACE" job jail-checkpoints-env-cleanup --ignore-not-found 2>/dev/null || true
  kubectl apply --server-side --context "$K8S_CLUSTER_CONTEXT" -f - <<EOF || true
apiVersion: batch/v1
kind: Job
metadata:
  name: jail-checkpoints-env-cleanup
  namespace: $NAMESPACE
  labels:
    app.kubernetes.io/managed-by: soperator-terraform
spec:
  ttlSecondsAfterFinished: 600
  backoffLimit: 2
  template:
    spec:
      restartPolicy: OnFailure
      containers:
        - name: cleanup
          image: cr.eu-north1.nebius.cloud/soperator/busybox
          command: ["/bin/sh", "-c", "rm -f /mnt/jail/etc/nebius-checkpoints.env /mnt/jail/etc/.nebius-checkpoints.env.tmp.* && echo removed"]
          volumeMounts:
            - name: jail
              mountPath: /mnt/jail
      volumes:
        - name: jail
          persistentVolumeClaim:
            claimName: jail-pvc
EOF
  kubectl wait --context "$K8S_CLUSTER_CONTEXT" -n "$NAMESPACE" --for=condition=complete job/jail-checkpoints-env-cleanup --timeout=120s 2>/dev/null ||
    echo "jail-checkpoints-env-cleanup did not complete (jail PVC may be gone already), continuing"
  kubectl delete --context "$K8S_CLUSTER_CONTEXT" -n "$NAMESPACE" job jail-checkpoints-env-cleanup --ignore-not-found 2>/dev/null || true

  "$retry_script" -- kubectl delete --context "$K8S_CLUSTER_CONTEXT" -n "$NAMESPACE" secret "$SECRET_NAME" --ignore-not-found ||
    echo "Failed to delete secret '$SECRET_NAME' from context '$K8S_CLUSTER_CONTEXT', skipping"
else
  echo "kubectl context '$K8S_CLUSTER_CONTEXT' or namespace '$NAMESPACE' not available, skipping secret cleanup"
fi
