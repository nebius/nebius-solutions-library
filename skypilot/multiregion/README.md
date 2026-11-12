# Multi-region SkyPilot on Nebius Managed Kubernetes

This solution shows how to provision two Nebius Managed Kubernetes clusters in different regions and use SkyPilot to run training in one region and model serving in another.

The guide covers the full flow:
1. Configure Nebius project/region values in `environment.sh`
2. Deploy Kubernetes with Terraform
3. Set up SkyPilot for the cluster
4. Run Llama 7B fine-tuning
5. Serve model and run inference

---

## 1) Configure environment variables

Edit:
- `./environment.sh`

Set:
- `NEBIUS_TENANT_ID`
- `NEBIUS_PROJECT_ID_REGION1`
- `NEBIUS_PROJECT_ID_REGION2` (optional if secondary region disabled)
- `NEBIUS_REGION1`
- `NEBIUS_REGION2` (optional if secondary region disabled)

Example:

```bash
cd skypilot/multiregion
vi environment.sh
```

Load variables:

```bash
source ./environment.sh
```

---

## 2) Deploy Terraform infrastructure

Review deployment settings:
- `./terraform.tfvars`

Important flags:
- `enable_secondary_region = false` (primary-only) or `true` (two regions)
- `gpu_nodes_*` and `cpu_nodes_*` sizing

Normal applies do not rotate the Grafana Observability key. For an intentional
rotation, run from the same machine or persistent runner that holds the current
local Grafana credential files, then use Terraform's one-shot replacement
workflow:

```bash
terraform plan \
  -replace='module.o11y.terraform_data.grafana_access_token[0]' \
  -out=grafana-rotation.tfplan
terraform show grafana-rotation.tfplan
terraform apply grafana-rotation.tfplan
```

Before applying, confirm that the plan replaces only
`module.o11y.terraform_data.grafana_access_token[0]`, updates the existing
Grafana application in place, and does not replace clusters or node groups.

After apply, validate all four provisioned datasources: **Nebius Services**,
**Nebius Monitoring**, **Nebius Logging** and **Nebius Tracing**. If validation
fails, retain both the current and previous credential files; do not revoke a
key or request another rotation. After validation succeeds, follow the
[canonical finalization steps](../../k8s-training/README.md#rotating-the-grafana-observability-key)
to invalidate the previous key and remove its local backup. A subsequent normal
`terraform plan` must report no changes.

Compatibility note: the sensitive `grafana_service_account` output no longer
contains `access_token`. The token is delivered ephemerally to Grafana and is
not stored in Terraform state. Consumers referencing that field must remove the
reference; the other fields are unchanged.

Deploy:

```bash
cd skypilot/multiregion
terraform init
terraform plan
terraform apply
```

Validate outputs:

```bash
terraform output
terraform output -json kube_cluster | jq
```

---

## 3) Set up SkyPilot on this Kubernetes cluster

Create uv environment and install SkyPilot:

```bash
cd skypilot/multiregion
mkdir -p .uv-cache
UV_CACHE_DIR=$PWD/.uv-cache uv venv .venv
UV_CACHE_DIR=$PWD/.uv-cache uv pip install --python .venv/bin/python "skypilot-nightly[kubernetes]==1.0.0.dev20260219"
source .venv/bin/activate
sky --version
```

Register Terraform-created cluster contexts in SkyPilot config:

```bash
cd skypilot/multiregion
./setup-skypilot-k8s.sh
sky check kubernetes
```

---

## 4) Create Sky cluster and run training

### 4.1 Bring up a Sky cluster on Kubernetes

Use all 8 B300s:

```bash
sky launch -c mk8s-eu-west2 --cloud kubernetes --gpus B300:8 "nvidia-smi"
sky status
```

### 4.2 Run Llama 7B fine-tuning job

Set Hugging Face token:

```bash
export HF_TOKEN=<your_hf_token>
```

Launch training:

```bash
sky launch -c mk8s-eu-west2 skypilot/llama7b_finetune.yaml --env HF_TOKEN=$HF_TOKEN
```

After the job starts, you can use the MLflow UI to track training progress, metrics, and runs.

Get the MLflow endpoint details from Terraform outputs:

```bash
cd skypilot/multiregion
terraform output -json mlflow_cluster | jq
terraform output -json mlflow_status | jq
terraform output -json mlflow_status | jq -r '.. | strings | select(test("^https?://"))'
```

If `enable_mlflow_cluster = true`, the last command should print the public MLflow UI URL when it is available. Open that URL in your browser.

Get the MLflow login credentials from Terraform outputs:

```bash
cd skypilot/multiregion
terraform output -raw mlflow_admin_username
terraform output -raw mlflow_admin_password
```

Monitor:

```bash
sky logs mk8s-eu-west2
```

---

## 5) Serve and run inference

Bring up SkyServe service:

```bash
sky serve up -n llama7b-svc skypilot/llama7b_serve.yaml --secret HF_TOKEN=$HF_TOKEN
```

Check status and endpoint:

```bash
sky serve status llama7b-svc --endpoint
sky serve status -v llama7b-svc
```

Example health check:

```bash
curl -s http://<endpoint>/health
```

### Inference request

For base model `meta-llama/Llama-2-7b-hf`, use completions:

```bash
curl -s http://<endpoint>/v1/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "meta-llama/Llama-2-7b-hf",
    "prompt": "Write a haiku about Kubernetes.",
    "max_tokens": 128,
    "temperature": 0.7
  }'
```

If you switch to a chat model (e.g. `meta-llama/Llama-2-7b-chat-hf`), use:
- `/v1/chat/completions`

---

## 6) Cleanup

```bash
sky serve down llama7b-svc -y
sky down mk8s-eu-west2 -y
```

If needed, destroy infra:

```bash
terraform destroy
```
