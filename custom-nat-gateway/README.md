# Creating a custom NAT gateway

This example deploys a custom NAT gateway in Nebius AI Cloud.

It creates:

* resources inside an existing VPC network
* a gateway subnet for the NAT VM
* a private workload subnet
* a private `/32` allocation for the gateway VM
* a public `/32` allocation for the gateway VM
* a route table with `0.0.0.0/0` pointed at the gateway private allocation
* an optional private test VM whose outbound traffic uses the gateway VM

The gateway VM must stay in its own subnet. The private route table belongs on the workload subnet only. If you attach it to the gateway subnet, the gateway routes to itself and stops working.

## Prerequisites

1. Install [Nebius CLI](https://docs.nebius.com/cli/).
2. Configure a Nebius CLI profile with access to the target project.
3. Install [Terraform](https://www.terraform.io/).
4. Install `jq`.

The CLI, Bash and `jq` are used by the optional `environment.sh` setup helper.
The VPC lookup during `terraform plan` uses the Nebius provider's credentials;
it does not run a CLI command or require a separate CLI login. You can instead
configure [provider authentication](https://docs.nebius.com/terraform-provider/authentication)
directly and set `parent_id` without using the helper.

## Installation

1. Configure `NEBIUS_TENANT_ID`, `NEBIUS_PROJECT_ID`, and `NEBIUS_REGION` in `environment.sh`.
2. Load the environment:

   ```bash
   source ./environment.sh
   ```

3. Initialize Terraform:

   ```bash
   terraform init
   ```

4. Update `terraform.tfvars` with your VPC network ID and SSH settings.
5. Preview the deployment:

   ```bash
   terraform plan
   ```

6. Apply the configuration:

   ```bash
   terraform apply
   ```

## Configuration variables

Set these values in `terraform.tfvars`:

* `vpc_network_id`
* `ssh_user_name`
* `ssh_public_key`
* `deploy_test_vm`

If you want a different footprint, adjust the VM presets and disk sizes in `variables.tf`.

`vpc_network_id` is required. The example validates that the network exists and belongs to the project in `parent_id`. If the network lookup fails, or the network belongs to a different project, Terraform fails before it creates any subnet or route table resources.

## What Terraform deploys

The flow is:

1. Validate the existing VPC network ID against the target project.
2. Create a dedicated gateway subnet.
3. Create a private workload subnet.
4. Create a private allocation in the gateway subnet.
5. Create a public allocation in the gateway subnet.
6. Create the gateway VM with the private and public allocations attached to `eth0`.
7. Configure IPv4 forwarding and the `iptables` MASQUERADE rule on the gateway VM through cloud-init.
8. Create a custom route table in the same network.
9. Create a default route in that route table that points to the gateway private allocation.
10. Associate the custom route table only with the workload subnet.
11. Optionally create a private workload VM in the routed subnet.

## Verifying the NAT gateway

After `terraform apply`, get the outputs:

```bash
terraform output
```

Then:

1. SSH to the gateway VM.
2. SSH from the gateway VM to the private workload VM.
3. On the workload VM, run:

   ```bash
   curl ifconfig.me
   ```

The command should return the public IP of the gateway VM.

You can also use the generated SSH jump command from `terraform output ssh_jump_command`.

If `deploy_test_vm = false`, Terraform still creates the gateway, route table, and private subnet. The workload VM outputs are `null` in that mode.

## Tests

The offline tests use a mocked provider and create no cloud resources:

```bash
terraform test -filter=tests/network_validation.tftest.hcl
```

They check the project ownership guard and both values of `deploy_test_vm`.

The separate live smoke test creates real VMs, disks and networking resources,
incurs charges, and attempts to destroy those resources when it finishes.
It requires provider credentials with permission to read the existing VPC and
create/delete resources in its project. Set the required inputs explicitly:

```bash
export TF_VAR_parent_id="project-..."
export TF_VAR_vpc_network_id="vpcnetwork-..."
export TF_VAR_ssh_public_key="$(jq -n --arg key "$(cat ~/.ssh/id_ed25519.pub)" '{key: $key}')"
export TF_VAR_deploy_test_vm=true
terraform test -filter=tests/main.tftest.hcl
```

Supply an existing SSH public key; replace the file path above if needed. Remove
conflicting placeholder values from `terraform.tfvars` before running the test,
because that file overrides `TF_VAR_*` values. Alternatively, pass the inputs
using `-var` or `-var-file`, which take precedence over `terraform.tfvars`.
Do not run unfiltered `terraform test` unless you intend to run the live test too.

The live test checks:

* the private subnet is attached to the custom route table
* the default route points to the gateway private allocation
* the gateway and workload IP outputs are populated when the test VM is enabled

## Useful outputs

The solution exposes:

* the existing network ID
* gateway public IP
* gateway private IP
* workload private IP
* route table ID
* a ready-to-copy SSH jump command
