# Saturn Cloud configuration for Nebius
#
# Before running terraform, source environment.sh to set Nebius infrastructure variables:
#   source environment.sh

############################
# Saturn Cloud configuration
############################

cluster_name           = "saturn-cluster"
saturn_admin_email     = "admin@example.com"
saturn_customer_name   = "my-org"
saturn_bootstrap_token = ""

# domain / base URL / SSH domain are derived from saturn_customer_name as
# <customer>.saturnenterprise.io. Set saturn_domain only for customers on their own
# domain (base_url/ssh_domain then follow from it).
# saturn_domain = "saturn.my-company.com"

# region (set via TF_VAR_region / environment.sh) selects everything below: the node
# groups to create AND the Saturn instance sizes shown in the UI are derived from it.
# Supported regions: eu-north1, eu-west1, me-west1, us-central1.

# Uncomment to pin a specific saturn-helm-operator-nebius chart version.
# Defaults to the version in variables.tf.
# helm_chart_version = "2026.02.01-123"

############################
# Node pools (optional override)
############################
# By default node groups are derived from the region (see region_node_pools in
# locals.tf), kept in lock-step with the chart's per-region instance sizes. Set
# node_pools only to deviate from the region defaults.
# 8-GPU presets require infiniband_fabric (obtain from Nebius support).

# node_pools = [
#   { platform = "cpu-d3", preset = "4vcpu-16gb" },
#   { platform = "cpu-d3", preset = "16vcpu-64gb" },
#   { platform = "cpu-d3", preset = "64vcpu-256gb" },
#   { platform = "gpu-h200-sxm", preset = "1gpu-16vcpu-200gb" },
# ]

############################
# Shared Filesystem (filestore)
############################
# A Nebius shared filesystem (ReadWriteMany) is created by default.
# Set enable_filestore = false to opt out.

# enable_filestore              = true   # Set to false to skip creating a shared filesystem
# existing_filestore            = ""     # Use an existing filestore ID, or leave empty to create a new one
# filestore_disk_size_gibibytes = 100    # Size of the shared filesystem in GiB (fixed, does not auto-expand)
