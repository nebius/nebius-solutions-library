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
