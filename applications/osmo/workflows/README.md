# Workflow Templates

OSMO workflow templates for training jobs on Nebius.

## Available Workflows

| File | Description | GPUs |
|------|-------------|------|
| `osmo/hello_nebius.yaml` | Hello World example with GPU | 1 |
| `osmo/gpu_test.yaml` | GPU validation test | 1 |
| `osmo/train.yaml` | Single GPU training | 1 |
| `osmo/train-multi-gpu.yaml` | Multi-GPU distributed training | 8 |

## Quick Start

### Test CPU Workflow

```bash
osmo workflow submit osmo/hello_nebius.yaml
```

This workflow runs on a GPU node and prints "Hello Nebius!".

### Test GPU Access

```bash
osmo workflow submit osmo/gpu_test.yaml
```

This workflow validates GPU availability by running `nvidia-smi` on a Nebius L40S node.

> **Note**: GPU workflows require the GPU platform to be configured. See [Configure OSMO GPU Platform](../deploy/002-setup/README.md#configure-osmo-gpu-platform).

## Usage

### Submit via Script

```bash
cd ../scripts
./submit-osmo-training.sh -w ../workflows/osmo/train.yaml
```

### Submit Directly

```bash
# Single GPU
kubectl apply -f osmo/train.yaml

# Multi-GPU
kubectl apply -f osmo/train-multi-gpu.yaml
```

## Workflow Structure

### Single GPU (`train.yaml`)

Best for:
- Development and debugging
- Small models
- Inference testing

Resources:
- 1 GPU
- 64 GB memory
- 8 vCPUs

### Multi-GPU (`train-multi-gpu.yaml`)

Best for:
- Large model training
- Distributed training
- Production workloads

Resources:
- 8 GPUs
- 1400 GB memory
- 120 vCPUs

Features:
- InfiniBand for NCCL
- Shared memory for GPU communication
- Node affinity for GPU cluster

## Customization

### Change Training Image

```yaml
containers:
  - name: training
    image: your-registry/your-image:tag
```

### Add Training Data

```yaml
volumeMounts:
  - name: shared-data
    mountPath: /data
```

### Configure Environment

```yaml
env:
  - name: LEARNING_RATE
    value: "0.001"
  - name: BATCH_SIZE
    value: "32"
```

### Add GPU Resources

```yaml
resources:
  limits:
    nvidia.com/gpu: 8
```

## Environment Variables

### NCCL Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `NCCL_DEBUG` | Debug level (INFO, WARN) | INFO |
| `NCCL_IB_DISABLE` | Disable InfiniBand (0/1) | 0 |
| `NCCL_NET_GDR_LEVEL` | GPUDirect RDMA level | 5 |

### PyTorch Distributed

| Variable | Description |
|----------|-------------|
| `MASTER_ADDR` | Master node address |
| `MASTER_PORT` | Master node port |
| `WORLD_SIZE` | Total number of processes |
| `RANK` | Process rank |

## Monitoring

### View Job Status

```bash
kubectl get jobs -n osmo
kubectl get pods -n osmo -l app=osmo-training
```

### View Logs

```bash
kubectl logs -n osmo -l job-name=<job-name> -f
```

### GPU Metrics

Access Grafana dashboard for GPU utilization metrics.
