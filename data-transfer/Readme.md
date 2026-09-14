
# Data Transfer
This repository contains solutions for high-performance data transfer between Amazon S3 and local file storage across multiple nodes. The primary script provided is copy_s3_to_sfs_multi_node.sh, which enables efficient data synchronization.

## From S3 to file system / File system to S3

Script: [copy_s3_to_sfs_multi_node.sh](copy_s3_to_sfs_multi_node.sh)`copy_s3_to_sfs_multi_node.sh`

This script facilitates file transfers:

From S3 to local file storage

From local file storage to S3

Across multiple nodes simultaneously

It operates with or without SLURM, as long as SSH access to the nodes is available.

### Configuration


1. Define worker nodes: Specify the nodes that should be used in the script by modifying the NODES array: 
```
NODES=("worker-0" "worker-1" "worker-2" "worker-3")
```

Nodes can be identified by either IP addresses or hostnames.


2. S3 Configuration

Ensure that the S3 configuration is correctly set up in `~/.config/rclone/rclone.conf`. Below is an example configuration:

```
[s3]
type = s3
provider = AWS
env_auth = false
region = eu-west2
no_check_bucket = true
endpoint = https://storage.eu-west2.nebius.cloud:443
acl = private
bucket_acl = private
access_key_id = <your-access-key-id>
secret_access_key = <your-secret-access-key>
```

If you want to copy from one bucket to another one, make sure to provide two configs here. 

### With slurm

On slurm, as worker names, use the hostnames of the workes, e.g. `worker-0`

### Without instances

If you don't have slurm, you can simply use any private or public ip you have access to.

## From S3 to S3

In order to clone from one s3  bucket to another one, simply create two sections in the rsync.conf.

Then run 
```
rclone cp s3-profile1:<source-bucket> s3-profile2:<dest-bucket> --progress
```

## From SFS to SFS

To copy fast from one file system to another one can be done by [copy_sfs_to_sfs_multi_node.sh](copy_sfs_to_sfs_multi_node.sh)

This will split the files, ssh into nodes, and copy the files in parallel. 

### Configuration

In the script, define your source and destination nodes:
```
WORKER_NODES=("worker-0" "worker-1" "worker-2" "worker-3")       # List of worker nodes
DEST_NODES=("185.82.69.46" "185.82.69.40" "185.82.69.127" "185.82.69.126" ) # List of destination nodes
```

Make sure there are the same amount of worker and destination nodes, as every worker node will copy data to a destination node. 
The destination nodes have to be reachable from the worker nodes via ssh. The ssh user can be defined in 

```
DEST_USER="tux"                            # Username for destination nodes
```

finally, set the source and destination folders.  
```
SOURCE_BASE="/mnt/shared/source"            # Base directory for source files
DEST_BASE="/mnt/share/dest"               # Base directory for destination files
```


### On slurm

This script can be executed within the same slurm cluster, e.g. to clone a shared file system:

```
WORKER_NODES=("worker-0" "worker-1")       # List of worker nodes
DEST_NODES=( "worker-2" "worker-3") # List of destination nodes
SOURCE_BASE="/mnt/shared/"            # Base directory for source files
DEST_BASE="/mnt/different-shared/"               # Base directory for destination files
```

as well as sync data with a different cluster. If you want to e.g. copy data from your proprietary slurm cluster into nebius cloud, you can simply spin up a couple of vm instances (see [terraform.tfvars](..%2Fvm-instance%2Fterraform.tfvars)) with public ip, and copy the data to all nodes in parallel:

```
WORKER_NODES=("worker-0" "worker-1" "worker-2" "worker-3")       # List of worker nodes
DEST_NODES=("185.82.69.46" "185.82.69.40" "185.82.69.127" "185.82.69.126" ) # List of destination nodes
```

### On vm instances

If you don't use slurm, all you need is public or private ips on your worker nodes, and public ips on your destination nodes, as well as ssh access.

# Benchmarks

The expected performance is around 1 GB/s/node and should scale linear. 

For the shared file system, be aware that the performance changes with the size of the storage and therefore with the shards. Especially the write speed is impacted by that. 
