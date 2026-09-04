#!/usr/bin/env python3
import json
import statistics
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ResultsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.results = json.loads((ROOT / "results.json").read_text())

    def test_selected_cohort_uses_response_body_boundary(self) -> None:
        result = self.results
        self.assertEqual(
            result["schema"],
            "archvteams.nebius.ai/genmol-production-shaped-results/v3",
        )
        self.assertEqual(result["response_boundary_requalification"]["status"], "PASS")
        self.assertEqual(
            result["timing_measurement"]["response_timing_contract"],
            "request-dispatch-to-complete-http-body/v1",
        )
        self.assertIn("response_received_at", result["timing_measurement"]["exact_total_source"])
        self.assertEqual(
            len(set(result["response_boundary_requalification"]["selected_run_ids"])),
            3,
        )

    def test_selected_arrays_and_medians_are_coherent(self) -> None:
        selected = self.results["buffered"]
        fields = {
            "demand_to_http_ready": "demand_to_http_ready_seconds",
            "demand_to_kubernetes_ready": "demand_to_kubernetes_ready_seconds",
            "semantic_request_1": "semantic_request_1_seconds",
            "semantic_request_2": "semantic_request_2_seconds",
            "demand_to_two_semantic": "demand_to_two_semantic_seconds",
            "worker_restore": "worker_restore_seconds",
        }
        for median_key, values_key in fields.items():
            values = selected[values_key]
            self.assertEqual(len(values), 3)
            self.assertEqual(
                selected["median_seconds"][median_key], statistics.median(values)
            )

        self.assertNotIn("legacy_demand_to_validation_complete_seconds", selected)
        self.assertIn(
            "demand_to_validation_complete_seconds",
            self.results["legacy_buffered_cohort"],
        )

    def test_reported_speedups_use_selected_medians(self) -> None:
        direct = self.results["direct"]["median_seconds"]
        buffered = self.results["buffered"]["median_seconds"]
        speedup = self.results["speedup"]
        self.assertAlmostEqual(
            speedup["demand_to_http_ready"],
            direct["demand_to_http_ready"] / buffered["demand_to_http_ready"],
        )
        self.assertAlmostEqual(
            speedup["worker_restore"],
            direct["worker_restore"] / buffered["worker_restore"],
        )
        self.assertIsNone(speedup["exact_total_direct_comparator"])


if __name__ == "__main__":
    unittest.main()
