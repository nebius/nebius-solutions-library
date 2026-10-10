"""Static, administrator-owned metadata for generated model pod templates."""
import re

from fastapi import HTTPException

from config import LABEL

NAME = re.compile(r"[A-Za-z0-9](?:[-_.A-Za-z0-9]*[A-Za-z0-9])?")
DNS = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?")
RESERVED = {LABEL, "kubernetes.io", "k8s.io", "kueue.x-k8s.io", "batch.kubernetes.io",
            "jobset.sigs.k8s.io", "serving.kserve.io", "serving.knative.dev", "autoscaling.knative.dev"}


def normalise(value: dict | None) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - {"labels", "annotations"}:
        raise HTTPException(400, "pod_metadata: only labels and annotations are supported")
    out = {}
    for field in ("labels", "annotations"):
        entries = value.get(field, {})
        if not isinstance(entries, dict) or len(entries) > 32:
            raise HTTPException(400, f"pod_metadata.{field}: at most 32 entries")
        for key, text in entries.items():
            if not isinstance(key, str) or not isinstance(text, str):
                raise HTTPException(400, f"pod_metadata.{field}: string keys and values required")
            parts = key.split("/")
            name = parts[-1]
            prefix = parts[0] if len(parts) == 2 else ""
            if (len(parts) > 2 or len(name) > 63 or not NAME.fullmatch(name) or
                    (len(parts) == 2 and (len(prefix) > 253 or not prefix or
                     any(len(label) > 63 or not DNS.fullmatch(label) for label in prefix.split("."))))):
                raise HTTPException(400, f"pod_metadata.{field}: invalid Kubernetes metadata key")
            if prefix in RESERVED or any(prefix.endswith("." + domain) for domain in ("k8s.io", "kubernetes.io")):
                raise HTTPException(400, "pod_metadata: platform, scheduler and serving metadata are reserved")
            if "{{" in text or (field == "labels" and (len(text) > 63 or (text and not NAME.fullmatch(text)))):
                raise HTTPException(400, f"pod_metadata.{field}: static valid metadata values required")
            if field == "annotations" and len(text.encode()) > 2048:
                raise HTTPException(400, "pod_metadata.annotations: values are limited to 2048 bytes")
        if entries:
            out[field] = dict(entries)
    return out
