# OpenFold2 semantic receipt validator

`validate_openfold2.py` submits two deterministic, distinct 20-residue probes
directly to an OpenFold2 HTTP(S) origin. It does not discover or access a
Kubernetes cluster.

```bash
python3 nim-fast-start/faststart-v2/validate_openfold2.py \
  --base-url http://127.0.0.1:8000 \
  --receipt-dir /private/evidence/openfold2-run-001 \
  --run-id openfold2-run-001-a \
  --run-id openfold2-run-001-b \
  --ready-timeout 300
```

The base URL must be an origin without a path, credentials, query, or fragment.
Both run IDs are caller supplied, safe-string validated, and must be unique.
The validator always appends the exact route:

`/biology/openfold/openfold2/predict-structure-from-msa-and-template`

HTTP redirects are never followed, and process proxy settings are disabled.
When `--ready-timeout` is set, the validator first requires HTTP 200 with JSON
`true` or `{"status":"ready"}` from `/v1/health/ready`; readiness polling does
not consume either of the two semantic calls.
The receipt path must not already exist. It is created with mode `0700` and
contains the exact posted request bytes, exact raw response bytes, and a summary:

- `request-01.json`, `request-02.json`
- `response-01.raw`, `response-02.raw`
- `summary.json`

Every file is created exclusively with mode `0600`. A zero-byte response file
is retained when transport fails before response bytes arrive.

For online cases, `elapsed_seconds` is monotonic request-dispatch-to-complete-
body latency. `response_received_at` is captured at that boundary before the
raw response is persisted, hashed, decoded, or semantically validated. The
summary exposes `validation_finished_at` separately. Offline raw-HTTP checks
emit `validation_elapsed_seconds` and are never accepted as performance timing.

Run the offline unit suite with:

```bash
python3 -m unittest discover -s nim-fast-start/faststart-v2/tests -v
```
