# Inspector plugin crash — reproduction harness (P32)

Found missing entirely in the third completeness pass. This is the real
diagnostic recipe that root-caused the Inspector plugin's use-after-free
crash (found during DLRM validation, fixed via the deferred-free
retirement queue already present in
`../inspector-plugin/inspector_plugin.cc` — see `INVENTORY.md` Category
G). The fix's own presence was already confirmed; this harness is the
supporting evidence for *why* that specific fix, not a re-validation.

A minimal, isolated, iterative reproduction — no model, no embeddings,
just raw `dist.all_to_all_single` calls:

- **`repro_alltoall.py`** (v1) — pure back-to-back AllToAll loop at a rate
  matching/exceeding DLRM's real fleet-wide call rate. Ran 50,000
  iterations clean — **disproved** "raw AllToAll call rate alone" as the
  trigger.
- **`repro_v2.py`** — reintroduces a custom `autograd.Function` (matching
  DLRM's real `alltoall_autograd`) and a real mix of AllReduce calls at
  DLRM's real parameter byte sizes. Also ran clean — disproved this combination too.
- **`repro_v3.py`** — targets the actual, eventually-confirmed hypothesis:
  a race between the Inspector dump thread (polling every
  `NCCL_INSPECTOR_DUMP_THREAD_INTERVAL_MICROSECONDS=500`) and the NCCL
  callback thread manipulating the same `commInfo`/`collInfo` structures,
  more likely to manifest at DLRM's real, slower per-iteration pace (more
  dump-thread polls per iteration) than the faster synthetic repros.

**Hardcoded paths found in this addition** (added to
`../STAGE2_HANDOFF.md`): each `run_repro*_node.sh` `cd`s into
`/root/P32_inspector_repro` and sets its own
`LD_LIBRARY_PATH=/root/nccl-2.28-src/build/lib:...` — a third instance of
this pattern beyond the two already documented for `long-context` and
`rl`.
