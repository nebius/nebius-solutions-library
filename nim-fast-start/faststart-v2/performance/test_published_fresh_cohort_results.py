from __future__ import annotations

import csv
import hashlib
import math
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_EVIDENCE_ROOT = (
    "<private-evidence-root>/of2-boltz-n20-20260818T121158Z"
)

METRIC_COLUMNS = (
    "demand_to_http_ready_seconds",
    "demand_to_http_ready_boottime_upper_seconds",
    "demand_to_kubernetes_ready_seconds",
    "semantic_request_1_seconds",
    "semantic_request_2_seconds",
    "demand_to_first_semantic_seconds",
    "demand_to_first_semantic_boottime_upper_seconds",
    "demand_to_two_semantic_seconds",
    "demand_to_two_semantic_boottime_upper_seconds",
    "acceptance_response_proxy_to_http_ready_seconds",
    "acceptance_response_proxy_to_kubernetes_ready_seconds",
    "acceptance_response_proxy_to_first_semantic_seconds",
    "acceptance_response_proxy_to_two_semantic_seconds",
    "target_create_api_round_trip_seconds",
)

EXPECTED_HEADER = (
    "record_type",
    "cohort_id",
    "record",
    "run_id",
    "runner_qualification",
    "cleanup",
    "failed_attempt_denominator",
    *METRIC_COLUMNS,
    "cohort_outcome",
    "local_evidence",
)

COHORTS = {
    "openfold2": {
        "path": ROOT / "performance/openfold2/fresh-cohort-n20-results.tsv",
        "cohort_id": "of2-n20-v3-20260818t1421z",
        "run_prefix": "of2v3-1421",
        "outcome": "PASS",
        "primary": {
            "p50": (17.202273, 17.302540),
            "p95-nearest-rank": (17.532731, 17.629887),
            "max": (17.955461, 18.063099),
        },
    },
    "boltz2": {
        "path": ROOT / "boltz2-native/fresh-cohort-n20-results.tsv",
        "cohort_id": "b2-n20-v3-20260818t1532z",
        "run_prefix": "b2v3-1532",
        "outcome": "SLO_FAIL",
        "primary": {
            "p50": (28.794544, 28.892235),
            "p95-nearest-rank": (30.208757, 30.310246),
            "max": (30.923531, 31.022641),
        },
    },
}


def _read_tsv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        return list(reader.fieldnames or ()), list(reader)


def _nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(percentile * len(ordered)) - 1]


class PublishedFreshCohortResultTests(unittest.TestCase):
    def test_exact_n20_arrays_and_summaries(self) -> None:
        for model, config in COHORTS.items():
            with self.subTest(model=model):
                header, rows = _read_tsv(config["path"])
                self.assertEqual(tuple(header), EXPECTED_HEADER)
                samples = [row for row in rows if row["record_type"] == "sample"]
                summaries = {
                    row["record"]: row
                    for row in rows
                    if row["record_type"] == "summary"
                }
                self.assertEqual(len(rows), 23)
                self.assertEqual(len(samples), 20)
                self.assertEqual(
                    set(summaries), {"p50", "p95-nearest-rank", "max"}
                )
                self.assertEqual(
                    [row["record"] for row in samples],
                    [str(index) for index in range(1, 21)],
                )
                self.assertEqual(
                    [row["run_id"] for row in samples],
                    [
                        f'{config["run_prefix"]}-{index:03d}'
                        for index in range(1, 21)
                    ],
                )
                for row in rows:
                    self.assertEqual(row["cohort_id"], config["cohort_id"])
                    self.assertEqual(row["failed_attempt_denominator"], "0/20")
                    self.assertEqual(row["cohort_outcome"], config["outcome"])
                for row in samples:
                    self.assertEqual(row["runner_qualification"], "PASS")
                    self.assertEqual(row["cleanup"], "PASS")
                    self.assertEqual(
                        row["local_evidence"],
                        f'{PUBLIC_EVIDENCE_ROOT}/runs/{row["run_id"]}',
                    )
                    self.assertGreaterEqual(
                        float(row["demand_to_http_ready_boottime_upper_seconds"]),
                        float(row["demand_to_http_ready_seconds"]),
                    )
                    self.assertGreaterEqual(
                        float(row["demand_to_first_semantic_boottime_upper_seconds"]),
                        float(row["demand_to_first_semantic_seconds"]),
                    )
                    self.assertGreaterEqual(
                        float(row["demand_to_two_semantic_boottime_upper_seconds"]),
                        float(row["demand_to_two_semantic_seconds"]),
                    )
                for metric in METRIC_COLUMNS:
                    values = [float(row[metric]) for row in samples]
                    self.assertEqual(
                        float(summaries["p50"][metric]),
                        _nearest_rank(values, 0.50),
                    )
                    self.assertEqual(
                        float(summaries["p95-nearest-rank"][metric]),
                        _nearest_rank(values, 0.95),
                    )
                    self.assertEqual(float(summaries["max"][metric]), max(values))
                for row in summaries.values():
                    self.assertEqual(
                        row["local_evidence"],
                        f'{PUBLIC_EVIDENCE_ROOT}/cohorts/{config["cohort_id"]}',
                    )

    def test_primary_observed_and_conservative_upper_outcomes(self) -> None:
        for model, config in COHORTS.items():
            with self.subTest(model=model):
                _, rows = _read_tsv(config["path"])
                summaries = {
                    row["record"]: row
                    for row in rows
                    if row["record_type"] == "summary"
                }
                for statistic, (observed, upper) in config["primary"].items():
                    self.assertEqual(
                        float(summaries[statistic]["demand_to_two_semantic_seconds"]),
                        observed,
                    )
                    self.assertEqual(
                        float(
                            summaries[statistic][
                                "demand_to_two_semantic_boottime_upper_seconds"
                            ]
                        ),
                        upper,
                    )
                upper_p95 = config["primary"]["p95-nearest-rank"][1]
                self.assertEqual(config["outcome"] == "PASS", upper_p95 < 30.0)

    def test_retained_n3_result_tables_are_byte_identical(self) -> None:
        expected = {
            ROOT / "performance/openfold2/provisioned-response-boundary-results.tsv":
                "f35393d0f096989340f9cffb8d3a2b34e791ab5ab1bd7f0cb17d328b0a1b481f",
            ROOT / "boltz2-native/response-boundary-results.tsv":
                "327d60b74238e9a79a379f13daba329b0ffb38cf69db1dd52d7e13a363c6730d",
        }
        for path, digest in expected.items():
            with self.subTest(path=path):
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_documentation_pins_evidence_and_does_not_promote_boltz(self) -> None:
        openfold = (ROOT / "performance/openfold2/README.md").read_text()
        boltz = (ROOT / "boltz2-native/README.md").read_text()
        matrix = (ROOT / "performance/COLD_START_METRICS.md").read_text()
        for text, required in (
            (
                openfold,
                (
                    "803a4c139b99d3016e6b4a9ab922dfaf18a11acb51de0ed921a112d1f7a44587",
                    "cefad84839f1e1e1794715abcdffdd2b10cc2bb25867b4466e7d30ea5cabcd6a",
                    "d0436512a91c7ec6630678ae19c788c61147cf06ec92f30681c2447c6e216400",
                    "296c15052074d9c8b8310f24e87d3af19e2841ed9ffcc763b1fe614131ba52f2",
                    "0661b8875e553da04581086178089be450327df949ce24b4b5019edec7357c4b",
                    "0/20",
                ),
            ),
            (
                boltz,
                (
                    "e9512d5e1f61d64456c8ac1a05ebbe3365f4a82fb83244fa9faa5219210424f5",
                    "1cb85fd9b844e00f26e684267d526eac88023ee8faf4d217411e46a5f05c68c7",
                    "2023272a9476535895101bc40702611c4b4b0de389190ddcdbb946351c6a9900",
                    "a8d30d707ec273e1e9bd5fa35468cad7466e985e1a5515fcf1f593de67b18643",
                    "SLO FAIL",
                    "0/20",
                ),
            ),
            (matrix, ("17.629887", "30.310246", "SLO FAIL")),
        ):
            for value in required:
                self.assertIn(value, text)

    def test_publication_records_xid_and_raw_body_limitations(self) -> None:
        documents = {
            "shared": (ROOT / "performance/COLD_START_METRICS.md").read_text(),
            "openfold2": (ROOT / "performance/openfold2/README.md").read_text(),
            "boltz2": (ROOT / "boltz2-native/README.md").read_text(),
        }
        for name, text in documents.items():
            with self.subTest(document=name):
                normalized = " ".join(text.lower().split())
                self.assertIn("target-container gpu checks passed", normalized)
                self.assertIn("host-driver xid absence", normalized)
                self.assertIn("unavailable/unproven", normalized)
                self.assertIn("task-scoped privileged node-log collector", normalized)
                self.assertIn("80", normalized)
                self.assertIn("raw response bodies", normalized)
                self.assertIn("not copied", normalized)
                self.assertIn("response sha-256", normalized)
                self.assertIn("byte count", normalized)
                self.assertIn("semantic invariant", normalized)
                self.assertIn("pinned validator", normalized)
        self.assertIn("all 40 selected qualification receipts", documents["shared"])
        self.assertIn("all 20 OpenFold2 qualification receipts", documents["openfold2"])
        self.assertIn("all 20 Boltz2 qualification receipts", documents["boltz2"])


if __name__ == "__main__":
    unittest.main()
