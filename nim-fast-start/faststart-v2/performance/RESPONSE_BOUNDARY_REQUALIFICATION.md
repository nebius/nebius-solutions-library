# Stable-lane response-boundary requalification

OpenFold2, Boltz2, ProteinMPNN, DiffDock, OpenFold3, GenMol, and MSA Search
completed coherent response-boundary n=3 reruns. Their call timers stop after
the complete HTTP body is received, and each run retains an absolute call-2
`response_received_at` from which the exact T0-to-call-2 total is computed.
Their current aggregate paths are:

- OpenFold2: `openfold2/provisioned-response-boundary-results.tsv`;
- Boltz2: `../boltz2-native/response-boundary-results.tsv`;
- ProteinMPNN: private aggregate
  `proteinmpnn-native-f7-response-20260818T114151Z/aggregate.json`, SHA-256
  `a19a7b8c618b771623c2f6df45267d125961d28a05b0e87eb8a40023ea5f88df`;
- DiffDock: private aggregate
  `diffdock-native-f7-response-20260818T1209Z/aggregate.json`, SHA-256
  `1e582f6c571e5d9af36e362b2f75df43fef035b7a7265780a5052e2531e88f24`;
- OpenFold3: private aggregate
  `<private-evidence-root>/openfold3-response-boundary/aggregate.json`, SHA-256
  `a8c8469759452aaf709aeeb5200e5b773337bae85788a94b7384e8f862d244f3`;
- GenMol: private aggregate
  `<private-evidence-root>/genmol-response-boundary-n3.json`,
  SHA-256
  `4739950f1032a77e896aa2673f139d9268e08d749a705fd8a86c0af58319548b`;
- MSA Search: private aggregate
  `msa-search-response-requal-20260818T111418Z/aggregate.json`, SHA-256
  `8b2e6a126d49ce49ed333d6e8b446d873856f66e9b9c3bf89e3b15eb94bbdb75`.

Their medians are 14.236758, 27.342018, 10.249097, 14.190621, 30.564921,
12.177434, and 4.942788 seconds
respectively from T0 through the second complete response. Historical
OpenFold2/Boltz2 response-plus-validation call columns and GenMol/MSA Search
validation-complete terminal values remain unchanged in explicitly legacy
evidence and must not be mixed with the corrected cohorts.

No stable production-shaped lane remains in the exact-total rerun queue.
Historical terminal timestamps that ended at validator completion remain only
as legacy T0-to-validation-complete evidence and are never relabeled as
T0-to-call-2.

Hash-bound historical evidence is not rewritten. Where an immutable historical
payload retains an old total key, the lane's current `results.json` and README
provide the authoritative validation-complete relabel.

Every rerun must emit the exact contract
`request-dispatch-to-complete-http-body/v1`. Each case must contain
`request_started_at`, `response_received_at`, and a monotonic
`elapsed_seconds` ending immediately after the complete HTTP body is read. The
summary must retain a separately named validation-completion timestamp.
Per-trial and n=3 T0-to-call-2 values must be computed from case 2's absolute
`response_received_at`, not from the sum of independently aggregated medians.

The completed OpenFold3 rerun retained the same warm-instance boundaries:
Ready GPU node, all three exact images resident, both storage volumes attached,
a fresh 9,263,246,107-byte full buffered read before T0, and T0 immediately
before target creation. Its exact total is computed per run from call 2's
absolute response timestamp.

ProteinMPNN, DiffDock, OpenFold3, GenMol, and MSA Search completed this
response-boundary requalification; MolMIM and RFdiffusion were qualified
directly under the same contract. None is part of the remaining rerun list.
ProteinMPNN's selected fully prewarmed buffered n=3 median is 10.249097 seconds
from T0 through the second complete response. DiffDock's selected median is
14.190621 seconds. RFdiffusion's selected
fully prewarmed buffered n=3 median is 31.379359 seconds from T0 through the
second complete response. OpenFold3's selected median is 30.564921 seconds.
Evo2-40B remains a manual H200 result until the
current owner-managed Deployment is explicitly released and a new
manifest-bound artifact can be captured and qualified.
