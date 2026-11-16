#!/bin/bash

#SBATCH --job-name=nccl_multi_node
#SBATCH --output=results/nccl_multi_node.out
#SBATCH --error=results/nccl_multi_node.out
#SBATCH --ntasks-per-node=8
#SBATCH --gpus-per-node=8
#SBATCH --cpus-per-task=16
#SBATCH --mem=1280G
# The line below is to make sure that the job runs on all available nodes. Otherwise, the job runs on a single node.
#SBATCH --nodes=1-10000

# Run a multi-node MPI NCCL test
srun --mpi=pmix \
    bash -c 'echo "Run a multi-node all_reduce_perf_mpi on $(hostname) (rank $SLURM_PROCID):" && all_reduce_perf_mpi -b 512M -e 8G -f 2 -g 1'