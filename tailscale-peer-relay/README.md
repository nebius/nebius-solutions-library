# Tailscale Peer Relay

This Terraform module deploys one or more Nebius VMs configured as Tailscale peer relay nodes. The common use cases for the peer-relay: https://tailscale.com/docs/features/peer-relay#use-cases

> Why to use the peer-relay? With the default VPC NAT behavior tailscale clients might struggle to establish the direct conenction due to the failure on the [NAT traversal](https://tailscale.com/docs/reference/connection-types#how-tailscale-establishes-connections) to avoid it there are two potential workarounds:
>1) Setup the [Custom NAT](https://docs.nebius.com/vpc/routing/custom-nat-gateway#setting-up-a-custom-nat-gateway-for-subnets)
>2) Setup the tailscale [peer-relay server](https://tailscale.com/docs/features/peer-relay)

## Public docs
 - https://tailscale.com/blog/peer-relays-ga
 - https://tailscale.com/docs/features/peer-relay

## Prerequesits 
- Create the tailscale-auth key https://tailscale.com/docs/features/access-control/auth-keys
- Put the key into Mysterybox within the project https://docs.nebius.com/mysterybox/secrets/create

## What This Module Creates

- `nebius_compute_v1_disk.tailscale-peer-relay-boot-disk[*]`
  - One 60 GiB `NETWORK_SSD` boot disk per relay instance.
- `nebius_compute_v1_instance.tailscale_peer_relay_instance[*]`
  - One VM per `tailscale.instance_count`.
  - Public IP attached on `eth0`.
  - Uses Ubuntu `ubuntu24.04-driverless` image family.
- `nebius_iam_v1_service_account.mysterybox-payload-reader-sa`
  - Service account used by relay instances to obtain the tailscale-auth key from the Mysterybox secret.
- `nebius_iam_v1_group_membership.mysterybox-sa-binding`
  - Adds that service account to the `mysterybox-payload-viewer` group.

## What Cloud-Init Configures

On first boot, cloud-init:

- Creates the SSH user and installs the provided public key.
- Installs Nebius CLI and creates a profile.
- Reads a Tailscale auth key from MysteryBox secret payload.
- Installs the requested Tailscale version.
- Enables `tailscaled`.
- Opens relay-related firewall ports.
- Runs:
  - `tailscale up --auth-key=...`
  - `tailscale set --relay-server-port=<configured_port>`

## Required Inputs

- `tenant_id`
- `parent_id`
- `subnet_id`
- `ssh_public_key` (`key`)
- `tailscale.auth_mysterybox_secret_id`
- `tailscale.version`

## Optional Inputs

- `ssh_user_name` (default: `ubuntu`)
- `tailscale.instance_count` (default: `1`)
- `tailscale.instance_preset` (default: `4vcpu-16gb`)
- `tailscale.relay_server_port` (default: `40000`)

See full variable schema in [variables.tf](./variables.tf).

## Example `terraform.tfvars`

```hcl
ssh_user_name = "ubuntu"

ssh_public_key = {
  key = "ssh-ed25519 AAAA... user@example"
}

tailscale = {
  instance_count            = 2
  instance_preset           = "8vcpu-32gb"
  version                   = "1.94.2"
  relay_server_port         = 40000
  auth_mysterybox_secret_id = "mbsec-xxxxxxxxxxxxxxxx"
}
```

## Deploy

1. Run `source environment.sh`
2. Initialize Terraform:

```bash
terraform init
```

3. Review plan:

```bash
terraform plan
```

4. Apply:

```bash
terraform apply
```

## Output

- Map of relay instance public IPs.

## Notes

- If you change cloud-init logic, existing instances will not re-run cloud-init automatically; replace/recreate instances to apply those changes.
