# Quick checks of the Slurm cluster

Within an SSH session to the Slurm cluster, go to the quickcheck directory:
```shell
cd /opt/slurm-test/quickcheck
```

<details>
<summary>There are 4 sbatch scripts</summary>

- `hello.sh`

  Performs basic checks of the Slurm cluster.
  Jobs can be executed and resources can be allocated.

- `containers.sh`

  Performs similar basic checks of the Slurm cluster but within a container.

- `nccl_single_node.sh`

  Executes single-node NCCL test "all_reduce_perf" twice: using NVLink and using the Infiniband switch.

- `nccl_multi_node.sh`

  Executes a multi-node NCCL test "all_reduce_perf_mpi".

</details>

To run them, execute the following commands:

```bash
sbatch hello.sh && \
tail -f results/hello.out
```

```bash
sbatch containers.sh && \
tail -f results/containers.out
```

```bash
sbatch nccl_single_node.sh && \
tail -f results/nccl_single_node.out
```

```bash
sbatch --nodes=4 nccl_multi_node.sh && \
tail -f results/nccl_multi_node.out
```