# Fail-closed usage and cost ledger

This directory turns explicit resource receipts into auditable **usage**, then
optionally joins an explicit price snapshot. It never queries a provider,
discovers prices, or substitutes a guessed price. The example therefore remains
`INCOMPLETE`: every unavailable `currency`, `unit_price`, `amount`, and total is
`null`.

## Boundary and accounting contract

`build_usage_ledger.py` consumes one or more
`faststart-usage-receipt/v1` JSON files and emits a
`faststart-usage-ledger/v1` document governed by
`usage-ledger.schema.json`. Each receipt must explicitly declare every attempt,
its outcome, and absolute UTC timestamps. Counts are assertions: omitting a
failed attempt while retaining a larger declared count is rejected. Each failed
attempt also needs a `failed_attempt` resource interval from its T0 through its
failure timestamp. Every attempt and resource timestamp is first checked against
the observation window of the receipt that supplied it; a broader shard cannot
make another shard's out-of-window evidence valid.

Resource intervals are partitioned into:

- `node_provision`: creation request through usable node capacity; it must be
  the node's initial interval and end no later than its target attempt T0;
- `pre_t0_setup`: setup before the measured target create, including explicit
  image/cache preparation when it is in the accounting scope; it must name its
  target attempt and end no later than that attempt's T0;
- `gpu_critical_path`: T0 immediately before target create through the complete
  second semantic response. Its absolute boundaries must equal the attempt
  timestamps;
- `cleanup`: resource-retaining cleanup after its named attempt's complete
  second response or explicit failure;
- `idle_retained`: attached/provisioned capacity not doing setup or an attempt;
  this is the only phase allowed to have `end_at: null`; and
- `failed_attempt`: T0 through the explicit failure observation.

Except for `idle_retained`, every interval names the attempt that gives the
phase its meaning. The phases are workload phases and may be recorded for
multiple meters (for example node-seconds and GPU-seconds). Each resource
explicitly lists `applies_to_attempt_ids`. That list must exactly equal every
attempt assigned to that meter by the source receipt, and each listed attempt
must be fully contained by the resource lifecycle and have exactly one matching
active interval on that resource. Resources retained for another simultaneous
attempt can therefore remain explicitly non-applicable. The ledger never infers
applicability from temporal overlap, but once applicability is declared it is
impossible to relabel that node, GPU, or volume as setup, cleanup, or idle during
the attempt. Every attempt must be assigned to at least one resource meter.

`resource_id` identifies one billable meter, so a node and its GPU need distinct
IDs. A repeated resource ID is
accepted only when every copy says `shared: true` and has the same type, SKU,
unit, quantity, applicability list, and lifecycle. `allocated_at` and nullable
`released_at` bound that lifecycle. Intervals must cover it contiguously: a gap
must be represented as `idle_retained`, the first interval must start at
allocation, and the last must end at release. An unreleased resource must
therefore end in an open idle interval. Identical shared interval observations
are deduplicated and retain all source receipt IDs. A non-identical overlap is
rejected as ambiguous double-counting. Open intervals remain open with null
duration and usage; they are never clipped to build time. Only create separate
node/GPU meters when the provider bills them separately; do not split one
bundled charge into aliases.

All quantities and derived seconds are plain decimal strings. JSON binary
floats, exponents, negative quantities or intervals, nonmonotonic
milestones, overlapping resource intervals, unsupported/mismatched units, and
unaccounted successful or failed attempts fail closed. Integers are used only
for counts. Receipt order is not data: attempts and intervals use canonical
total-order keys so permuting equivalent receipt shards produces byte-identical
ledger JSON.

## What the current exact metrics mean

The exact warm-provisioned results in `../COLD_START_METRICS.md` provide attempt
milestones, not a bill. The mapping is:

| Existing metric | Ledger derivation | Cost interpretation |
|---|---|---|
| HTTP readiness | `t0_at` to `http_ready_at` | Latency only; it does not reveal how long a pre-existing node, GPU, or attached volume was retained. |
| First inference call (including deferred model load) | `call1_dispatched_at` to `call1_response_received_at` | Latency inside the critical path, not independently billable usage. |
| Second warm inference call | `call2_dispatched_at` to `call2_response_received_at` | Latency inside the critical path, not independently billable usage. |
| Exact total | `t0_at` to `call2_response_received_at` | Boundary for `gpu_critical_path`; resource quantity and the provider's actual billing meter are still required. |
| Validation completion | second response to `validation_completed_at` | Retained as `validation_tail`; excluded from exact total and not silently added to usage. Explicit resource receipts decide whether this time is critical, cleanup, or idle. |

Consequently, the current latency matrix can seed absolute attempt intervals but
cannot be presented as billed cost. Warm provisioned-node time before T0,
storage retention, node provisioning, cleanup, idle capacity, failed attempts,
billing increments, and authoritative effective-dated prices are separate
evidence.

## Receipt shape and commands

Each receipt has exactly five top-level fields: `schema_version` with value
`faststart-usage-receipt/v1`, a unique `receipt_id`, `run`, `attempts`, and
`resources`. The run declares its ID, model, measurement class, observation
window, and total/success/failure counts. A complete runnable example, including
a success, an explicit failure, attached storage, all six phases, and an open
retained interval, is in `examples/receipt.json`.
Build and validate without network access:

```bash
python3 build_usage_ledger.py \
  --receipt examples/receipt.json \
  --output examples/usage-ledger.json
python3 validate_usage_ledger.py examples/usage-ledger.json
```

## Explicit price join

`join_price_snapshot.py` is intentionally separate. A
`faststart-price-snapshot/v1` file must contain a price record for every
resource SKU and exact usage unit. Every record must explicitly carry
`effective_from` and `effective_to` (`null` means open-ended). The selected
record must cover the entire absolute resource interval; price changes are not
silently split or extrapolated. Overlapping records for the same SKU/unit are
rejected.

An `AVAILABLE` price requires a plain Decimal `unit_price`, currency, and source.
An `UNAVAILABLE` price requires null currency and unit price. Any unavailable
price or open usage interval makes the ledger total `INCOMPLETE`, with total
currency and value null. The joiner retains per-interval price provenance and
uses `decimal.Decimal` for multiplication and summation.

`COMPLETE` means only that every closed interval was multiplied and summed under
the supplied linear, exact-unit price records. It is not a provider invoice and
must not be described as one unless authoritative evidence also establishes the
provider's billing increments or minimums, bundling, discounts, taxes, and every
other invoice rule that applies. This v1 schema deliberately does not infer or
approximate those rules.

```bash
python3 join_price_snapshot.py \
  --ledger examples/usage-ledger.json \
  --price-snapshot examples/price-snapshot-unavailable.json \
  --output examples/usage-ledger-unavailable-cost.json
python3 validate_usage_ledger.py examples/usage-ledger-unavailable-cost.json
```

The checked-in unavailable snapshot is a contract example, not a price source.
No currency amount in it was fetched or inferred.

Run the focused tests with:

```bash
python3 -m unittest -v test_cost_ledger.py
```
