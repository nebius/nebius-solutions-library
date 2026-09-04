# Slurm cluster testing and benchmarking

We offer few kinds of checks:
- [Tests](#tests):
  - [Quick tests](./quickcheck)
  - [Checkpointing to Nebius Object Storage](./checkpointing)

## Uploading

Tests and benchmarks require you to upload data and scripts to the Slurm cluster.
You can use [`deliver.sh`](./deliver.sh) script for tests listed above.

```console
$ ./deliver.sh -h
Usage: ./deliver.sh <REQUIRED_FLAGS> [FLAGS] [-h]
Required flags:
  -t  [str ]  Test type. One of:
                quickcheck
                checkpointing
  -u  [str ]  SSH username
  -k  [path]  Path to private SSH key
  -a  [str ]  Address of login node (IP or domain name)

Flags:
  -p  [int ]  SSH port of login node.
              By default, 22

  -h  Print help and exit
```

It accepts following parameters:
- `-t` - type of the test you want to run. It must be one of:
  - `quickcheck` - for quick tests
  - `checkpointing` - checkpointing to Nebius Object Storage: verification and reference example
- `-u` - SSH **username** for login nodes
- `-k` - path to the private part of the keypair used for **username** auth
- `-a` - address of the Slurm login node. It could be either IP address, or domain name you gave it in `/etc/hosts`
- `-p` - SSH port of the login node

It will upload data and scripts of chosen test type to the Slurm cluster alongside with [common](./common) scripts.
You can upload all test types sequentially.

Once it's uploaded, you can find these tests inside `/opt/slurm-test` directory on your cluster.

## Tests

For quick check tests, see its [README](./quickcheck/README.md).

For the checkpointing verification and reference example (async sharded checkpoints,
auto-resume after preemption, cross-cluster resume, `verify.sh`), see the
[checkpointing README](./checkpointing/README.md).

## Benchmarks

Benchmarks need datasets and checkpoints to be downloaded to the cluster.
As well as some configuration needed to be done before running training.

Please follow instructions [here](https://github.com/NVIDIA/dgxc-benchmarking?tab=readme-ov-file#quick-start-guide) for NVIDIA DGXC benchamrks.

In case benchmark scripts require path to the data directory it's better to have it on dedicated shared storage that can handle multiple connections from Slurm workers
(aka Jail sub-mounts).

<details>
<summary>Creating storage for benchmarks</summary>

You can create storage within this Terraform recipe, as in provided [terraform.tfvars](../installations/example/terraform.tfvars):

```terraform
# Shared filesystems to be mounted inside jail.
# ---
filestore_jail_submounts = [{
  name       = "benchmark-data"
  mount_path = "/data"
  spec = {
    size_gibibytes       = 4096
    block_size_kibibytes = 32
    forbid_deletion      = true
  }
}]
```

Or, you can use the same filestore for multiple clusters.
In order to do this, create it on your own with the Nebius CLI

```shell
nebius compute filesystem create \
  --parent-id "${NEBIUS_PROJECT_ID}" \
  --name 'shared-benchmark-data' \
  --type 'network_ssd' \
  --size-bytes 4398046511104
```

And provide its ID to the recipe as follows:

```terraform
# Shared filesystems to be mounted inside jail.
# ---
filestore_jail_submounts = [{
  name       = "benchmark-data"
  mount_path = "/data"
  existing = {
    id = "<ID of created filestore>"
  }
}]
```

It will attach the storage to your cluster at `/data` directory.
</details>
