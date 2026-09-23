# Bastion instance

This Terraform solution deploys a Bastion instance that serves as a secure jump host for your infrastructure. 
It improves the security by minimizing the use of Public IPs and limiting access to the rest of the environment. 

Also create a Service Account with generated Auhorization key pair to authentificate Nebius CLI on the host.

Also installed on the host:
- WireGuard VPN solution with UI
- Nebius CLI and configured with profile authentificated by Service account
- kubectl and configured to connect to first mk8s cluster available in project by --internal flag
  (scanned by: `nebius mk8s v1 cluster list`)

The solution creates a managed Nebius security group by default. The security group allows SSH and WireGuard UDP access only from the configured CIDR blocks; when no SSH CIDRs are provided, no managed inbound SSH or WireGuard rule is created. The WireGuard UI port is not exposed unless `bastion_allowed_wireguard_ui_cidrs` is set. Outbound traffic is unrestricted by default for package installation, Nebius APIs, and container/object storage access; set `bastion_egress_cidrs = []` to create no default egress rule.

## How to connect over bastion

### Edit your local ssh config

`~/.ssh/config`

```
Host bastion
    HostName <public_ip_of_bastion_host>
    User bastion
    IdentityFile ~/.ssh/private.key

Host target
    HostName <private_ip_of_host_after_bastion>
    User ubuntu
    IdentityFile ~/.ssh/private.key
    ProxyJump bastion
```

### Login to remote VM behind bastion
```
ssh target
```

## Prerequisites

1. Install [Nebius CLI](https://docs.nebius.dev/en/cli/#installation):
   ```bash
   curl -sSL https://storage.eu-north1.nebius.cloud/cli/install.sh | bash
   ```

2. Reload your shell session:

   ```bash
   exec -l $SHELL
   ```

   or

   ```bash
   source ~/.bashrc
   ```

3. [Configure](https://docs.nebius.ai/cli/configure/) Nebius CLI (we recommend using [service account](https://docs.nebius.ai/iam/service-accounts/manage/)):
   ```bash
   nebius profile create
   ```

4. Install JQuery (for Debian-based distributions):
   ```bash
   sudo apt install jq -y
   ```

## Installation

To deploy the solution, follow these steps:

1. Configure `NEBIUS_TENANT_ID`, `NEBIUS_PROJECT_ID` and `NEBIUS_REGION` in environment.sh.

2. Load environment variables:
   ```bash
   source ./environment.sh
   ```

3. Initialize Terraform:
   ```bash
   terraform init
   ```

4. Replace the placeholder content in `terraform.tfvars` with the configuration values that you need. See the details [below](#configuration-variables).

5. Preview the deployment plan:
   ```bash
   terraform plan
   ```

6. Apply the configuration:
   ```bash
   terraform apply
   ```
   Wait for the operation to complete.

## Configuration variables

Update the following variables in the `terraform.tfvars` file with your own values:

- `ssh_user_name`
- `ssh_public_key`
- `bastion_allowed_ssh_cidrs` if you want the managed security group to allow SSH access

Security group variables:

- `enable_bastion_security_group`: Create and attach the managed security group. Defaults to `true`.
- `bastion_allowed_ssh_cidrs`: CIDRs allowed to access SSH on TCP 22. When unset or empty, no managed SSH ingress rule is created.
- `bastion_allowed_wireguard_cidrs`: CIDRs allowed to access WireGuard on UDP 51820. Defaults to `bastion_allowed_ssh_cidrs`; set to `[]` to create no managed WireGuard ingress rule.
- `bastion_allowed_wireguard_ui_cidrs`: CIDRs allowed to access the WireGuard UI on TCP 5000. Defaults to no public UI access. Set this explicitly only for trusted admin CIDRs.
- `bastion_allow_unrestricted_ingress_rules`: Set to `true` only when intentionally allowing ingress rules from `0.0.0.0/0` or an empty source.
- `bastion_security_group_name`: Name of the managed bastion security group. Override this when deploying multiple bastion instances in one project; the default keeps the existing single-bastion naming pattern.
- `bastion_egress_cidrs`: CIDRs the bastion can reach. Defaults to `["0.0.0.0/0"]`. Set to `[]` to create no default egress rule; add explicit `bastion_extra_egress_rules` for stricter deployments.
- `bastion_extra_security_group_ids`: Existing security group IDs to attach in addition to the managed security group.
- `bastion_extra_ingress_rules`: Additional managed ingress rules merged with the SSH, WireGuard, and optional WireGuard UI defaults.
- `bastion_extra_egress_rules`: Additional managed egress rules merged with the default egress rule when `bastion_egress_cidrs` is not empty.

When `enable_bastion_security_group = true` and no SSH CIDRs are configured, Terraform emits a check warning and `bastion_security_group_access_note` to make the locked-down access posture visible in the plan and outputs.

Example:

```hcl
bastion_allowed_ssh_cidrs = ["203.0.113.10/32"]

# Optional: expose WireGuard UI only to trusted admin CIDRs.
bastion_allowed_wireguard_ui_cidrs = ["203.0.113.10/32"]
```

For a stricter egress posture, disable the default egress rule and add only the explicit destinations your deployment needs:

```hcl
bastion_egress_cidrs = []
```

Without egress, first boot may fail to install packages or reach Nebius APIs unless equivalent access is provided through extra egress rules or another attached security group.

For a quick test from a dynamic source IP, you can temporarily use unrestricted ingress:

```hcl
bastion_allowed_ssh_cidrs          = ["0.0.0.0/0"]
bastion_allow_unrestricted_ingress_rules = true
```

Do not use unrestricted ingress for a durable deployment.

## Creating and using a public IP allocation

This step allows you to retain the IP address even if the VM is deleted. If you don’t need to keep the IP adress, skip section.

1. Create a public IP allocation:
   ```bash
   nebius vpc v1 allocation create  --ipv-4-public \
   --parent-id <project-id> --name wireguard_allocation_pub \
   --format json | jq -r '.metadata.id'
   ```
2. Assign the value from the previous step to the `public_ip_allocation_id` variable in `terraform.tfvars`:

```bash
public_ip_allocation_id = <public_ip_allocation_id>
```

### Logging into WireGuard UI

1. SSH into the WireGuard instance:
   ```bash
   ssh -i <path_to_private_ssh_key> <ssh_user_name>@<instance_public_ip>
   ```

2. Retrieve the WireGuard UI password:
   ```bash
   sudo cat /var/lib/wireguard-ui/initial_password
   ```

3. Open the WireGuard UI in your browser:
   ```
   http://<instance_public_ip>:5000
   ```

   The managed security group allows this only when `bastion_allowed_wireguard_ui_cidrs` is set. If the variable is unset or empty, the UI service may be running on the VM, but TCP 5000 is not opened by the managed security group.

4. Log in with the following credentials:
   - **Username:** `admin`
   - **Password:** [password retrieved in step 2]

### Notes

- **Apply Config:** After creating, deleting or changing WireGuard users, select "Apply Config".
- **Allowed IPs:** When adding new users, specify the CIDRs of your existing infrastructure in the "Allowed IPs" field.
