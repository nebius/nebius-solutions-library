#!/usr/bin/env python3

from __future__ import annotations

import copy
import json
import re
import tempfile
import unittest
from pathlib import Path

from ledgerlib import (
    LedgerError,
    build_usage_ledger,
    join_price_snapshot,
    load_json,
    validate_ledger,
)


HERE = Path(__file__).resolve().parent
EXAMPLE_RECEIPT = HERE / "examples" / "receipt.json"
EXAMPLE_PRICE = HERE / "examples" / "price-snapshot-unavailable.json"
FAKE_SHA = "a" * 64


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build(receipt: dict) -> dict:
    return build_usage_ledger([(receipt, FAKE_SHA)])


def render(value: dict) -> str:
    return json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"


def disjoint_shard(tag: str, sha_character: str) -> tuple[dict, str]:
    receipt = read(EXAMPLE_RECEIPT)
    receipt["receipt_id"] = f"receipt-{tag}"
    attempt_mapping: dict[str, str] = {}
    for attempt in receipt["attempts"]:
        old_id = attempt["attempt_id"]
        new_id = f"{old_id}-{tag}"
        attempt["attempt_id"] = new_id
        attempt_mapping[old_id] = new_id
    for resource in receipt["resources"]:
        resource["resource_id"] = f"{resource['resource_id']}-{tag}"
        resource["applies_to_attempt_ids"] = [
            attempt_mapping[attempt_id]
            for attempt_id in resource["applies_to_attempt_ids"]
        ]
        for interval in resource["intervals"]:
            if interval["attempt_id"] is not None:
                interval["attempt_id"] = attempt_mapping[interval["attempt_id"]]
    return receipt, sha_character * 64


def closed_receipt() -> dict:
    receipt = read(EXAMPLE_RECEIPT)
    receipt["run"]["observed_until"] = "2026-08-18T00:00:40Z"
    for resource in receipt["resources"]:
        resource["intervals"][-1]["end_at"] = "2026-08-18T00:00:40Z"
        resource["released_at"] = "2026-08-18T00:00:40Z"
    return receipt


def available_price() -> dict:
    snapshot = read(EXAMPLE_PRICE)
    snapshot["snapshot_id"] = "explicit-available-example"
    snapshot["currency"] = "XTS"
    for price in snapshot["prices"]:
        price["status"] = "AVAILABLE"
        price["currency"] = "XTS"
        price["source"] = "synthetic-decimal-test-only"
        price["unit_price"] = {
            "gpu-second": "2",
            "node-second": "3",
            "storage-gibibyte-second": "0.01",
        }[price["usage_unit"]]
    return snapshot


class BuildUsageLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.receipt = read(EXAMPLE_RECEIPT)

    def test_recomputes_absolute_metric_and_usage_intervals(self) -> None:
        ledger = build(self.receipt)
        derived = {
            interval["name"]: interval["duration_seconds"]
            for interval in ledger["attempts"][0]["derived_intervals"]
        }
        self.assertEqual(derived["demand_to_http_ready"], "8")
        self.assertEqual(derived["first_inference_call"], "2.3")
        self.assertEqual(derived["second_inference_call"], "0.6")
        self.assertEqual(derived["demand_to_second_response"], "11.1")
        gpu_critical = next(
            interval
            for resource in ledger["resources"]
            if resource["resource_type"] == "gpu"
            for interval in resource["intervals"]
            if interval["phase"] == "gpu_critical_path"
        )
        self.assertEqual(gpu_critical["duration_seconds"], "11.1")
        self.assertEqual(gpu_critical["usage_quantity"], "11.1")
        self.assertEqual(ledger["pricing"]["status"], "INCOMPLETE")
        self.assertIsNone(ledger["pricing"]["currency"])
        validate_ledger(ledger)

    def test_preserves_open_idle_intervals(self) -> None:
        ledger = build(self.receipt)
        open_intervals = [
            interval
            for resource in ledger["resources"]
            for interval in resource["intervals"]
            if interval["end_at"] is None
        ]
        self.assertEqual(len(open_intervals), 3)
        self.assertTrue(all(item["phase"] == "idle_retained" for item in open_intervals))
        self.assertTrue(all(item["duration_seconds"] is None for item in open_intervals))
        self.assertTrue(all(item["usage_quantity"] is None for item in open_intervals))

    def test_deduplicates_identical_shared_resource_observation(self) -> None:
        self.receipt["resources"].append(copy.deepcopy(self.receipt["resources"][0]))
        ledger = build(self.receipt)
        nodes = [item for item in ledger["resources"] if item["resource_type"] == "node"]
        self.assertEqual(len(nodes), 1)
        self.assertEqual(len(nodes[0]["intervals"]), 8)

    def test_deduplicates_shared_resource_across_receipt_shards(self) -> None:
        second = copy.deepcopy(self.receipt)
        second["receipt_id"] = "second-resource-observer"
        second["attempts"] = []
        second["run"]["attempt_count"] = 0
        second["run"]["successful_attempt_count"] = 0
        second["run"]["failed_attempt_count"] = 0
        second["resources"] = [copy.deepcopy(self.receipt["resources"][0])]
        ledger = build_usage_ledger(
            [(self.receipt, "a" * 64), (second, "b" * 64)]
        )
        node = next(item for item in ledger["resources"] if item["resource_type"] == "node")
        self.assertEqual(
            node["source_receipt_ids"],
            ["example-explicit-observation", "second-resource-observer"],
        )
        self.assertTrue(
            all(len(interval["source_receipt_ids"]) == 2 for interval in node["intervals"])
        )
        validate_ledger(ledger)

    def test_rejects_source_timestamps_outside_that_receipt_window(self) -> None:
        narrow = copy.deepcopy(self.receipt)
        narrow["receipt_id"] = "narrow-source"
        narrow["run"]["observed_until"] = "2026-08-18T00:00:05Z"
        broad = copy.deepcopy(self.receipt)
        broad["receipt_id"] = "broad-observer"
        broad["attempts"] = []
        broad["run"]["attempt_count"] = 0
        broad["run"]["successful_attempt_count"] = 0
        broad["run"]["failed_attempt_count"] = 0
        with self.assertRaisesRegex(LedgerError, "follows run.observed_until"):
            build_usage_ledger([(narrow, "a" * 64), (broad, "b" * 64)])

    def test_rejects_resource_observer_outside_own_window(self) -> None:
        observer = copy.deepcopy(self.receipt)
        observer["receipt_id"] = "late-resource-observer"
        observer["attempts"] = []
        observer["run"]["attempt_count"] = 0
        observer["run"]["successful_attempt_count"] = 0
        observer["run"]["failed_attempt_count"] = 0
        observer["run"]["observed_from"] = "2026-08-18T00:00:06Z"
        observer["resources"] = [observer["resources"][0]]
        with self.assertRaisesRegex(LedgerError, "precedes run.observed_from"):
            build_usage_ledger(
                [(self.receipt, "a" * 64), (observer, "b" * 64)]
            )

    def test_receipt_permutation_is_byte_identical_with_equal_t0(self) -> None:
        first = disjoint_shard("a", "a")
        second = disjoint_shard("b", "b")
        zero = {
            "interval_id": "gpu-zero-at-boundary",
            "attempt_id": None,
            "phase": "idle_retained",
            "start_at": "2026-08-18T00:00:22Z",
            "end_at": "2026-08-18T00:00:22Z",
        }
        first[0]["resources"][1]["intervals"].append(zero)
        forward = build_usage_ledger([first, second])
        reverse = build_usage_ledger([second, first])
        self.assertEqual(render(forward), render(reverse))
        self.assertEqual(
            [attempt["attempt_id"] for attempt in forward["attempts"]],
            [
                "attempt-001-a",
                "attempt-001-b",
                "attempt-002-a",
                "attempt-002-b",
            ],
        )

    def test_rejects_nonshared_duplicate_resource_id(self) -> None:
        self.receipt["resources"][0]["shared"] = False
        self.receipt["resources"].append(copy.deepcopy(self.receipt["resources"][0]))
        with self.assertRaisesRegex(LedgerError, "non-shared resource_id"):
            build(self.receipt)

    def test_rejects_shared_resource_identity_mismatch(self) -> None:
        duplicate = copy.deepcopy(self.receipt["resources"][0])
        duplicate["sku"] = "different-node-sku"
        self.receipt["resources"].append(duplicate)
        with self.assertRaisesRegex(LedgerError, "mismatched identity/SKU/unit"):
            build(self.receipt)

    def test_rejects_shared_resource_overlap(self) -> None:
        duplicate = copy.deepcopy(self.receipt["resources"][0])
        duplicate["intervals"][1]["interval_id"] = "ambiguous-overlap"
        self.receipt["resources"].append(duplicate)
        with self.assertRaisesRegex(LedgerError, "overlap.*double-count"):
            build(self.receipt)

    def test_rejects_negative_interval(self) -> None:
        interval = self.receipt["resources"][0]["intervals"][0]
        interval["end_at"] = "2026-08-17T23:59:59Z"
        with self.assertRaisesRegex(LedgerError, "negative/nonmonotonic"):
            build(self.receipt)

    def test_rejects_nonmonotonic_attempt_milestones(self) -> None:
        attempt = self.receipt["attempts"][0]
        attempt["call1_response_received_at"] = "2026-08-18T00:00:18.05Z"
        with self.assertRaisesRegex(LedgerError, "nonmonotonic success milestones"):
            build(self.receipt)

    def test_rejects_declared_failure_omission(self) -> None:
        self.receipt["run"]["attempt_count"] = 3
        self.receipt["run"]["failed_attempt_count"] = 2
        with self.assertRaisesRegex(LedgerError, "omits attempts"):
            build(self.receipt)

    def test_rejects_empty_attempt_ledger(self) -> None:
        self.receipt["attempts"] = []
        self.receipt["run"]["attempt_count"] = 0
        self.receipt["run"]["successful_attempt_count"] = 0
        self.receipt["run"]["failed_attempt_count"] = 0
        with self.assertRaisesRegex(LedgerError, "at least one explicit attempt"):
            build(self.receipt)

    def test_rejects_failed_attempt_usage_omission(self) -> None:
        for resource in self.receipt["resources"]:
            for interval in resource["intervals"]:
                if interval["phase"] == "failed_attempt":
                    interval["phase"] = "cleanup"
        with self.assertRaisesRegex(LedgerError, "cleanup starts before|active interval"):
            build(self.receipt)

    def test_rejects_success_critical_path_omission(self) -> None:
        for resource in self.receipt["resources"]:
            for interval in resource["intervals"]:
                if interval["phase"] == "gpu_critical_path":
                    interval["phase"] = "cleanup"
        with self.assertRaisesRegex(LedgerError, "cleanup starts before|active interval"):
            build(self.receipt)

    def test_rejects_cross_resource_attempt_phase_relabel(self) -> None:
        for resource in self.receipt["resources"][1:]:
            for interval in resource["intervals"]:
                if interval["phase"] in {"gpu_critical_path", "failed_attempt"}:
                    interval["phase"] = "idle_retained"
                    interval["attempt_id"] = None
        with self.assertRaisesRegex(LedgerError, "exactly one active interval"):
            build(self.receipt)

    def test_rejects_active_interval_removed_from_applicability(self) -> None:
        self.receipt["resources"][1]["applies_to_attempt_ids"].remove("attempt-001")
        with self.assertRaisesRegex(LedgerError, "not applicable"):
            build(self.receipt)

    def test_rejects_applicability_not_spanned_by_resource_lifecycle(self) -> None:
        self.receipt["resources"].append(
            {
                "resource_id": "short-lived-setup-helper",
                "resource_type": "cpu",
                "sku": "example-helper-cpu",
                "usage_unit": "vcpu-second",
                "quantity": "1",
                "shared": False,
                "applies_to_attempt_ids": ["attempt-001"],
                "allocated_at": "2026-08-18T00:00:00Z",
                "released_at": "2026-08-18T00:00:05Z",
                "intervals": [
                    {
                        "interval_id": "helper-setup",
                        "attempt_id": "attempt-001",
                        "phase": "pre_t0_setup",
                        "start_at": "2026-08-18T00:00:00Z",
                        "end_at": "2026-08-18T00:00:05Z",
                    }
                ],
            }
        )
        with self.assertRaisesRegex(LedgerError, "does not span the complete active interval"):
            build(self.receipt)

    def test_rejects_cleanup_before_its_named_attempt_terminal(self) -> None:
        cleanup = self.receipt["resources"][0]["intervals"][3]
        cleanup["attempt_id"] = "attempt-002"
        with self.assertRaisesRegex(LedgerError, "cleanup starts before"):
            build(self.receipt)

    def test_rejects_setup_after_target_attempt_t0(self) -> None:
        cleanup = self.receipt["resources"][0]["intervals"][3]
        cleanup["phase"] = "pre_t0_setup"
        with self.assertRaisesRegex(LedgerError, "pre_t0_setup ends after"):
            build(self.receipt)

    def test_rejects_noninitial_node_provision(self) -> None:
        cleanup = self.receipt["resources"][0]["intervals"][3]
        cleanup["phase"] = "node_provision"
        with self.assertRaisesRegex(LedgerError, "must be the initial resource interval"):
            build(self.receipt)

    def test_rejects_nonidle_phase_without_attempt(self) -> None:
        self.receipt["resources"][0]["intervals"][1]["attempt_id"] = None
        with self.assertRaisesRegex(LedgerError, "pre_t0_setup requires attempt_id"):
            build(self.receipt)

    def test_rejects_attempt_with_no_applicable_meter(self) -> None:
        for resource in self.receipt["resources"]:
            resource["applies_to_attempt_ids"] = []
            for interval in resource["intervals"]:
                if interval["phase"] in {"gpu_critical_path", "failed_attempt"}:
                    interval["phase"] = "idle_retained"
                    interval["attempt_id"] = None
                elif interval["phase"] == "cleanup":
                    interval["phase"] = "idle_retained"
                    interval["attempt_id"] = None
                elif interval["phase"] == "node_provision":
                    interval["phase"] = "pre_t0_setup"
        with self.assertRaisesRegex(LedgerError, "has no applicable resource meter"):
            build(self.receipt)

    def test_rejects_unaccounted_resource_gap(self) -> None:
        del self.receipt["resources"][0]["intervals"][4]
        with self.assertRaisesRegex(LedgerError, "unaccounted gap"):
            build(self.receipt)

    def test_json_loader_rejects_binary_float(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "float.json"
            path.write_text('{"quantity": 0.1}\n', encoding="utf-8")
            with self.assertRaisesRegex(LedgerError, "binary JSON float"):
                load_json(path)


class PriceJoinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = build(read(EXAMPLE_RECEIPT))
        self.snapshot = read(EXAMPLE_PRICE)

    def test_unavailable_price_stays_null_and_incomplete(self) -> None:
        joined = join_price_snapshot(self.ledger, self.snapshot, FAKE_SHA)
        self.assertEqual(joined["pricing"]["status"], "INCOMPLETE")
        self.assertIsNone(joined["pricing"]["currency"])
        self.assertIsNone(joined["pricing"]["total_cost"])
        self.assertEqual(
            joined["pricing"]["reason_codes"],
            ["OPEN_USAGE_INTERVAL", "PRICE_UNAVAILABLE"],
        )
        for resource in joined["resources"]:
            for interval in resource["intervals"]:
                self.assertEqual(interval["cost"]["status"], "INCOMPLETE")
                self.assertIsNone(interval["cost"]["currency"])
                self.assertIsNone(interval["cost"]["unit_price"])
                self.assertIsNone(interval["cost"]["amount"])
        validate_ledger(joined)

    def test_available_price_uses_exact_decimal_arithmetic(self) -> None:
        ledger = build(closed_receipt())
        joined = join_price_snapshot(ledger, available_price(), FAKE_SHA)
        self.assertEqual(joined["pricing"]["status"], "COMPLETE")
        self.assertEqual(joined["pricing"]["currency"], "XTS")
        self.assertEqual(joined["pricing"]["total_cost"], "230")
        self.assertEqual(joined["pricing"]["reason_codes"], [])
        validate_ledger(joined)

    def test_available_price_with_open_usage_has_null_total(self) -> None:
        joined = join_price_snapshot(self.ledger, available_price(), FAKE_SHA)
        self.assertEqual(joined["pricing"]["status"], "INCOMPLETE")
        self.assertEqual(joined["pricing"]["reason_codes"], ["OPEN_USAGE_INTERVAL"])
        self.assertIsNone(joined["pricing"]["currency"])
        self.assertIsNone(joined["pricing"]["total_cost"])

    def test_one_unavailable_closed_meter_nulls_total(self) -> None:
        snapshot = available_price()
        snapshot["prices"][0]["status"] = "UNAVAILABLE"
        snapshot["prices"][0]["currency"] = None
        snapshot["prices"][0]["unit_price"] = None
        snapshot["prices"][0]["source"] = None
        joined = join_price_snapshot(build(closed_receipt()), snapshot, FAKE_SHA)
        self.assertEqual(joined["pricing"]["reason_codes"], ["PRICE_UNAVAILABLE"])
        self.assertIsNone(joined["pricing"]["currency"])
        self.assertIsNone(joined["pricing"]["total_cost"])

    def test_rejects_missing_sku(self) -> None:
        self.snapshot["prices"] = [self.snapshot["prices"][0]]
        with self.assertRaisesRegex(LedgerError, "missing price SKU"):
            join_price_snapshot(self.ledger, self.snapshot, FAKE_SHA)

    def test_rejects_mismatched_unit(self) -> None:
        self.snapshot["prices"][0]["usage_unit"] = "node-second"
        with self.assertRaisesRegex(LedgerError, "price unit mismatch"):
            join_price_snapshot(self.ledger, self.snapshot, FAKE_SHA)

    def test_rejects_missing_effective_date(self) -> None:
        del self.snapshot["prices"][0]["effective_from"]
        with self.assertRaisesRegex(LedgerError, "missing required field.*effective_from"):
            join_price_snapshot(self.ledger, self.snapshot, FAKE_SHA)

    def test_rejects_effective_date_gap(self) -> None:
        for price in self.snapshot["prices"]:
            price["effective_from"] = "2026-08-18T00:00:06Z"
        with self.assertRaisesRegex(LedgerError, "effective dates do not uniquely cover"):
            join_price_snapshot(self.ledger, self.snapshot, FAKE_SHA)

    def test_rejects_negative_effective_interval(self) -> None:
        self.snapshot["prices"][0]["effective_to"] = "2026-07-31T00:00:00Z"
        with self.assertRaisesRegex(LedgerError, "negative/nonmonotonic effective dates"):
            join_price_snapshot(self.ledger, self.snapshot, FAKE_SHA)

    def test_rejects_overlapping_price_records(self) -> None:
        duplicate = copy.deepcopy(self.snapshot["prices"][0])
        duplicate["price_id"] = "ambiguous-price"
        self.snapshot["prices"].append(duplicate)
        with self.assertRaisesRegex(LedgerError, "overlap.*ambiguous"):
            join_price_snapshot(self.ledger, self.snapshot, FAKE_SHA)

    def test_rejects_interval_spanning_price_transition(self) -> None:
        snapshot = available_price()
        for price in list(snapshot["prices"]):
            price["effective_to"] = "2026-08-18T00:00:20Z"
            later = copy.deepcopy(price)
            later["price_id"] = f"{price['price_id']}-later"
            later["effective_from"] = "2026-08-18T00:00:20Z"
            later["effective_to"] = None
            snapshot["prices"].append(later)
        with self.assertRaisesRegex(LedgerError, "effective dates do not uniquely cover"):
            join_price_snapshot(build(closed_receipt()), snapshot, FAKE_SHA)

    def test_rejects_mixed_snapshot_currency(self) -> None:
        snapshot = available_price()
        snapshot["prices"][0]["currency"] = "EUR"
        with self.assertRaisesRegex(LedgerError, "currency mismatches snapshot currency"):
            join_price_snapshot(build(closed_receipt()), snapshot, FAKE_SHA)

    def test_rejects_available_price_without_source(self) -> None:
        snapshot = available_price()
        snapshot["prices"][0]["source"] = None
        with self.assertRaisesRegex(LedgerError, "requires currency and source"):
            join_price_snapshot(build(closed_receipt()), snapshot, FAKE_SHA)

    def test_rejects_exponent_price(self) -> None:
        snapshot = available_price()
        snapshot["prices"][0]["unit_price"] = "1e-3"
        with self.assertRaisesRegex(LedgerError, "plain decimal string"):
            join_price_snapshot(build(closed_receipt()), snapshot, FAKE_SHA)

    def test_rejects_binary_float_price(self) -> None:
        snapshot = available_price()
        snapshot["prices"][0]["unit_price"] = 2.5
        with self.assertRaisesRegex(LedgerError, "plain decimal string"):
            join_price_snapshot(build(closed_receipt()), snapshot, FAKE_SHA)


class ValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = build(read(EXAMPLE_RECEIPT))

    def test_rejects_tampered_duration(self) -> None:
        self.ledger["resources"][0]["intervals"][0]["duration_seconds"] = "999"
        with self.assertRaisesRegex(LedgerError, "duration_seconds was not recomputed"):
            validate_ledger(self.ledger)

    def test_rejects_tampered_attempt_metric(self) -> None:
        self.ledger["attempts"][0]["derived_intervals"][0]["duration_seconds"] = "999"
        with self.assertRaisesRegex(LedgerError, "do not match absolute timestamps"):
            validate_ledger(self.ledger)

    def test_rejects_duplicate_shared_resource_in_output(self) -> None:
        self.ledger["resources"].append(copy.deepcopy(self.ledger["resources"][0]))
        with self.assertRaisesRegex(LedgerError, "shared IDs must be deduplicated"):
            validate_ledger(self.ledger)

    def test_rejects_noncanonical_attempt_order(self) -> None:
        self.ledger["attempts"].reverse()
        with self.assertRaisesRegex(LedgerError, "canonical T0/attempt_id order"):
            validate_ledger(self.ledger)

    def test_rejects_tampered_resource_applicability(self) -> None:
        self.ledger["resources"][0]["applies_to_attempt_ids"].remove("attempt-001")
        with self.assertRaisesRegex(LedgerError, "not applicable"):
            validate_ledger(self.ledger)

    def test_rejects_tampered_decimal_cost(self) -> None:
        joined = join_price_snapshot(build(closed_receipt()), available_price(), FAKE_SHA)
        joined["resources"][0]["intervals"][0]["cost"]["amount"] = "0.1"
        with self.assertRaisesRegex(LedgerError, "does not match Decimal multiplication"):
            validate_ledger(joined)

    def test_schema_document_is_json_and_versioned(self) -> None:
        schema = read(HERE / "usage-ledger.schema.json")
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(
            schema["properties"]["schema_version"]["const"],
            "faststart-usage-ledger/v1",
        )
        positive_ref = schema["$defs"]["resource"]["properties"]["quantity"]["$ref"]
        self.assertEqual(positive_ref, "#/$defs/positive_decimal")
        pattern = schema["$defs"]["positive_decimal"]["pattern"]
        self.assertIsNone(re.fullmatch(pattern, "0"))
        self.assertIsNone(re.fullmatch(pattern, "0.000"))
        self.assertIsNotNone(re.fullmatch(pattern, "0.001"))
        self.assertIsNotNone(re.fullmatch(pattern, "1"))

    def test_checked_examples_are_exact_regenerations(self) -> None:
        receipt, receipt_sha = load_json(EXAMPLE_RECEIPT)
        ledger = build_usage_ledger([(receipt, receipt_sha)])
        self.assertEqual(ledger, read(HERE / "examples" / "usage-ledger.json"))
        snapshot, snapshot_sha = load_json(EXAMPLE_PRICE)
        joined = join_price_snapshot(ledger, snapshot, snapshot_sha)
        self.assertEqual(
            joined,
            read(HERE / "examples" / "usage-ledger-unavailable-cost.json"),
        )

    def test_docs_define_complete_as_arithmetic_not_invoice(self) -> None:
        readme = (HERE / "README.md").read_text(encoding="utf-8")
        self.assertIn("`COMPLETE` means only", readme)
        self.assertIn("not a provider invoice", readme)


if __name__ == "__main__":
    unittest.main()
