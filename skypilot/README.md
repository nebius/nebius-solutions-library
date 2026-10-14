# SkyPilot Integration with Nebius AI Cloud

## Overview

SkyPilot is an open-source framework for running AI and batch workloads. Nebius AI Cloud offers seamless integration with SkyPilot, simplifying the process of launching and managing distributed AI workloads on powerful GPU instances:

https://nebius.com/blog/posts/nebius-ai-cloud-skypilot-integration 

## Prerequisites

Before getting started, ensure you have:

- **Nebius Account and CLI**:
  - Create your Nebius account
  - Install and configure the [Nebius CLI](https://docs.nebius.com/cli)
  - Create a standalone Skypilot instance on Nebius. 
  - Prepare a configuration file location in `~/.sky/config.yaml`. [Skypilot Config Documentation](https://docs.skypilot.co/en/v0.10.3/reference/config.html) 
    ```
    nebius:
      region_configs:
        <your_region>:
          project_id: project-<your_project_id>
          filesystems:
          - filesystem_id: computefilesystem-<your_filesystem_id>
            mount_path: <your_mount_path>
            attach_mode: READ_WRITE
    api_server:
      endpoint: https://USER:PASSWORD@<skypilot_instance_public_ip_endpoint>
    ```
  - Download and run the setup script:
    ```bash
    wget https://raw.githubusercontent.com/nebius/nebius-solution-library/refs/heads/main/skypilot/nebius-setup.sh
    chmod +x nebius-setup.sh 
    ./nebius-setup.sh
    ```
    - You'll be prompted to select a Nebius tenant and project ID

- **Python Requirements**:
  - Python version 3.10 or higher
  - Install SkyPilot with Nebius support:
    ```bash
    pip install "skypilot-nightly[nebius]"
    ```

## Examples and Solutions

For detailed examples and solutions using SkyPilot on Nebius, refer to:

- [Multi-region Nebius Managed Kubernetes example](./multiregion/README.md)
- https://github.com/nebius/ml-cookbook/tree/main/skypilot

It includes examples on how to:
- lanch individual VMs as well as clusters
- mount Nebius Object Storage to SkyPilot clusters
- do distibuted training and inference
- etc.

For detailed SkyPilot documentation, refer to the official [SkyPilot documentation](https://skypilot.readthedocs.io/en/latest/).
