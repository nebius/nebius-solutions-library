#!/usr/bin/env python3
"""FSDP2 training example that checkpoints to Nebius Object Storage.

Demonstrates the recommended checkpointing pattern for Soperator clusters:

- FSDP2-sharded model/optimizer state + PyTorch Distributed Checkpoint (DCP):
  every rank uploads only its own shard, directly to Object Storage
  (no shared-FS staging, no rank-0 gather).
- ``dcp.async_save``: training blocks only for the GPU->host staging (typically
  well under a second), the upload happens in the background.
- Atomic-commit protocol: a checkpoint only "exists" once the small ``latest``
  marker object points at it. Jobs killed mid-upload never corrupt the resume
  path - the marker still points at the previous complete checkpoint.
- Auto-resume: on start, the script reads the marker and continues from the
  latest complete checkpoint if one exists. To start over, use a new prefix
  (the default prefix is already unique per submission).

Credentials/endpoint come from the environment (see checkpoint_train.sbatch,
which sources /etc/nebius-checkpoints.env delivered by the checkpoints_access
Terraform module):
    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY (S3-protocol SDK compatibility),
    NEBIUS_OBJECT_STORAGE_ENDPOINT, NEBIUS_OBJECT_STORAGE_REGION,
    NEBIUS_CHECKPOINT_BUCKET
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import threading
import time

import boto3
import torch
import torch.distributed as dist
import torch.distributed.checkpoint as dcp
import torch.nn as nn
from botocore.config import Config
from torch.distributed.checkpoint.state_dict import get_state_dict, set_state_dict
from torch.distributed.fsdp import fully_shard

from s3torchconnector import S3ClientConfig
from s3torchconnector.dcp import S3StorageReader, S3StorageWriter


CHECKPOINT_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def log(msg: str) -> None:
    if int(os.environ.get("RANK", "0")) == 0:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class CheckpointDemoModel(nn.Sequential):
    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        # The outer FSDP2 wrapper requires a non-view output so its backward
        # hook cannot be lost if downstream code later performs an in-place op.
        return super().forward(inputs).clone()


def build_model(hidden: int, layers: int) -> nn.Module:
    return CheckpointDemoModel(
        nn.Embedding(1024, hidden),
        *[
            nn.TransformerEncoderLayer(
                hidden,
                8,
                hidden * 4,
                dropout=0.0,
                batch_first=True,
            )
            for _ in range(layers)
        ],
        nn.Linear(hidden, 1024),
    )


class ObjectStorageCheckpointManager:
    """Async DCP checkpoints to Nebius Object Storage with an atomic marker."""

    def __init__(
        self,
        bucket: str,
        prefix: str,
        region: str,
        endpoint: str,
        control_group,
        keep_last: int = 3,
    ):
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.region = region
        self.endpoint = endpoint
        self.keep_last = keep_last
        self.control_group = control_group
        self.s3client_config = S3ClientConfig(force_path_style=True)
        # boto3 is only used for the tiny marker and retention operations.
        self._object_storage = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region,
            config=Config(
                s3={"addressing_style": "path"},
                retries={"max_attempts": 5, "mode": "standard"},
            ),
        )
        self._pending: threading.Thread | None = None
        # PyTorch DCP's CheckpointException derives directly from BaseException,
        # not Exception. Keep the wider type so upload failures cannot escape the
        # background thread and let training continue without persistence.
        self._commit_error: BaseException | None = None

    def _step_uri(self, step: int) -> str:
        return f"s3://{self.bucket}/{self.prefix}/step-{step:08d}/"

    def _writer(self, step: int) -> S3StorageWriter:
        return S3StorageWriter(
            region=self.region,
            path=self._step_uri(step),
            s3client_config=self.s3client_config,
        )

    def read_marker(self):
        """Return the committed step, or None if no checkpoint exists.

        Only a definitive "key does not exist" means no checkpoint. Any other
        error (network, throttling, auth) is retried and then raised: treating
        a transient read failure as "no checkpoint" would silently restart
        training from scratch.
        """
        last_err = None
        for attempt in range(5):
            try:
                body = self._object_storage.get_object(
                    Bucket=self.bucket, Key=f"{self.prefix}/latest"
                )["Body"].read()
                text = body.decode("ascii").strip()
                if not text.isdigit():
                    raise RuntimeError(
                        f"checkpoint marker is not a non-negative integer: {text!r}"
                    )
                return int(text)
            except self._object_storage.exceptions.NoSuchKey:
                return None
            except Exception as e:  # noqa: BLE001 - retried, then re-raised below
                code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
                if code in ("NoSuchKey", "NotFound", "404"):
                    return None
                last_err = e
                time.sleep(2**attempt)
        raise RuntimeError(
            f"cannot read checkpoint marker (refusing to start fresh): {last_err}"
        )

    def _write_marker(self, step: int) -> None:
        self._object_storage.put_object(
            Bucket=self.bucket,
            Key=f"{self.prefix}/latest",
            Body=str(step).encode(),
        )

    def _prune(self, keep_last: int, committed_step: int) -> None:
        """Retention, driven by the just-committed marker step.

        Runs only while no save is in flight (saves are serialized), so:
        - steps ABOVE the marker are leftovers of killed/aborted uploads - garbage;
        - retention counts only steps at or below the marker (committed history);
        - the marker target is the newest committed step and is always kept.
        Counting partials toward retention would let a killed high-numbered save
        push the marker's own checkpoint out of the keep window.
        """
        if keep_last <= 0:
            return
        steps = set()
        step_prefix_re = re.compile(rf"^{re.escape(self.prefix)}/step-([0-9]+)/$")
        paginator = self._object_storage.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=self.bucket, Prefix=f"{self.prefix}/step-", Delimiter="/"
        ):
            for cp in page.get("CommonPrefixes", []):
                match = step_prefix_re.fullmatch(cp["Prefix"])
                if match:
                    steps.add(int(match.group(1)))
        stale_partials = sorted(s for s in steps if s > committed_step)
        committed = sorted(s for s in steps if s <= committed_step)
        doomed = [(s, "stale partial") for s in stale_partials]
        doomed += [(s, "retention") for s in committed[:-keep_last]]
        for step, reason in doomed:
            keys = []
            for page in paginator.paginate(
                Bucket=self.bucket, Prefix=f"{self.prefix}/step-{step:08d}/"
            ):
                keys += [{"Key": o["Key"]} for o in page.get("Contents", [])]
            for i in range(0, len(keys), 1000):
                response = self._object_storage.delete_objects(
                    Bucket=self.bucket,
                    Delete={"Objects": keys[i : i + 1000]},
                )
                errors = response.get("Errors", [])
                if errors:
                    first = errors[0]
                    raise RuntimeError(
                        f"failed to delete {len(errors)} checkpoint objects at step {step}; "
                        f"first error: {first.get('Code', 'unknown')}: "
                        f"{first.get('Message', 'no message')}"
                    )
            log(f"pruned checkpoint step {step} ({reason}, {len(keys)} objects)")

    def wait_pending(self) -> None:
        """Wait until the in-flight upload (if any) is finished and committed.

        Raises if the background upload/commit failed: a checkpoint that
        silently stops persisting is lost progress waiting to happen.
        """
        if self._pending is not None:
            self._pending.join()
            self._pending = None
        self._raise_if_any_rank_failed(
            self._commit_error,
            "background checkpoint upload/commit failed",
        )

    def _raise_if_any_rank_failed(
        self, local_error: BaseException | None, message: str
    ) -> None:
        """Make a rank-local persistence error fatal on every training rank.

        Without this rendezvous, the failing rank raises while its peers enter the
        next DCP collective, turning an actionable upload error into a hung job.
        The dedicated Gloo group is never used by async DCP.
        """
        failed = torch.tensor(1 if local_error is not None else 0, dtype=torch.int32)
        dist.all_reduce(failed, op=dist.ReduceOp.MAX, group=self.control_group)
        if not failed.item():
            return
        if local_error is not None:
            raise RuntimeError(message) from local_error
        raise RuntimeError(f"{message} on another rank")

    def save_async(self, state_dict: dict, step: int) -> float:
        """Start an async save; returns the training-blocking time in seconds.

        The `latest` marker is committed from a background thread as soon as the
        upload completes - independent of the training loop.
        """
        # Only one save in flight: wait for the previous one first.
        self.wait_pending()
        rank = dist.get_rank()
        t0 = time.perf_counter()
        future = dcp.async_save(state_dict, storage_writer=self._writer(step))
        blocked_s = time.perf_counter() - t0

        def _commit():
            try:
                future.result()
                # Rank 0 coordinates DCP's final .metadata write, so its future
                # resolving means the checkpoint is globally complete.
                if rank == 0:
                    self._write_marker(step)
                    upload_s = time.perf_counter() - t0
                    log(
                        f"checkpoint step {step}: upload finished in {upload_s:.1f}s, marker committed"
                    )
                    self._prune(self.keep_last, step)
            except BaseException as exc:  # noqa: BLE001 - DCP uses BaseException
                self._commit_error = exc

        self._pending = threading.Thread(target=_commit, daemon=True)
        self._pending.start()
        return blocked_s

    def save_sync(self, state_dict: dict, step: int) -> None:
        """Blocking save + marker commit (used for the final/SIGTERM save)."""
        self.wait_pending()
        dcp.save(state_dict, storage_writer=self._writer(step))
        commit_error = None
        if dist.get_rank() == 0:
            try:
                self._write_marker(step)
                self._prune(self.keep_last, step)
            except Exception as exc:  # noqa: BLE001 - propagated collectively below
                commit_error = exc
        self._raise_if_any_rank_failed(
            commit_error, "synchronous checkpoint commit failed"
        )
        log(f"checkpoint step {step}: synchronous save committed")

    def load(self, model, optimizer, step: int) -> None:
        reader = S3StorageReader(
            region=self.region,
            path=self._step_uri(step),
            s3client_config=self.s3client_config,
        )
        model_sd, optim_sd = get_state_dict(model, optimizer)
        extra = {"step": torch.tensor(0)}
        dcp.load(
            {"model": model_sd, "optim": optim_sd, "extra": extra},
            storage_reader=reader,
        )
        loaded_step = int(extra["step"].item())
        if loaded_step != step:
            raise RuntimeError(
                f"checkpoint payload says step {loaded_step}, but its marker says step {step}"
            )
        set_state_dict(
            model, optimizer, model_state_dict=model_sd, optim_state_dict=optim_sd
        )


def validate_checkpoint_prefix(value: str) -> str:
    prefix = value.strip().strip("/")
    if not prefix or not CHECKPOINT_PREFIX_RE.fullmatch(prefix):
        raise ValueError("must contain only letters, digits, '.', '_', '-', and '/'")
    if any(part in ("", ".", "..") for part in prefix.split("/")):
        raise ValueError("contains an unsafe path segment")
    return prefix


def run_on_rank_zero(operation, description: str, control_group):
    """Run an Object Storage control-plane operation once and broadcast its result."""
    local_error = None
    result = None
    if dist.get_rank() == 0:
        try:
            result = operation()
        except Exception as exc:  # noqa: BLE001 - serialized for every peer below
            local_error = exc
    payload = [
        result,
        None if local_error is None else f"{type(local_error).__name__}: {local_error}",
    ]
    dist.broadcast_object_list(payload, src=0, group=control_group)
    if payload[1] is not None:
        if local_error is not None:
            raise RuntimeError(f"{description} failed on rank 0") from local_error
        raise RuntimeError(f"{description} failed on rank 0: {payload[1]}")
    return payload[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hidden", type=int, default=1024)
    parser.add_argument("--layers", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--seq-len", type=int, default=512)
    parser.add_argument(
        "--steps", type=int, default=10_000_000, help="Stop after this many steps."
    )
    parser.add_argument(
        "--save-every-seconds",
        type=float,
        default=120.0,
        help="Checkpoint cadence. See README for how to size this (Young/Daly).",
    )
    parser.add_argument(
        "--save-every-steps",
        type=int,
        default=None,
        help="Optional step-based cadence; overrides --save-every-seconds.",
    )
    parser.add_argument(
        "--prefix",
        default=None,
        help="Key prefix inside the bucket. Defaults to <job name>-<job id>, which "
        "isolates independent submissions from each other while staying stable "
        "across requeues of the same job. Pass an explicit prefix to deliberately "
        "resume across separate submissions or from another cluster.",
    )
    parser.add_argument(
        "--keep-last",
        type=int,
        default=3,
        help="Retention: keep this many newest checkpoints, prune older ones (0 keeps all).",
    )
    args = parser.parse_args()

    if args.hidden <= 0 or args.hidden % 8:
        parser.error(
            "--hidden must be a positive multiple of the model's 8 attention heads"
        )
    if min(args.layers, args.batch_size, args.seq_len, args.steps) <= 0:
        parser.error("--layers, --batch-size, --seq-len, and --steps must be positive")
    if args.save_every_seconds <= 0:
        parser.error("--save-every-seconds must be positive")
    if args.save_every_steps is not None and args.save_every_steps <= 0:
        parser.error("--save-every-steps must be positive")
    if args.keep_last < 0:
        parser.error("--keep-last must be non-negative")

    bucket = os.environ.get("NEBIUS_CHECKPOINT_BUCKET", "").strip()
    endpoint = os.environ.get("NEBIUS_OBJECT_STORAGE_ENDPOINT", "").strip()
    region = os.environ.get("NEBIUS_OBJECT_STORAGE_REGION", "").strip() or "eu-north1"
    if not bucket:
        parser.error("NEBIUS_CHECKPOINT_BUCKET is required")
    if not endpoint:
        parser.error("NEBIUS_OBJECT_STORAGE_ENDPOINT is required")

    if args.prefix is None:
        job_name = os.environ.get("SLURM_JOB_NAME", "checkpointing-demo")
        job_id = os.environ.get("SLURM_JOB_ID")
        # Job ID survives requeues (unattended resume keeps working) but differs
        # between submissions (no accidental sharing of `latest` between runs).
        args.prefix = f"{job_name}-{job_id}" if job_id else job_name
    try:
        args.prefix = validate_checkpoint_prefix(args.prefix)
    except ValueError as exc:
        parser.error(f"invalid --prefix: {exc}")

    # async_save stages via a CPU (gloo) process group alongside NCCL.
    dist.init_process_group(backend="cpu:gloo,cuda:nccl")
    rank = dist.get_rank()
    local_rank = int(os.environ["LOCAL_RANK"])
    torch.cuda.set_device(local_rank)
    device = torch.device("cuda", local_rank)
    # DCP async saves use the default group's Gloo backend from a background
    # thread. Keep main-thread control collectives on a separate process group
    # so their ordering cannot interleave with DCP collectives across ranks.
    control_group = dist.new_group(backend="gloo")

    # FSDP2 (fully_shard): model and optimizer state are sharded across ranks, so
    # every rank checkpoints only its own shard - this is what makes the DCP
    # saves in this example genuinely parallel per-rank uploads.
    # Initialize identical parameters on every rank. Dropout is disabled in the
    # toy model because this example does not include per-rank RNG state in DCP.
    torch.manual_seed(0)
    torch.cuda.manual_seed_all(0)
    model = build_model(args.hidden, args.layers).to(device)
    n_params = sum(p.numel() for p in model.parameters())
    for module in model:
        if isinstance(module, nn.TransformerEncoderLayer):
            fully_shard(module)
    fully_shard(model)
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-4)
    log(
        f"world={dist.get_world_size()} params={n_params / 1e6:.0f}M (FSDP2-sharded) "
        f"checkpoint~{n_params * 12 / 1e9:.1f}GB bucket={bucket} prefix={args.prefix}"
    )

    ckpt = ObjectStorageCheckpointManager(
        bucket,
        args.prefix,
        region,
        endpoint,
        control_group,
        keep_last=args.keep_last,
    )

    # Resume from the latest complete checkpoint under the prefix.
    start_step = 0
    marker = run_on_rank_zero(ckpt.read_marker, "checkpoint marker read", control_group)
    last_checkpoint_step = marker
    if marker is not None:
        log(f"resuming from checkpoint step {marker}")
        ckpt.load(model, optimizer, marker)
        start_step = marker
    else:
        log("no checkpoint found, starting fresh")

    # On SIGTERM/SIGUSR1: commit the current step so no progress is lost, then
    # exit non-zero. Automatic requeue still depends on
    # the scheduler event and site policy; a user cancellation is not requeued.
    stop_requested = {"flag": False}

    def _stop_handler(_signum, _frame):
        stop_requested["flag"] = True

    signal.signal(signal.SIGTERM, _stop_handler)
    signal.signal(signal.SIGUSR1, _stop_handler)

    loss_fn = nn.CrossEntropyLoss()
    last_save = time.monotonic()
    step = start_step
    stop_now = False

    while step < args.steps and not stop_now:
        step += 1
        # Fresh batch each step (seeded by step for reproducibility across resume).
        gen = torch.Generator(device="cpu").manual_seed(
            step * dist.get_world_size() + rank
        )
        data = torch.randint(
            0, 1024, (args.batch_size, args.seq_len), generator=gen
        ).to(device)
        optimizer.zero_grad(set_to_none=True)
        out = model(data)
        loss = loss_fn(out.view(-1, 1024), data.view(-1))
        loss.backward()
        optimizer.step()

        # Save/stop decisions must be identical on every rank: DCP saves and the
        # final checkpoint are collective operations, so a rank acting alone
        # (wall-clock skew, a signal delivered to only some ranks yet) would hang
        # the job. Rank 0 decides saves; stop requests are OR-reduced. The
        # all_reduce runs on a dedicated Gloo group that async DCP never uses.
        control = torch.zeros(2, dtype=torch.int32)
        if rank == 0:
            due = (
                args.save_every_steps is not None and step % args.save_every_steps == 0
            ) or (
                args.save_every_steps is None
                and time.monotonic() - last_save >= args.save_every_seconds
            )
            control[0] = 1 if due else 0
        control[1] = 1 if stop_requested["flag"] else 0
        dist.all_reduce(control, op=dist.ReduceOp.MAX, group=control_group)

        if control[0]:
            model_sd, optim_sd = get_state_dict(model, optimizer)
            state = {
                "model": model_sd,
                "optim": optim_sd,
                "extra": {"step": torch.tensor(step)},
            }
            blocked_s = ckpt.save_async(state, step)
            last_checkpoint_step = step
            last_save = time.monotonic()
            log(
                f"step {step} loss {loss.item():.4f}: async_save blocked training for {blocked_s * 1000:.0f}ms"
            )
        elif step % 50 == 0:
            log(f"step {step} loss {loss.item():.4f}")
        if control[1]:
            log(
                "stop signal observed at a safe step boundary; checkpointing before exit"
            )
        stop_now = bool(control[1])

    # Final checkpoint: synchronous unless this exact step is already being
    # persisted. Never rewrite a prefix that `latest` may already reference: a
    # kill during that redundant overwrite could corrupt the committed target.
    if last_checkpoint_step == step:
        ckpt.wait_pending()
        log(
            f"checkpoint step {step}: existing save committed; skipped duplicate final write"
        )
    else:
        model_sd, optim_sd = get_state_dict(model, optimizer)
        ckpt.save_sync(
            {
                "model": model_sd,
                "optim": optim_sd,
                "extra": {"step": torch.tensor(step)},
            },
            step,
        )

    dist.barrier()
    dist.destroy_process_group(control_group)
    dist.destroy_process_group()
    if stop_now:
        log(f"exiting after signal at step {step} (checkpoint committed)")
        raise SystemExit(143)
    log(f"finished at step {step}")


if __name__ == "__main__":
    main()
