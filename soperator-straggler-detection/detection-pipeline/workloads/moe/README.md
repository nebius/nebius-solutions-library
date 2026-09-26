# MoE workload — 3 files found missing in this session's adversarial pass

The first packaging pass copied `train_moe.py`/`model_moe.py`/the 4
`run_moe_*.sh` variants/2 `train_node_moe*.sh` scripts, but missed 3 real,
load-bearing files this workload's rank-fault path actually depends on
(found by tracing the MoE fix's real historical validation file-by-file,
not by re-reading the same directory listing):

- **`configurator.py`** — `train_moe.py` does `exec(open('configurator.py').read())`
  (a relative file read, not a Python import), so it needs its own local
  copy sitting next to `train_moe.py`, unlike `nanogpt/`/`tp2/`/`fsdp/`
  which share `../nanogpt-base/configurator.py` via `sys.path`. Confirmed
  byte-identical to the shared copy — added directly here rather than
  symlinked, to match how `exec(open(...))` actually resolves it.
- **`rank_fault_wrapper.sh`** — `train_node_moe_rankfault.sh` execs this
  directly (via torchrun's `--no-python`) instead of `train_moe.py`
  directly. Without it, the rank-fault launch path (`run_moe_rankfault.sh`)
  is broken.
- **`qp_rate_limit_shim.c`** — a real, previously-undocumented **RDMA-layer
  fault-injection mechanism**, not GPU-clock or software-sleep based like
  every other fault in this package. `rank_fault_wrapper.sh` `LD_PRELOAD`s
  the compiled form of this file for exactly one target rank's process
  (`FAULT_TARGET_RANK`, default `4`) — it hooks the real `ibv_modify_qp()`
  and applies a real RDMA queue-pair send-rate limit the instant that
  rank's QPs reach RTS (ready-to-send), throttling only that one rank's
  RDMA layer with no cross-rank NCCL negotiation involved (an earlier
  candidate approach, per-rank `NCCL_MAX_NCHANNELS`, was tried and found to
  be a real dead end — NCCL rejects an asymmetric channel-count request
  across ranks of the same communicator outright). See the file's own
  header comment for the full real design rationale.

## Build requirement — not documented anywhere in this project's history

No build command for `qp_rate_limit_shim.c` was found anywhere in this
project's scripts or docs (confirmed via an exhaustive search this
session) — only the compiled `.so`'s presence on the original host. Based
on the source's own `#include`s (`dlfcn.h`, `infiniband/verbs.h`), the
real build requirement is:

```bash
gcc -shared -fPIC -o qp_rate_limit_shim.so qp_rate_limit_shim.c -ldl -libverbs
```

**This exact command is reconstructed from the source's own includes, not
independently verified by actually building it this session** — Stage 2
should confirm it compiles cleanly before relying on it. It needs
`libibverbs-dev` (or the equivalent RDMA development package) installed —
a real external build dependency not previously captured in
`../../INVENTORY.md` Category D's external-tool audit.

`rank_fault_wrapper.sh` also hardcodes `/root/P23_moe/` twice (the shim's
`.so` path and the `train_moe.py` path it execs) — an additional
portability item beyond the standard 2-node/8-GPU hardcoding, tracked in
the top-level README's known-limitations list.
