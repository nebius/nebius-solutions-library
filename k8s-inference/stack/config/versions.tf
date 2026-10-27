# Evaluation-only root: `terraform console` turns terraform.tfvars into the plan stack.sh follows. No providers, no state.
terraform {
  required_version = ">= 1.12.0, < 2.0.0"
}
