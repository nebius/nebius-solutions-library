#!/usr/bin/env python3

import asyncio
import json
import logging
import os
import random
import sys
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Optional

ALLOWED_CLEANUP_NAMESPACES: tuple[str, ...] = (
    "logs-system",
    "monitoring-system",
    "soperator",
    "nfs-system",
)

NOT_FOUND_PATTERNS: tuple[str, ...] = (
    "ResourceNotFound",
    "disk not found",
)

PARENT_ID: str = ""

START_DELETE_PARALLELISM: Final[int] = 100
PAGE_SIZE: Final[int] = 999
CLI_RETRIES: Final[int] = 5
MAX_REQUEUE: Final[int] = 3
INITIAL_REQUEUE_BACKOFF_SECONDS: Final[float] = 1.0
MAX_REQUEUE_BACKOFF_SECONDS: Final[float] = 60.0
START_RATE_PER_SECOND: Final[float] = 100.0
OPERATION_WAIT_TIMEOUT: Final[str] = "5m"

logger = logging.getLogger("disk_cleanup")


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))

    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


class CleanupError(Exception):
    pass


class CommandError(CleanupError):
    def __init__(self, command: list[str], returncode: int, stdout: str, stderr: str):
        super().__init__(format_command_error(command, returncode, stdout, stderr))
        self.command: list[str] = command
        self.returncode: int = returncode
        self.stdout: str = stdout
        self.stderr: str = stderr

    @property
    def combined_output(self) -> str:
        return f"{self.stdout}\n{self.stderr}"


class TaskStatus(StrEnum):
    COMPLETE = "complete"
    REQUEUE = "requeue"


@dataclass(frozen=True)
class Disk:
    id: str
    name: str
    namespace: str


@dataclass(frozen=True)
class StartDeleteTask:
    disk: Disk
    requeues: int = 0


@dataclass(frozen=True)
class WaitDeleteTask:
    disk: Disk
    operation_id: str
    requeues: int = 0


@dataclass(frozen=True)
class TaskResult:
    status: TaskStatus
    reason: str = ""


class CleanupTracker:
    def __init__(self, disk_count: int):
        self._remaining: int = disk_count
        self._done = asyncio.Event()

        if disk_count == 0:
            self._done.set()

    def complete_disk(self) -> None:
        self._remaining -= 1

        if self._remaining < 0:
            raise CleanupError("cleanup tracker completed more disks than scheduled")

        if self._remaining == 0:
            self._done.set()

    async def wait_done(self) -> None:
        await self._done.wait()


class StartRateLimiter:
    def __init__(self, rate_per_second: float):
        self._interval: int | float = 0 if rate_per_second <= 0 else 1 / rate_per_second
        self._lock = asyncio.Lock()
        self._next_start: float = 0.0

    async def wait(self) -> None:
        if self._interval == 0:
            return

        async with self._lock:
            now: float = time.monotonic()

            if self._next_start > now:
                await asyncio.sleep(self._next_start - now)
                now = time.monotonic()

            self._next_start = max(now, self._next_start) + self._interval


def load_parent_id() -> str:
    parent_id: Optional[str] = os.environ.get("PARENT_ID")
    if not parent_id:
        raise CleanupError("PARENT_ID is required")
    return parent_id


def format_command_error(
    command: list[str], returncode: int, stdout: str, stderr: str
) -> str:
    details = [f"command failed with exit code {returncode}: {' '.join(command)}"]

    if stdout.strip():
        details.append(f"stdout: {stdout.strip()}")

    if stderr.strip():
        details.append(f"stderr: {stderr.strip()}")

    return "\n".join(details)


def is_not_found(error: CommandError) -> bool:
    return any(
        pattern.lower() in error.combined_output.lower()
        for pattern in NOT_FOUND_PATTERNS
    )


def parse_operation_id(output: str) -> str:
    stripped_output: str = output.strip()
    if not stripped_output:
        raise CleanupError("delete command returned an empty operation id")

    try:
        payload = json.loads(stripped_output)
    except json.JSONDecodeError as exc:
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        if len(lines) == 1 and is_operation_id(lines[0]):
            return lines[0]
        raise CleanupError(
            f"delete command returned unexpected output: {stripped_output}"
        ) from exc

    if not isinstance(payload, dict):
        raise CleanupError(f"delete command returned non-object JSON: {payload}")

    operation_id = payload.get("id")
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise CleanupError(f"delete operation JSON does not contain id: {payload}")

    return operation_id.strip()


def is_operation_id(value: str) -> bool:
    return value.startswith("computeoperation-")


def requeue_delay(requeues: int) -> float:
    exponential = INITIAL_REQUEUE_BACKOFF_SECONDS * (2**requeues)
    capped = min(exponential, MAX_REQUEUE_BACKOFF_SECONDS)
    return random.uniform(0, capped)


async def run_command(command: list[str]) -> str:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await process.communicate()
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")

    if process.returncode != 0:
        raise CommandError(command, process.returncode or 1, stdout, stderr)
    return stdout


def disk_from_item(item: dict) -> Optional[Disk]:
    metadata = item.get("metadata") or {}
    status = item.get("status") or {}
    labels = metadata.get("labels") or {}

    disk_id = metadata.get("id") or ""
    name = metadata.get("name") or ""
    namespace = labels.get("kubernetes.io/created-for/pvc/namespace") or ""
    attachment = status.get("read_write_attachment")

    if (
        disk_id
        and not attachment
        and namespace in ALLOWED_CLEANUP_NAMESPACES
        and name.startswith("pvc-")
    ):
        return Disk(id=disk_id, name=name, namespace=namespace)
    return None


async def list_disks() -> list[Disk]:
    page_token = ""
    result: list[Disk] = []
    page_number = 0

    while True:
        page_number += 1
        command = [
            "nebius",
            "compute",
            "disk",
            "list",
            "--parent-id",
            PARENT_ID,
            "--page-size",
            str(PAGE_SIZE),
            "--format",
            "json",
            "--no-progress",
            "--retries",
            str(CLI_RETRIES),
        ]
        if page_token:
            command.extend(["--page-token", page_token])

        output = await run_command(command)
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise CleanupError(
                f"failed to parse disk list JSON page {page_number}: {exc}"
            ) from exc

        items = payload.get("items") or []
        for item in items:
            disk = disk_from_item(item)
            if disk is not None:
                result.append(disk)

        page_token = payload.get("next_page_token") or ""
        logger.info(
            "Scanned disk page %d: %d items, %d cleanup candidates so far",
            page_number,
            len(items),
            len(result),
        )
        if not page_token:
            return result


async def disk_exists(disk: Disk) -> bool:
    command = [
        "nebius",
        "compute",
        "disk",
        "get",
        "--id",
        disk.id,
        "--format",
        "json",
        "--no-progress",
        "--retries",
        str(CLI_RETRIES),
    ]
    try:
        await run_command(command)
        return True
    except CommandError as error:
        if is_not_found(error):
            return False
        raise


async def start_delete_operation(
    disk: Disk,
    start_limiter: StartRateLimiter,
) -> Optional[str]:
    command = [
        "nebius",
        "compute",
        "disk",
        "delete",
        "--id",
        disk.id,
        "--async",
        "--format",
        "json",
        "--no-progress",
        "--retries",
        str(CLI_RETRIES),
    ]
    await start_limiter.wait()
    try:
        output = await run_command(command)
    except CommandError as error:
        if is_not_found(error):
            return None
        raise

    return parse_operation_id(output)


async def wait_delete_operation(
    operation_id: str,
) -> bool:
    command = [
        "nebius",
        "compute",
        "disk",
        "operation",
        "wait",
        "--id",
        operation_id,
        "--timeout",
        OPERATION_WAIT_TIMEOUT,
        "--no-progress",
        "--retries",
        str(CLI_RETRIES),
    ]
    try:
        await run_command(command)
        return True
    except CommandError as error:
        if is_not_found(error):
            return False
        raise


async def recheck_after_not_found(disk: Disk) -> TaskResult:
    exists = await disk_exists(disk)
    if not exists:
        logger.info("Disk %s is absent; task complete", disk.id)
        return TaskResult(TaskStatus.COMPLETE)
    return TaskResult(TaskStatus.REQUEUE, "disk still exists after not-found response")


async def run_start_delete_task(
    task: StartDeleteTask,
    start_limiter: StartRateLimiter,
) -> TaskResult | WaitDeleteTask:
    disk = task.disk

    logger.info(
        "Starting deletion of leftover disk %s (%s/%s)",
        disk.id,
        disk.namespace,
        disk.name,
    )
    operation_id = await start_delete_operation(disk, start_limiter)
    if operation_id is None:
        return await recheck_after_not_found(disk)

    logger.info("Started disk %s delete operation %s", disk.id, operation_id)
    return WaitDeleteTask(
        disk=disk,
        operation_id=operation_id,
        requeues=task.requeues,
    )


async def run_wait_delete_task(
    task: WaitDeleteTask,
) -> TaskResult:
    disk = task.disk

    logger.info("Waiting for disk %s delete operation %s", disk.id, task.operation_id)
    operation_wait_completed = await wait_delete_operation(task.operation_id)
    if not operation_wait_completed:
        return await recheck_after_not_found(disk)

    logger.info("Deleted leftover disk %s", disk.id)
    return TaskResult(TaskStatus.COMPLETE)


async def complete_or_requeue_start(
    name: str,
    disk: Disk,
    requeues: int,
    result: TaskResult,
    start_queue: asyncio.Queue[StartDeleteTask],
    failures: dict[str, str],
    tracker: CleanupTracker,
) -> None:
    if result.status == TaskStatus.COMPLETE:
        tracker.complete_disk()
        return

    if requeues >= MAX_REQUEUE:
        failures[disk.id] = result.reason
        logger.error(
            "%s: disk %s exceeded max requeue (%d): %s",
            name,
            disk.id,
            MAX_REQUEUE,
            result.reason,
        )
        tracker.complete_disk()
        return

    delay = requeue_delay(requeues)
    logger.warning(
        "%s: requeue disk %s in %.2fs (%d/%d): %s",
        name,
        disk.id,
        delay,
        requeues + 1,
        MAX_REQUEUE,
        result.reason,
    )
    await asyncio.sleep(delay)
    start_queue.put_nowait(StartDeleteTask(disk=disk, requeues=requeues + 1))


async def start_worker(
    name: str,
    start_queue: asyncio.Queue[StartDeleteTask],
    wait_queue: asyncio.Queue[WaitDeleteTask],
    start_limiter: StartRateLimiter,
    failures: dict[str, str],
    tracker: CleanupTracker,
) -> None:
    while True:
        task = await start_queue.get()
        disk = task.disk
        try:
            result = await run_start_delete_task(task, start_limiter)
        except Exception as exc:
            result = TaskResult(TaskStatus.REQUEUE, str(exc))

        if isinstance(result, WaitDeleteTask):
            wait_queue.put_nowait(result)
        else:
            await complete_or_requeue_start(
                name,
                disk,
                task.requeues,
                result,
                start_queue,
                failures,
                tracker,
            )

        start_queue.task_done()


async def wait_worker(
    name: str,
    start_queue: asyncio.Queue[StartDeleteTask],
    wait_queue: asyncio.Queue[WaitDeleteTask],
    failures: dict[str, str],
    tracker: CleanupTracker,
) -> None:
    while True:
        task = await wait_queue.get()
        try:
            result = await run_wait_delete_task(task)
        except Exception as exc:
            result = TaskResult(TaskStatus.REQUEUE, str(exc))

        await complete_or_requeue_start(
            name,
            task.disk,
            task.requeues,
            result,
            start_queue,
            failures,
            tracker,
        )
        wait_queue.task_done()


async def cleanup_disks(disks: list[Disk]) -> int:
    start_queue: asyncio.Queue[StartDeleteTask] = asyncio.Queue()
    wait_queue: asyncio.Queue[WaitDeleteTask] = asyncio.Queue()
    for disk in disks:
        start_queue.put_nowait(StartDeleteTask(disk=disk))

    failures: dict[str, str] = {}
    start_worker_count = min(START_DELETE_PARALLELISM, len(disks))
    wait_worker_count = len(disks)
    start_limiter = StartRateLimiter(START_RATE_PER_SECOND)
    tracker = CleanupTracker(len(disks))

    logger.info(
        "Disk cleanup workers: start_delete=%d, wait_delete=%d",
        start_worker_count,
        wait_worker_count,
    )

    workers = [
        asyncio.create_task(
            start_worker(
                f"start-worker-{index + 1}",
                start_queue,
                wait_queue,
                start_limiter,
                failures,
                tracker,
            )
        )
        for index in range(start_worker_count)
    ]
    workers.extend(
        asyncio.create_task(
            wait_worker(
                f"wait-worker-{index + 1}",
                start_queue,
                wait_queue,
                failures,
                tracker,
            )
        )
        for index in range(wait_worker_count)
    )

    await tracker.wait_done()
    for worker_task in workers:
        worker_task.cancel()
    await asyncio.gather(*workers, return_exceptions=True)

    deleted_count = len(disks) - len(failures)
    if deleted_count > 0:
        logger.info("Deleted %d leftover disks", deleted_count)

    if failures:
        logger.error("Failed disk deletions:")
        for disk_id, error in sorted(failures.items()):
            logger.error("- %s: %s", disk_id, error)
        return 1
    return 0


async def async_main() -> int:
    global PARENT_ID
    PARENT_ID = load_parent_id()

    logger.info(
        "Disk cleanup config: start_delete_parallelism=%d, page_size=%d, cli_retries=%d, "
        "max_requeue=%d, start_rate_per_second=%.2f",
        START_DELETE_PARALLELISM,
        PAGE_SIZE,
        CLI_RETRIES,
        MAX_REQUEUE,
        START_RATE_PER_SECOND,
    )

    disks = await list_disks()
    if not disks:
        logger.info("No leftover disks to delete")
        return 0

    logger.info("Found %d leftover disks to delete", len(disks))
    return await cleanup_disks(disks)


def main() -> int:
    configure_logging()

    try:
        return asyncio.run(async_main())
    except CleanupError as error:
        logger.error("%s", error)
        return 1
    except KeyboardInterrupt:
        logger.error("Interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
