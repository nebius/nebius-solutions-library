#!/usr/bin/env python3
"""Offline tests for DiffDock rootfs inspection and immutable variant rendering."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


variant = load_module("rootfs_variant", ROOT / "rootfs_variant.py")
renderer = load_module("render_rootfs_variant", ROOT / "render_rootfs_variant.py")


def write_tar(path: Path, unsafe: bool = False) -> None:
    with tarfile.open(path, "w") as archive:
        for directory in (".", "./etc", "./usr", "./usr/lib", "./usr/lib/x86_64-linux-gnu"):
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            archive.addfile(info)
        files = {
            "./etc/ld.so.cache": b"generated-cache",
            "./usr/lib/x86_64-linux-gnu/libcuda.so.580.159.04": b"",
        }
        if unsafe:
            files["./opt/nim/generated-model-state.db"] = b"model state"
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def source_artifact(checkpoints: Path, unsafe: bool = False) -> Path:
    root = checkpoints / variant.SOURCE_ID / "versions" / "1"
    root.mkdir(parents=True)
    (root / "manifest.yaml").write_text(
        f"checkpointId: {variant.SOURCE_ID}\nsourceNode: {renderer.NODE}\n",
        encoding="utf-8",
    )
    (root / "inventory.img").write_bytes(b"inventory")
    (root / "pstree.img").write_bytes(b"pstree")
    (root / "pages-1.img").write_bytes(b"pages")
    write_tar(root / "rootfs-diff.tar", unsafe=unsafe)
    return root


class RootfsVariantTests(unittest.TestCase):
    def test_exact_runtime_only_delta_is_candidate_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoints = Path(directory)
            source_artifact(checkpoints)
            review = variant.inspect(checkpoints)
        self.assertTrue(review["eligible_for_rootfsless_candidate"])
        self.assertEqual(review["unclassified_members"], [])
        categories = {item["category"] for item in review["members"]}
        self.assertIn("runtime-ldconfig-state", categories)
        self.assertIn("nvidia-container-runtime", categories)

    def test_model_state_in_delta_is_not_candidate_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoints = Path(directory)
            source_artifact(checkpoints, unsafe=True)
            review = variant.inspect(checkpoints)
        self.assertFalse(review["eligible_for_rootfsless_candidate"])
        self.assertEqual(
            review["unclassified_members"],
            ["opt/nim/generated-model-state.db"],
        )

    def test_build_is_immutable_rootfsless_and_hardlinks_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoints = Path(directory)
            source = source_artifact(checkpoints)
            review = variant.inspect(checkpoints)
            review_sha = hashlib.sha256(variant._canonical(review)).hexdigest()
            receipt = variant.build(
                checkpoints,
                review["source_manifest_sha256"],
                review_sha,
            )
            destination = (
                checkpoints / variant.DESTINATION_ID / "versions" / variant.VERSION
            )
            self.assertFalse((destination / "rootfs-diff.tar").exists())
            self.assertEqual(
                (source / "pages-1.img").stat().st_ino,
                (destination / "pages-1.img").stat().st_ino,
            )
            self.assertTrue((destination / "rootfsless-review.json").is_file())
            self.assertEqual(receipt["status"], "PASS")
            self.assertFalse(receipt["rootfs_diff_present"])
            with self.assertRaisesRegex(variant.VariantError, "overwrite"):
                variant.build(
                    checkpoints,
                    review["source_manifest_sha256"],
                    review_sha,
                )

    def test_build_refuses_changed_review_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoints = Path(directory)
            source_artifact(checkpoints)
            review = variant.inspect(checkpoints)
            with self.assertRaisesRegex(variant.VariantError, "review digest changed"):
                variant.build(
                    checkpoints,
                    review["source_manifest_sha256"],
                    "0" * 64,
                )

    def test_renderer_pins_hf93_and_binds_build_to_both_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoints = root / "checkpoints"
            source_artifact(checkpoints)
            review = variant.inspect(checkpoints)
            review_path = root / "review.json"
            review_path.write_bytes(variant._canonical(review) + b"\n")
            artifact = {
                "schema": "archvteams.nebius.ai/diffdock-native-artifact-receipt/v1",
                "checkpoint_id": variant.SOURCE_ID,
                "artifact_version": "1",
                "manifest_sha256": review["source_manifest_sha256"],
            }
            artifact_path = root / "artifact.json"
            artifact_path.write_text(
                json.dumps(artifact, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            inspect_docs = renderer.render_inspect()
            build_docs = renderer.render_build(artifact_path, review_path)

        for documents in (inspect_docs, build_docs):
            self.assertTrue(documents[0]["immutable"])
            job = documents[1]
            spec = job["spec"]["template"]["spec"]
            values = spec["affinity"]["nodeAffinity"][
                "requiredDuringSchedulingIgnoredDuringExecution"
            ]["nodeSelectorTerms"][0]["matchExpressions"][0]["values"]
            self.assertEqual(values, [renderer.NODE])
        inspect_claim = inspect_docs[1]["spec"]["template"]["spec"]["volumes"][1][
            "persistentVolumeClaim"
        ]
        build_claim = build_docs[1]["spec"]["template"]["spec"]["volumes"][1][
            "persistentVolumeClaim"
        ]
        self.assertTrue(inspect_claim["readOnly"])
        self.assertFalse(build_claim["readOnly"])
        build_args = build_docs[1]["spec"]["template"]["spec"]["containers"][0]["args"]
        self.assertIn(review["source_manifest_sha256"], build_args)
        self.assertIn(hashlib.sha256(variant._canonical(review)).hexdigest(), build_args)


if __name__ == "__main__":
    unittest.main()
