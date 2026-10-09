[![Nebius](./.assets/nebius-dark.png)](https://nebius.ai/#gh-dark-mode-only)
[![Nebius](./.assets/nebius-light.png)](https://nebius.ai/#gh-light-mode-only)

# Nebius Solution Library

## Table of contents
* [Introduction](#introduction)
* [Solutions](#solutions)
* [Prerequisites](#prerequisites)

## Introduction

This repository is a curated collection of Terraform and Helm solutions designed to streamline the deployment and management of AI and ML applications on Nebius AI Cloud.  Our solutions library has the tools and resources to help you deploy complex machine learning models, manage scalable infrastructure and ensure that your AI-powered applications run smoothly.

## Solutions

### Training

[Kubernetes prepared for Training](./k8s-training/README.md)

For those who prefer containerized environments, our Kubernetes solution includes GPU-Operator and Network-Operator. This setup ensures that your training workloads use dedicated GPU resources and optimized network configurations, both of which are critical components for AI models that require a lot of computational power. . GPU-Operator simplifies the management of NVIDIA GPUs, automating the deployment of necessary drivers and plugins. Similarly, the Network-Operator improves network performance, ensuring seamless communication throughout your cluster. The cluster uses InfiniBand technology, which provides the fastest host connections for data-intensive tasks. 

[SLURM](./soperator/README.md)

Our SLURM solutions offer a streamlined approach for users who prefer traditional HPC environments. These solutions include ready-to-use images pre-configured with NVIDIA drivers and are ideal for those looking to take advantage of SLURM’s robust job scheduling capabilities.  Similar to our Kubernetes offerings, the SLURM solutions are optimized for InfiniBand connectivity, ensuring peak performance and efficiency in data transfer and communication between nodes.

<!-- k8s-inference:start -->
### Inference

[Kubernetes inference fleet](./k8s-inference/README.md)

A multi-region GPU inference platform on Managed Kubernetes from one `terraform.tfvars`: a CPU control cluster, one cluster per GPU region, spot, on-demand and reserved pools (InfiniBand optional), one API for models with always-on endpoints that scale to zero, queued endpoint calls and long jobs with checkpoint resume, multi-node jobs, a per-region image cache for fast cold starts, tenants with API keys and budgets, and observability with cost reporting. Any container is a model; the platform ships none. Everything inside the clusters is an upstream Helm chart or a small manifest.

**Nebius Serverless AI or this solution?**

| Question | Nebius Serverless AI (the managed service) | This solution (your own fleet) |
|---|---|---|
| What is it? | A service in the Nebius console: you give it a container, Nebius runs it. Nothing to install or operate. | A platform Terraform brings up inside your own Nebius projects: Kubernetes clusters, GPU pools, queues, one API. You (or your platform team) run it. |
| How long until the first call? | Minutes. | About 45 minutes, then minutes per model. |
| How do I define a model? | A form: image, command, GPU, scaling, environment. | A container description with the same kind of fields (image, args, port, protocol, GPU class, scaling, regions). |
| What runs? | Always-on containers behind an HTTP endpoint that scale to zero. | The same endpoints, plus queued calls and long jobs with checkpoints that survive spot preemption, plus multi-node jobs over InfiniBand. |
| Which GPUs? | One platform and preset per endpoint. | A preferred GPU class with fallbacks per model; spot, on-demand and reserved pools, in several regions, the cheapest free one wins. |
| Who are the users? | You and your Nebius project members. | Your tenants: namespaces, API keys with budgets and model allow-lists, GPU quotas, rate limits. |
| Where do the data and the network live? | In the service. | In your projects, your subnets, your buckets, behind your source-IP allow-lists; private gateways are an option. |
| What do I pay for? | Usage, per the service's price list. | The nodes of your clusters (idle GPU pools scale to zero) plus a small control plane; cost reports per key and per run. |
| Operations? | None. | Grafana, Prometheus, Loki, OpenCost, backups and spot recovery are installed; upgrades and incidents are yours. |
| Pick it when... | You want one or a few models online quickly and do not want to run anything. | You run a platform for several teams or customers, mix online and batch work, need placement control, reserved capacity, private networking or your own observability, and are fine operating Kubernetes. |

The two are not exclusive: a team can start on Serverless AI and move to this fleet when it needs
queues, tenants or multi-node jobs; a model is a container plus a few knobs in both. Check the
Serverless AI documentation for its limits of the day; this table compares the shapes of the two offerings.

<!-- k8s-inference:end -->
### Network

[Wireguard](./wireguard/README.md)

Enhance security with a Wireguard VPN instance by minimizing the use of public IPs and limiting access to your cloud environment.

[Bastion](./bastion/README.md)

Deploys a Bastion instance that serves as a secure jump host for your infrastructure. It improves the security by minimizing the use of Public IPs and limiting access to the rest of the environment. 

### Integration

[Anyscale](./anyscale/README.md)

Installs the Anyscale operator on Nebius AI Cloud and offers integration with Anyscale. 

[Skypilot](./skypilot/README.md)

Offers seamless integration with SkyPilot, simplifying the process of launching and managing distributed AI workloads on powerful GPU instances.

[Skypilot multi-region on Nebius Managed Kubernetes](./skypilot/multiregion/README.md)

Provision two Nebius Managed Kubernetes clusters in different regions and use SkyPilot to run training in one region and serving in another.

## Prerequisites

These solutions are built for Nebius AI Cloud, for more information please check our [website](https://nebius.ai/).

These samples mainly use [Terraform](https://www.terraform.io/) to deploy architectures on Nebius AI Cloud, for more instructions on how to use Terraform in Nebius check [here](https://docs.nebius.ai/terraform-provider/)

These solutions will also require you to install the [Nebius AI CLI](https://docs.nebius.ai/cli/).

More general documentation about Nebius AI can be found [here](https://docs.nebius.ai/).
