#!/bin/bash
set -euo pipefail

base64_value() {
  printf '%s' "$1" | base64 | tr -d '\n'
}

# The access key is created with secret_delivery_mode=MYSTERY_BOX, so the secret
# is not in the Terraform state; fetch it ephemerally from MysteryBox.
ACCESS_KEY_SECRET_VAL=$(nebius mysterybox v1 payload get --secret-id "$SECRET_REFERENCE_ID" --format json |
  jq -r '.. | .string_value? // empty' | head -1)
if [ -z "$ACCESS_KEY_SECRET_VAL" ]; then
  echo "ERROR: could not retrieve the access key secret from MysteryBox ($SECRET_REFERENCE_ID)" >&2
  exit 1
fi

if ! kubectl get namespace "$NAMESPACE" --context "$K8S_CLUSTER_CONTEXT" >/dev/null; then
  echo "ERROR: Soperator namespace $NAMESPACE does not exist; deploy Slurm before checkpoint access." >&2
  exit 1
fi

kubectl apply --server-side --context "$K8S_CLUSTER_CONTEXT" -f - <<EOF
apiVersion: v1
kind: Secret
type: Opaque
metadata:
  name: $SECRET_NAME
  namespace: $NAMESPACE
  labels:
    app.kubernetes.io/managed-by: soperator-terraform
  annotations:
    slurm.nebius.ai/service-account: $SERVICE_ACCOUNT_ID
data:
  access-key-id: $(base64_value "$ACCESS_KEY_ID_VAL")
  secret-access-key: $(base64_value "$ACCESS_KEY_SECRET_VAL")
  endpoint: $(base64_value "$OBJECT_STORAGE_ENDPOINT")
  bucket: $(base64_value "$CHECKPOINT_BUCKET")
  region: $(base64_value "$OBJECT_STORAGE_REGION")
EOF

# Render the secret into the jail as /etc/nebius-checkpoints.env so Slurm jobs
# (which cannot read k8s secrets) can source Object Storage credentials. The
# AWS_* exports are compatibility inputs required by S3-protocol SDKs; the
# customer-facing endpoint, region, and bucket exports use Nebius names.
kubectl delete job jail-checkpoints-env --context "$K8S_CLUSTER_CONTEXT" -n "$NAMESPACE" --ignore-not-found >/dev/null

kubectl apply --server-side --context "$K8S_CLUSTER_CONTEXT" -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: jail-checkpoints-env
  namespace: $NAMESPACE
  labels:
    app.kubernetes.io/managed-by: soperator-terraform
spec:
  ttlSecondsAfterFinished: 600
  backoffLimit: 4
  template:
    spec:
      restartPolicy: OnFailure
      containers:
        - name: render
          image: cr.eu-north1.nebius.cloud/soperator/busybox
          command:
            - /bin/sh
            - -c
            - |
              set -e
              umask 077
              # Write to a temp file and rename: readers never see a truncated
              # or permission-transitional file, and key rotation is atomic.
              # Use a private, unique file so a killed prior renderer cannot
              # leave a predictable or permissive file that is reused here.
              rm -f /mnt/jail/etc/.nebius-checkpoints.env.tmp.*
              tmp="\$(mktemp /mnt/jail/etc/.nebius-checkpoints.env.tmp.XXXXXX)"
              trap 'rm -f "\$tmp"' EXIT
              trap 'exit 1' HUP INT TERM
              # Encode values while rendering and decode them when sourced. This
              # preserves shell-significant credential bytes as data rather than
              # allowing them to become shell syntax in the environment file.
              write_export() {
                encoded="\$(printf '%s' "\$2" | base64 | tr -d '\n')"
                printf 'export %s="\$(printf %%s %s | base64 -d)"\n' "\$1" "\$encoded"
              }
              {
                write_export NEBIUS_OBJECT_STORAGE_ENDPOINT "\$OBJECT_STORAGE_ENDPOINT"
                write_export NEBIUS_OBJECT_STORAGE_REGION "\$OBJECT_STORAGE_REGION"
                write_export NEBIUS_CHECKPOINT_BUCKET "\$CHECKPOINT_BUCKET"
                write_export AWS_ACCESS_KEY_ID "\$ACCESS_KEY_ID"
                write_export AWS_SECRET_ACCESS_KEY "\$ACCESS_KEY_SECRET"
                write_export AWS_ENDPOINT_URL "\$OBJECT_STORAGE_ENDPOINT"
                write_export AWS_REGION "\$OBJECT_STORAGE_REGION"
              } > "\$tmp"
              chown $JAIL_ENV_FILE_OWNER "\$tmp"
              chmod $JAIL_ENV_FILE_MODE "\$tmp"
              mv -f "\$tmp" /mnt/jail/etc/nebius-checkpoints.env
              trap - EXIT HUP INT TERM
              echo "rendered /etc/nebius-checkpoints.env in jail (owner $JAIL_ENV_FILE_OWNER mode $JAIL_ENV_FILE_MODE)"
          env:
            - name: ACCESS_KEY_ID
              valueFrom: { secretKeyRef: { name: $SECRET_NAME, key: access-key-id } }
            - name: ACCESS_KEY_SECRET
              valueFrom: { secretKeyRef: { name: $SECRET_NAME, key: secret-access-key } }
            - name: OBJECT_STORAGE_ENDPOINT
              valueFrom: { secretKeyRef: { name: $SECRET_NAME, key: endpoint } }
            - name: OBJECT_STORAGE_REGION
              valueFrom: { secretKeyRef: { name: $SECRET_NAME, key: region } }
            - name: CHECKPOINT_BUCKET
              valueFrom: { secretKeyRef: { name: $SECRET_NAME, key: bucket } }
          volumeMounts:
            - name: jail
              mountPath: /mnt/jail
      volumes:
        - name: jail
          persistentVolumeClaim:
            claimName: jail-pvc
EOF

# The module depends on the Slurm deployment (see the example installation), so
# the jail PVC exists by now and the Job must succeed promptly. A failure here
# means credentials will not be available to jobs - fail loudly.
if ! kubectl wait --context "$K8S_CLUSTER_CONTEXT" -n "$NAMESPACE" --for=condition=complete job/jail-checkpoints-env --timeout=300s; then
  echo "ERROR: jail-checkpoints-env Job did not complete: /etc/nebius-checkpoints.env is missing in the jail." >&2
  kubectl describe job jail-checkpoints-env --context "$K8S_CLUSTER_CONTEXT" -n "$NAMESPACE" | tail -20 >&2 || true
  exit 1
fi
