"""Render an unmodified chart checkout; point SOPERATOR_MAIN_CHART at a main snapshot."""

import json
import os
from pathlib import Path
import subprocess
import unittest


MODULE = Path(__file__).resolve().parents[1]
RELEASE = "flux-system-soperator-fluxcd"


def documents(manifest):
    result = subprocess.check_output(
        ["yq", "eval-all", "-o=json", ". as $doc ireduce ([]; . + [$doc])", "-"],
        input=manifest, text=True,
    )
    return [doc for doc in json.loads(result) if doc]


@unittest.skipUnless(os.environ.get("SOPERATOR_MAIN_CHART"), "set SOPERATOR_MAIN_CHART to an unmodified main chart")
class MainChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chart = Path(os.environ["SOPERATOR_MAIN_CHART"])
        cls.rendered = subprocess.check_output([
            "helm", "template", RELEASE, str(cls.chart), "--namespace", "flux-system",
            "--values", str(MODULE / "values.yaml"),
        ], text=True)

    def filter(self, manifest):
        return subprocess.run(
            ["bash", str(MODULE / "scripts/filter_foundation.sh")],
            input=manifest, capture_output=True, text=True,
            env=dict(os.environ, FOUNDATION_RELEASE_NAME=RELEASE),
        )

    def test_main_needs_filter_and_renders_exact_foundation_after_it(self):
        before = documents(self.rendered)
        self.assertTrue(any(doc["metadata"]["name"] == RELEASE + "-slurm-cluster" for doc in before))
        result = self.filter(self.rendered)
        self.assertEqual(result.returncode, 0, result.stderr)
        after = documents(result.stdout)
        releases = {doc["metadata"]["name"]: doc for doc in after if doc["kind"] == "HelmRelease"}
        repositories = {doc["metadata"]["name"] for doc in after if doc["kind"] == "HelmRepository"}
        self.assertEqual(set(releases), {RELEASE + "-" + suffix for suffix in ["ns", "cert-manager", "storageclasses", "kruise"]})
        self.assertEqual(len(after), 8)
        for release in releases.values():
            self.assertIn(release["spec"]["chart"]["spec"]["sourceRef"]["name"], repositories)
            for dependency in release["spec"].get("dependsOn", []):
                self.assertIn(dependency["name"], releases)

    def test_missing_repository_fails_before_emitting_any_manifests(self):
        docs = [doc for doc in documents(self.rendered)
                if not (doc["kind"] == "HelmRepository" and doc["metadata"]["name"] == RELEASE + "-soperator")]
        result = self.filter("\n---\n".join(json.dumps(doc) for doc in docs))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_installed_helm_invokes_postrenderer(self):
        rendered = subprocess.check_output([
            "bash", str(MODULE / "scripts/helm_with_filter.sh"),
            "template", RELEASE, str(self.chart), "--namespace", "flux-system",
            "--values", str(MODULE / "values.yaml"),
        ], text=True, env=dict(os.environ, FOUNDATION_RELEASE_NAME=RELEASE))
        self.assertEqual(len(documents(rendered)), 8)

    def test_unavailable_dependency_fails_before_emitting_any_manifests(self):
        docs = documents(self.rendered)
        for doc in docs:
            if doc["kind"] == "HelmRelease" and doc["metadata"]["name"] == RELEASE + "-kruise":
                doc["spec"]["dependsOn"].append({"name": RELEASE + "-soperator"})
        result = self.filter("\n---\n".join(json.dumps(doc) for doc in docs))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_unexpected_workloads_are_not_installed(self):
        result = self.filter(self.rendered + '\n---\napiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: unexpected\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(documents(result.stdout)), 8)

    def test_flux_takeover_uses_same_helm_identity(self):
        bootstrap = subprocess.check_output([
            "helm", "template", "soperator-fluxcd-bootstrap",
            str(self.chart.parent / "soperator-fluxcd-bootstrap"), "--namespace", "flux-system",
        ], text=True)
        umbrella = next(doc for doc in documents(bootstrap) if doc["kind"] == "HelmRelease")
        spec = umbrella["spec"]
        identity = spec.get("releaseName", spec["targetNamespace"] + "-" + umbrella["metadata"]["name"])
        self.assertEqual(identity, RELEASE)
        self.assertTrue(spec["install"]["disableWait"])
        self.assertTrue(spec["upgrade"]["disableWait"])
        full = subprocess.check_output([
            "helm", "template", identity, str(self.chart), "--namespace", "flux-system",
        ], text=True)
        names = {doc["metadata"]["name"] for doc in documents(full) if doc["kind"] == "HelmRelease"}
        self.assertIn(RELEASE + "-soperator", names)
        self.assertIn(RELEASE + "-slurm-cluster", names)
