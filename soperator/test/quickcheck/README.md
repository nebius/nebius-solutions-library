# Quick checks of the Slurm cluster

Within an SSH session to the Slurm cluster, go to the quickcheck directory:

```shell
cd /opt/slurm-test/quickcheck
```

<details>
<summary>There are 4 sbatch scripts</summary>

- `hello.sh`

  Performs basic checks of the Slurm cluster: jobs can be executed and resources can be allocated.

- `containers.sh`

  Launches jobs inside enroot containers.

- `nccl_single_node.sh`

  Executes single-node NCCL test "all_reduce_perf" twice: using NVLink and using the closest Infiniband switch.

- `nccl_multi_node.sh`

  Executes a multi-node NCCL test "all_reduce_perf_mpi".

</details>

To run them, execute following commands:

```shell
sbatch hello.sh && \
tail -f results/hello.out
```

```shell
sbatch containers.sh && \
tail -f results/containers.out
```

```shell
sbatch nccl_single_node.sh && \
tail -f results/nccl_single_node.out
```

```shell
sbatch --nodes=4 nccl_multi_node.sh && \
tail -f results/nccl_multi_node.out
```
