# Kubeflow deployment on Nebius AI Cloud

This Terraform module installs the Kubeflow on Nebius AI Cloud.

## Requirements
- [Terraform CLI](https://developer.hashicorp.com/terraform/install)

## Preparation

1. Make a copy of the configuration template: 

```bash
cp default.yaml.tpl default.yaml
```

3. Edit the `environment.sh` file and fill in the values for `NEBIUS_TENANT_ID`, `NEBIUS_PROJECT_ID` and `NEBIUS_REGION`.

4. Load environment variables:
```bash
source ./environment.sh
```

## Installation

1. Initialize the Terraform code in the `deploy` directory: 
```bash
terraform init
```

2. Preview the deployment plan:
```bash
terraform plan
```

3. Apply the configuration:
```bash
terraform apply
```

Wait for the operation to complete.

## Usage
[Connect to the cluster](https://docs.nebius.com/kubernetes/connect) and check if the operator pod is running. If the pod is running, you can start deploying workloads via Kubeflow.

