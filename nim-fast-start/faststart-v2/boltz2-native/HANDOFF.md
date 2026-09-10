# Boltz2 native handoff

## Result and evidence

- `README.md`: qualification result, scope and rejected/excluded variants
- `response-boundary-results.tsv`: current response-body n=3 and median
- `results.tsv`: historical response-plus-validation n=3, rejected writeback
  trial and excluded p1
- Raw evidence root:
  `<private-evidence-root>/boltz2-native-f7-20260818T0310Z`
- Current counted run receipts: `runs/b2rb1-0924`, `runs/b2rb2-0925`,
  `runs/b2rb3-0927`
- Historical counted receipts: `runs/b2p2-0337`, `runs/b2p3-0343`, and
  `runs/b2p4-0344`
- Direct capture evidence: `podsnapshotcontent.json`, `capture-agent.log`,
  `manifest.yaml`, `artifact-inventory.txt`, `artifact-total-bytes.txt`
- Donor baseline: `donor-r5-strict-baseline.json`
- Rejected variant: `writeback-prewarm.log`,
  `writeback-artifact-verification.txt`, `writeback-variant-decision.json`, and
  `runs/b2wb1-0341`
- Excluded attempts: donor r1-r4 files plus `runs/b2p1-0333/excluded.json`

## Runnable files

- `validate_boltz2.py`: strict two-request external semantic validator
- `render.py`: Boltz target, one-shot worker and separate early-probe renderer
- `bind_target.py`: exact live UID/container/cgroup/PodSpec binding
- `restore-interface.live.json`: immutable reviewed worker/probe contract
- `run_one_native_trial.sh`: serial production-shaped trial driver
- `boltz2-donor-job.yaml`: warmed capture donor with the corrected actual-LF
  inline A3M request
- `boltz2-podsnapshotcontent.yaml.tmpl`: native capture object template
- `boltz2-cache-pvc.yaml` and `boltz2-cache-holder.yaml`: isolated cache assets
- `boltz2-writeback-prewarm-job.yaml`: rejected variant experiment, retained for
  reproducibility but not for rollout
- `tests/test_boltz2_native.py`: nine focused offline regression tests

## Runtime handoff state

At handoff, no Boltz Pod or GPU request remained, the Boltz r3 cache holder was
deleted, and no Boltz Pod mounted M3. The pre-existing CPU-only
`of2-artifact-holder-t12` was still Ready and was the sole M3-mounted Pod on
t12. The direct `boltz2-native-f7-v1` artifact and raw evidence were preserved.
The rejected writeback artifact was also left intact because the scale-zero
lane requested no further live work after t12 release.
