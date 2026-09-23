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

Use all 8 H100s:

```bash
sky launch -c mk8s-eu-north1 --cloud kubernetes --gpus H100:8 "nvidia-smi"
sky status
```

### 4.2 Run Llama 7B fine-tuning job

Set Hugging Face token:

```bash
export HF_TOKEN=<your_hf_token>
```

Launch training:

```bash
sky launch -c mk8s-eu-north1 skypilot/llama7b_finetune.yaml --env HF_TOKEN=$HF_TOKEN
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
sky logs mk8s-eu-north1
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
sky down mk8s-eu-north1 -y
```

If needed, destroy infra:

```bash
terraform destroy
```
