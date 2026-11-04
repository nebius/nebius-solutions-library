import torch, torch.distributed as dist, os
dist.init_process_group("nccl"); r=dist.get_rank(); torch.cuda.set_device(int(os.environ["LOCAL_RANK"]))
x=torch.ones(1<<20,device="cuda")
for _ in range(50): dist.all_reduce(x)
torch.cuda.synchronize(); dist.barrier(); dist.destroy_process_group()
