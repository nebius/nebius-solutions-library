"""Presigned upload/download URLs scoped to the tenant buckets (secret `tenant-storage` in the
tenant namespace of each cluster: access_key, secret_key, bucket, endpoint, region). On the control
cluster that Secret names the tenant's hub-region bucket: the bucket every fleet-placed run reads
its inputs from and writes its outputs to (the worker is unknown when the run is submitted)."""
import base64, json, time, uuid
from datetime import datetime, timedelta, timezone
import boto3
from botocore.config import Config
from fastapi import HTTPException
from kubernetes.client.rest import ApiException
from config import REGION, STORAGE_SECRET
import kube
from resilience import retry

_cache: dict[str, tuple[float, dict]] = {}


def storage(ns: str, region: str = REGION) -> dict:
    """The tenant's bucket in `region` (secret `tenant-storage` in the tenant namespace of that cluster)."""
    hit = _cache.get(f"{region}/{ns}")
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        data = retry(kube.core(region).read_namespaced_secret, STORAGE_SECRET, ns).data or {}
    except ApiException as e:
        raise HTTPException(503 if e.status != 404 else 404, f"tenant storage not configured in {region} ({e.reason})")
    d = {k: base64.b64decode(v).decode() for k, v in data.items()}
    d["client"] = boto3.client("s3", endpoint_url=d["endpoint"], region_name=d["region"], aws_access_key_id=d["access_key"],
                               aws_secret_access_key=d["secret_key"],
                               config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=15, retries={"max_attempts": 3, "mode": "standard"}))
    _cache[f"{region}/{ns}"] = (time.time() + 300, d)
    return d


def bucket_of(uri: str | None) -> str | None:
    return uri[5:].split("/", 1)[0] if uri and uri.startswith("s3://") else None


def storage_for(ns: str, uri: str | None, region: str = REGION) -> dict:
    """The tenant storage whose bucket holds `uri` (an operation's output prefix), searched over every
    cluster this API reaches; `region`'s storage when the uri names no bucket."""
    bucket = bucket_of(uri)
    if not bucket:
        return storage(ns, region)
    for r in [region] + [x for x in kube.regions() if x != region]:
        try:
            s = storage(ns, r)
        except HTTPException:
            continue
        if s["bucket"] == bucket:
            return s
    raise HTTPException(403, f"bucket {bucket} is not one of the tenant's buckets")


def presign_upload(ns: str, filename: str, content_type: str, expires_s: int, region: str = REGION) -> dict:
    s = storage(ns, region)
    key = f"uploads/{uuid.uuid4().hex[:12]}/{filename.strip('/').replace('..', '')}"
    url = s["client"].generate_presigned_url("put_object", Params={"Bucket": s["bucket"], "Key": key, "ContentType": content_type}, ExpiresIn=expires_s)
    return {"uri": f"s3://{s['bucket']}/{key}", "url": url, "method": "PUT", "headers": {"Content-Type": content_type},
            "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=expires_s)).strftime("%Y-%m-%dT%H:%M:%SZ")}


def presign_download(ns: str, uri: str, expires_s: int = 3600, region: str | None = None) -> dict:
    s = next((st for r in ([region] if region else kube.regions()) if uri.startswith(f"s3://{(st := storage(ns, r))['bucket']}/")), None)
    if not s:
        raise HTTPException(403, "artifact is not in one of the tenant's buckets")
    key = uri[len(f"s3://{s['bucket']}/"):]
    url = s["client"].generate_presigned_url("get_object", Params={"Bucket": s["bucket"], "Key": key}, ExpiresIn=expires_s)
    return {"name": key.rsplit("/", 1)[-1], "uri": uri, "url": url}


def read_json(ns: str, key: str, region: str = REGION, max_bytes: int = 4 << 20, prefix: str | None = None):
    """A small JSON object from the tenant bucket (an endpoint-call job's out/response.json); None when absent."""
    s = storage_for(ns, prefix, region)
    try:
        o = s["client"].get_object(Bucket=s["bucket"], Key=key)
    except s["client"].exceptions.NoSuchKey:
        return None
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"bucket read failed: {e}")
    body = o["Body"].read(max_bytes + 1)
    if len(body) > max_bytes:
        return {"truncated": True, "uri": f"s3://{s['bucket']}/{key}"}
    try:
        return json.loads(body)
    except ValueError:
        return body.decode(errors="replace")


def attempt_records(ns: str, op: str, region: str = REGION, prefix: str | None = None) -> list[dict]:
    """attempts/<pod>.json written by the uploader of every attempt (docs/JOBS.md): the record that
    outlives pods deleted by preemption. Empty when the bucket is unreachable (live pods still count).
    `prefix` (the Job's output prefix) names the bucket; else the region's tenant bucket."""
    try:
        s = storage_for(ns, prefix, region)
        resp = s["client"].list_objects_v2(Bucket=s["bucket"], Prefix=f"operations/{op}/attempts/")
        out = []
        for o in resp.get("Contents", []):
            r = read_json(ns, o["Key"], region, prefix=prefix)
            if isinstance(r, dict) and r.get("pod"):
                out.append(r)
        return out
    except Exception:  # noqa: BLE001
        return []


def list_outputs(ns: str, op: str, region: str = REGION, prefix: str | None = None) -> list[dict]:
    """Objects the job's uploader wrote under operations/<op>/ in the run's bucket (the output_prefix
    the API sets on every run and endpoint-call Job)."""
    s = storage_for(ns, prefix, region)
    out = []
    try:
        resp = s["client"].list_objects_v2(Bucket=s["bucket"], Prefix=f"operations/{op}/")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"bucket listing failed: {e}")
    for o in resp.get("Contents", []):
        a = presign_download(ns, f"s3://{s['bucket']}/{o['Key']}", region=None)
        a["size_bytes"] = o["Size"]
        out.append(a)
    return out
