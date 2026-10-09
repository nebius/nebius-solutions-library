#!/bin/bash
#
# Deploy Observability Stack (Prometheus, Grafana, Loki)
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
source "${SCRIPT_DIR}/lib/common.sh"
source "${SCRIPT_DIR}/defaults.sh"

echo ""
echo "========================================"
echo "  Observability Stack Deployment"
echo "========================================"
echo ""

# Check prerequisites
check_kubectl || exit 1
check_helm || exit 1

# Add Helm repos
log_info "Adding Helm repositories..."
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update
helm repo add grafana https://grafana.github.io/helm-charts --force-update
helm repo update

# Create namespace
kubectl create namespace "${MONITORING_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

# Generate Grafana password if not set
if [[ -z "$GRAFANA_ADMIN_PASSWORD" ]]; then
    GRAFANA_ADMIN_PASSWORD=$(openssl rand -base64 16)
    log_info "Generated Grafana admin password"
fi

# -----------------------------------------------------------------------------
# Deploy Prometheus
# -----------------------------------------------------------------------------
log_info "Deploying Prometheus..."

helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
    --namespace "${MONITORING_NAMESPACE}" \
    --values "${VALUES_DIR}/prometheus.yaml" \
    --set grafana.adminPassword="${GRAFANA_ADMIN_PASSWORD}" \
    --wait --timeout 10m

log_success "Prometheus stack deployed"

# -----------------------------------------------------------------------------
# Deploy Loki
# -----------------------------------------------------------------------------
log_info "Deploying Loki..."

helm upgrade --install loki grafana/loki-stack \
    --namespace "${MONITORING_NAMESPACE}" \
    --values "${VALUES_DIR}/loki.yaml" \
    --wait --timeout 10m

log_success "Loki deployed"

# -----------------------------------------------------------------------------
# Deploy Promtail
# -----------------------------------------------------------------------------
log_info "Deploying Promtail..."

helm upgrade --install promtail grafana/promtail \
    --namespace "${MONITORING_NAMESPACE}" \
    --values "${VALUES_DIR}/promtail.yaml" \
    --wait --timeout 5m

log_success "Promtail deployed"

# -----------------------------------------------------------------------------
# Configure Grafana Datasources
# -----------------------------------------------------------------------------
log_info "Configuring Grafana datasources..."

# Loki datasource is auto-configured via values

# Wait for Grafana
wait_for_pods "${MONITORING_NAMESPACE}" "app.kubernetes.io/name=grafana" 180

# -----------------------------------------------------------------------------
# Output Access Information
# -----------------------------------------------------------------------------
echo ""
echo "========================================"
log_success "Observability stack deployment complete!"
echo "========================================"
echo ""
echo "Access Grafana:"
echo "  kubectl port-forward -n ${MONITORING_NAMESPACE} svc/prometheus-grafana 3000:80"
echo "  URL: http://localhost:3000"
echo "  Username: admin"
echo "  Password: ${GRAFANA_ADMIN_PASSWORD}"
echo ""
echo "Access Prometheus:"
echo "  kubectl port-forward -n ${MONITORING_NAMESPACE} svc/prometheus-kube-prometheus-prometheus 9090:9090"
echo "  URL: http://localhost:9090"
echo ""
echo "Next step: ./03-deploy-nginx-ingress.sh"
echo ""
