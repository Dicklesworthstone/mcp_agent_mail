"""Filesystem and Git archive helpers for MCP Agent Mail.

Concurrency Architecture:
- Per-project archive locks (.archive.lock) serialize archive mutations
- Per-project commit locks (.commit.lock) serialize git commit operations
- Commit queue with batching reduces lock contention under high load
- Adaptive retry with exponential backoff + jitter for transient failures

Key Design Decisions:
1. File locks use SoftFileLock (cross-platform, doesn't require OS support)
2. Lock metadata (.owner.json) enables stale lock detection and cleanup
3. Process-level asyncio.Lock prevents re-entrant acquisition within same process
4. Git index.lock errors are retried with exponential backoff + stale cleanup
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator, Iterable, Sequence, TypeVar, cast

from filelock import SoftFileLock, Timeout
from git import Actor, Repo
from git.objects.tree import Tree
from PIL import Image

from .config import Settings
from .utils import validate_thread_id_format

_logger = logging.getLogger(__name__)
_IMAGE_PATTERN = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<path>[^)]+)\)")
_SUBJECT_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


# =============================================================================
# Commit Queue - Batches multiple commits to reduce lock contention
# =============================================================================

@dataclass
class _CommitRequest:
    """A pending commit request in the queue."""
    repo_root: Path
    settings: Settings
    message: str
    rel_paths: list[str]
    future: asyncio.Future[None] = field(default_factory=lambda: asyncio.get_event_loop().create_future())
    created_at: float = field(default_factory=time.monotonic)


class _CommitQueue:
    """Batches and serializes git commit operations to reduce lock contention.

    Instead of each operation acquiring a lock, performing a commit, and releasing,
    this queue batches multiple pending commits into a single git operation when
    the paths don't conflict. This dramatically reduces lock contention under
    high concurrency.

    Design:
    - Commits are queued with asyncio.Future for result notification
    - A background task processes the queue periodically or when full
    - Non-conflicting commits (different file paths) are batched together
    - Conflicting commits are processed sequentially

    Usage:
        queue = _CommitQueue()
        await queue.start()
        await queue.enqueue(repo_root, settings, message, rel_paths)
        await queue.stop()
    """

    def __init__(
        self,
        max_batch_size: int = 10,
        max_wait_ms: float = 50.0,
        max_queue_size: int = 100,
    ) -> None:
        self._queue: asyncio.Queue[_CommitRequest] = asyncio.Queue(maxsize=max_queue_size)
        self._max_batch_size = max_batch_size
        self._max_wait_ms = max_wait_ms
        self._max_queue_size = max_queue_size
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        self._lock = asyncio.Lock()
        self._enqueued = 0
        self._batched = 0
        self._commits = 0
        self._batch_sizes: list[int] = []  # Rolling window of last 100 batch sizes

    @property
    def stats(self) -> dict[str, Any]:
        """Return queue statistics for monitoring."""
        batch_sizes = self._batch_sizes
        avg_batch = sum(batch_sizes) / len(batch_sizes) if batch_sizes else 0.0
        return {
            "enqueued": self._enqueued,
            "batched": self._batched,
            "commits": self._commits,
            "avg_batch_size": round(avg_batch, 2),
            "queue_size": self._queue.qsize(),
            "running": self._task is not None and not self._task.done(),
        }

    async def start(self) -> None:
        """Start the background queue processor."""
        if self._task is not None:
            return
        self._stopped = False
        self._task = asyncio.create_task(self._process_loop())

    async def stop(self, timeout_seconds: float = 5.0) -> None:
        """Stop the queue processor, draining pending commits."""
        self._stopped = True
        if self._task is not None:
            # Signal the processor to wake up and check stopped flag
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(_CommitRequest(
                    repo_root=Path("/dev/null"),  # Sentinel
                    settings=None,  # type: ignore
                    message="",
                    rel_paths=[],
                ))
            try:
                async with asyncio.timeout(timeout_seconds):
                    await self._task
            except TimeoutError:
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
            self._task = None

    async def enqueue(
        self,
        repo_root: Path,
        settings: Settings,
        message: str,
        rel_paths: Sequence[str],
    ) -> None:
        """Enqueue a commit request and wait for completion.

        Args:
            repo_root: Path to git repository root
            settings: Application settings
            message: Commit message
            rel_paths: Relative paths to add and commit

        Raises:
            asyncio.QueueFull: If queue is at capacity
            Exception: Any exception from the actual commit operation
        """
        if not rel_paths:
            return

        request = _CommitRequest(
            repo_root=repo_root,
            settings=settings,
            message=message,
            rel_paths=list(rel_paths),
        )
        self._enqueued += 1

        # If queue processor isn't running, fall back to direct commit
        if self._task is None or self._task.done():
            await _commit_direct(repo_root, settings, message, rel_paths)
            return

        try:
            self._queue.put_nowait(request)
        except asyncio.QueueFull:
            # Queue is full - fall back to direct commit
            _logger.warning("commit_queue.full", extra={"queue_size": self._queue.qsize()})
            await _commit_direct(repo_root, settings, message, rel_paths)
            return

        # Wait for the commit to complete
        await request.future

    async def _process_loop(self) -> None:
        """Background loop that processes queued commits."""
        while not self._stopped:
            try:
                # Wait for first request with timeout
                try:
                    first = await asyncio.wait_for(
                        self._queue.get(),
                        timeout=self._max_wait_ms / 1000.0,
                    )
                except asyncio.TimeoutError:
                    continue

                # Skip sentinel requests
                if first.settings is None:
                    continue

                # Collect more requests if available (non-blocking)
                batch = [first]
                deadline = time.monotonic() + (self._max_wait_ms / 1000.0)

                while len(batch) < self._max_batch_size and time.monotonic() < deadline:
                    try:
                        request = self._queue.get_nowait()
                        if request.settings is not None:  # Skip sentinels
                            batch.append(request)
                    except asyncio.QueueEmpty:
                        # Brief wait to allow more requests to arrive
                        await asyncio.sleep(0.005)
                        break

                # Process the batch
                await self._process_batch(batch)

            except Exception as e:
                _logger.exception("commit_queue.error", extra={"error": str(e)})
                await asyncio.sleep(0.1)  # Back off on errors

    async def _process_batch(self, batch: list[_CommitRequest]) -> None:
        """Process a batch of commit requests.

        Attempts to merge non-conflicting commits into a single git operation.
        Falls back to sequential commits for conflicts.
        """
        if not batch:
            return

        self._batched += len(batch)

        # Group by repo root (commits to same repo can potentially be batched)
        by_repo: dict[str, list[_CommitRequest]] = {}
        for req in batch:
            key = str(req.repo_root)
            by_repo.setdefault(key, []).append(req)

        for _repo_path, requests in by_repo.items():
            if len(requests) == 1:
                # Single request - just commit it directly
                req = requests[0]
                try:
                    await _commit_direct(req.repo_root, req.settings, req.message, req.rel_paths)
                    req.future.set_result(None)
                    self._commits += 1
                except Exception as e:
                    req.future.set_exception(e)
            else:
                # Multiple requests to same repo - try to batch if no path conflicts
                all_paths: set[str] = set()
                can_batch = True
                for req in requests:
                    path_set = set(req.rel_paths)
                    if all_paths & path_set:  # Overlap detected
                        can_batch = False
                        break
                    all_paths.update(path_set)

                if can_batch and len(requests) <= 5:  # Only batch small groups
                    # Merge into single commit
                    merged_paths: list[str] = []
                    merged_messages: list[str] = []
                    for req in requests:
                        merged_paths.extend(req.rel_paths)
                        merged_messages.append(req.message.split("\n")[0])  # First line only

                    combined_message = f"batch: {len(requests)} commits\n\n" + "\n".join(
                        f"- {msg}" for msg in merged_messages
                    )

                    try:
                        await _commit_direct(
                            requests[0].repo_root,
                            requests[0].settings,
                            combined_message,
                            merged_paths,
                        )
                        for req in requests:
                            req.future.set_result(None)
                        self._commits += 1
                        # Record batch size
                        self._batch_sizes.append(len(requests))
                        if len(self._batch_sizes) > 100:
                            self._batch_sizes.pop(0)
                    except Exception as e:
                        for req in requests:
                            req.future.set_exception(e)
                else:
                    # Process sequentially (conflicts or large batch)
                    for req in requests:
                        try:
                            await _commit_direct(req.repo_root, req.settings, req.message, req.rel_paths)
                            req.future.set_result(None)
                            self._commits += 1
                        except Exception as e:
                            req.future.set_exception(e)


# Global commit queue instance (lazily initialized)
_COMMIT_QUEUE: _CommitQueue | None = None
_COMMIT_QUEUE_LOCK: asyncio.Lock | None = None


def _get_commit_queue_lock() -> asyncio.Lock:
    """Get or create commit queue lock."""
    global _COMMIT_QUEUE_LOCK
    if _COMMIT_QUEUE_LOCK is None:
        _COMMIT_QUEUE_LOCK = asyncio.Lock()
    return _COMMIT_QUEUE_LOCK


async def _get_commit_queue() -> _CommitQueue:
    """Get or create the global commit queue."""
    global _COMMIT_QUEUE
    if _COMMIT_QUEUE is not None:
        return _COMMIT_QUEUE
    async with _get_commit_queue_lock():
        if _COMMIT_QUEUE is None:
            _COMMIT_QUEUE = _CommitQueue()
            await _COMMIT_QUEUE.start()
        return _COMMIT_QUEUE


def get_commit_queue_stats() -> dict[str, Any]:
    """Get commit queue statistics for monitoring."""
    if _COMMIT_QUEUE is None:
        return {"running": False, "initialized": False}
    return _COMMIT_QUEUE.stats


@dataclass(slots=True)
class ProjectArchive:
    settings: Settings
    slug: str
    # Project-specific root inside the single global archive repo
    root: Path
    # The single Git repo object rooted at settings.storage.root
    repo: Repo
    # Path used for advisory file lock during archive writes
    lock_path: Path
    # Filesystem path to the Git repo working directory (archive root)
    repo_root: Path

    @property
    def attachments_dir(self) -> Path:
        return self.root / "attachments"

_PROCESS_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}
_PROCESS_LOCK_OWNERS: dict[tuple[int, str], int] = {}


class _LRURepoCache:
    """LRU cache for Git Repo objects with size limit.

    This prevents file descriptor leaks by:
    1. Limiting the number of cached repos (default: 16)
    2. Evicting oldest repos when at capacity
    3. Aggressively cleaning up evicted repos when reference counts drop

    IMPORTANT: Evicted repos are NOT closed immediately because they may still be in use
    by other coroutines. They will be closed when garbage collected or when clear() is called.
    Cleanup runs opportunistically on both get() and put() operations.
    """

    def __init__(self, maxsize: int = 16) -> None:
        self._maxsize = max(1, maxsize)
        self._cache: dict[str, Repo] = {}
        self._order: list[str] = []  # LRU order: oldest first
        self._evicted: list[Repo] = []  # Evicted repos pending close
        self._cleanup_counter: int = 0  # Track operations for periodic cleanup

    def peek(self, key: str) -> Repo | None:
        """Check if key exists and return value WITHOUT updating LRU order.

        Safe to call without holding the external lock for a fast-path check.
        """
        return self._cache.get(key)

    def get(self, key: str) -> Repo | None:
        """Get a repo from cache, updating LRU order.

        Should only be called while holding the external lock.
        Also performs opportunistic cleanup of evicted repos.
        """
        if key in self._cache:
            # Move to end (most recently used)
            with contextlib.suppress(ValueError):
                self._order.remove(key)
            self._order.append(key)
            # Opportunistically try to clean up evicted repos every 4th access
            self._cleanup_counter += 1
            if self._cleanup_counter >= 4:
                self._cleanup_counter = 0
                self._cleanup_evicted()
            return self._cache[key]
        return None

    def put(self, key: str, repo: Repo) -> None:
        """Add a repo to cache, evicting oldest if at capacity.

        Should only be called while holding the external lock.
        Evicted repos are added to a pending list for later cleanup.

        Note: If the key already exists, only the LRU order is updated;
        the cached repo value is NOT replaced. This is intentional since
        the cache is only used by _ensure_repo which checks existence first.
        """
        if key in self._cache:
            # Already exists, just update LRU order
            with contextlib.suppress(ValueError):
                self._order.remove(key)
            self._order.append(key)
            return

        # Evict oldest entries if at capacity
        while len(self._cache) >= self._maxsize and self._order:
            oldest_key = self._order.pop(0)
            old_repo = self._cache.pop(oldest_key, None)
            if old_repo is not None:
                # Don't close immediately - repo may still be in use by another coroutine
                # Add to evicted list for later cleanup
                self._evicted.append(old_repo)

        self._cache[key] = repo
        self._order.append(key)

        # Opportunistically try to close evicted repos that are no longer referenced
        self._cleanup_evicted()

    def _cleanup_evicted(self) -> int:
        """Try to close evicted repos that have only one reference (ours).

        Returns count of repos closed. Logs warning if evicted list grows large.
        """
        still_in_use: list[Repo] = []
        closed = 0
        for repo in self._evicted:
            # If refcount is 2 (our list + the getrefcount call), it's safe to close
            # In practice, we use 3 as threshold to be safe (list + local var + getrefcount)
            if sys.getrefcount(repo) <= 3:
                with contextlib.suppress(Exception):
                    repo.close()
                    closed += 1
            else:
                still_in_use.append(repo)
        self._evicted = still_in_use
        # Warn if evicted list is growing large (potential file handle pressure)
        if len(still_in_use) > self._maxsize:
            import logging
            logging.getLogger(__name__).warning(
                "repo_cache.evicted_backlog",
                extra={"evicted_count": len(still_in_use), "maxsize": self._maxsize},
            )
        return closed

    @property
    def evicted_count(self) -> int:
        """Number of evicted repos waiting to be closed."""
        return len(self._evicted)

    @property
    def stats(self) -> dict[str, int]:
        """Return cache statistics for monitoring."""
        return {
            "cached": len(self._cache),
            "evicted": len(self._evicted),
            "maxsize": self._maxsize,
        }

    def __contains__(self, key: str) -> bool:
        return key in self._cache

    def __len__(self) -> int:
        return len(self._cache)

    def clear(self) -> int:
        """Close all cached and evicted repos and clear the cache. Returns count closed."""
        count = 0
        # Close cached repos
        for repo in self._cache.values():
            with contextlib.suppress(Exception):
                repo.close()
                count += 1
        self._cache.clear()
        self._order.clear()
        # Also close any evicted repos still in pending list
        for repo in self._evicted:
            with contextlib.suppress(Exception):
                repo.close()
                count += 1
        self._evicted.clear()
        return count

    def values(self) -> list[Repo]:
        """Return list of cached repos (for iteration)."""
        return list(self._cache.values())


# LRU cache for Repo objects with automatic cleanup
# Limits to 16 concurrent repos to prevent file handle exhaustion under heavy load
# Increased from 8 to handle multi-project scenarios better (GitHub issue #59)
_REPO_CACHE: _LRURepoCache = _LRURepoCache()  # Uses default maxsize=16
_REPO_CACHE_LOCK: asyncio.Lock | None = None

# Semaphore to limit concurrent repo operations (prevents FD exhaustion under high concurrency)
# This acts as a second line of defense beyond the LRU cache
_REPO_SEMAPHORE: asyncio.Semaphore | None = None
_REPO_SEMAPHORE_LIMIT: int = 32  # Max concurrent repo operations


def _get_repo_cache_lock() -> asyncio.Lock:
    """Get or create the repo cache lock (must be called from async context)."""
    global _REPO_CACHE_LOCK
    if _REPO_CACHE_LOCK is None:
        _REPO_CACHE_LOCK = asyncio.Lock()
    return _REPO_CACHE_LOCK


def _get_repo_semaphore() -> asyncio.Semaphore:
    """Get or create the repo semaphore (must be called from async context)."""
    global _REPO_SEMAPHORE
    if _REPO_SEMAPHORE is None:
        _REPO_SEMAPHORE = asyncio.Semaphore(_REPO_SEMAPHORE_LIMIT)
    return _REPO_SEMAPHORE


def get_fd_usage() -> tuple[int, int]:
    """Get current and maximum file descriptor counts.

    Returns (current_count, max_limit) tuple.
    On platforms where this isn't available, returns (-1, -1).
    """
    try:
        import resource
        soft_limit, _hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
        # Count open file descriptors by checking /proc/self/fd (Linux) or /dev/fd (macOS)
        fd_dir = Path("/dev/fd") if sys.platform == "darwin" else Path("/proc/self/fd")
        if fd_dir.exists():
            current = len(list(fd_dir.iterdir()))
            return (current, soft_limit)
        return (-1, soft_limit)
    except (ImportError, OSError, AttributeError):
        return (-1, -1)


def get_fd_headroom() -> int:
    """Get remaining file descriptor headroom before hitting the limit.

    Returns -1 if unable to determine.
    """
    current, limit = get_fd_usage()
    if current < 0 or limit < 0:
        return -1
    return max(0, limit - current)


def proactive_fd_cleanup(*, threshold: int = 100) -> int:
    """Proactively cleanup resources if file descriptor headroom is low.

    Args:
        threshold: Minimum headroom required; cleanup runs if below this.

    Returns:
        Number of repos freed (0 if no cleanup needed or unable to check).
    """
    headroom = get_fd_headroom()
    if headroom < 0:
        # Can't determine headroom, skip cleanup
        return 0
    if headroom >= threshold:
        # Sufficient headroom, no cleanup needed
        return 0
    # Low headroom - force cleanup of evicted repos
    freed = _REPO_CACHE._cleanup_evicted()
    # If still low, clear some cached repos too
    new_headroom = get_fd_headroom()
    if new_headroom >= 0 and new_headroom < threshold // 2:
        # Critical: clear all cached repos
        freed += _REPO_CACHE.clear()
    return freed


def clear_repo_cache() -> int:
    """Close all cached Repo objects and clear the cache.

    Returns the number of repos that were closed.
    Should be called during shutdown or between tests.
    """
    return _REPO_CACHE.clear()


def get_repo_cache_stats() -> dict[str, int]:
    """Get repository cache statistics for monitoring.

    Returns dict with 'cached', 'evicted', and 'maxsize' counts.
    Useful for diagnosing file handle pressure.
    """
    return _REPO_CACHE.stats


class AsyncFileLock:
    """Async-friendly wrapper around SoftFileLock with metadata tracking and adaptive retries.

    Features:
    - Metadata tracking (.owner.json) enables stale lock detection
    - Process-level asyncio.Lock prevents re-entrant acquisition
    - Adaptive retry with exponential backoff on acquisition failure
    - Stale lock cleanup when owner process is dead or lock is too old

    Adaptive Timeout Strategy:
    - Initial attempt uses short timeout (10% of total)
    - Failed attempts trigger stale lock cleanup check
    - Subsequent attempts use progressively longer timeouts
    - This allows fast acquisition when lock is free while still handling
      edge cases like stale locks or slow I/O
    """

    def __init__(
        self,
        path: Path,
        *,
        timeout_seconds: float = 60.0,
        stale_timeout_seconds: float = 180.0,
        max_retries: int = 5,
    ) -> None:
        self._path = Path(path)
        self._lock = SoftFileLock(str(self._path))
        self._timeout = float(timeout_seconds)
        self._stale_timeout = float(max(stale_timeout_seconds, 0.0))
        self._max_retries = max_retries
        self._pid = os.getpid()
        self._metadata_path = self._path.parent / f"{self._path.name}.owner.json"
        self._held = False
        self._lock_key = str(self._path.resolve())
        self._acquisition_start: float | None = None
        self._acquisition_attempts: int = 0
        self._loop_key: tuple[int, str] | None = None
        self._process_lock: asyncio.Lock | None = None
        self._process_lock_held = False

    async def __aenter__(self) -> None:
        """Acquire the file lock with adaptive retry and stale lock detection.

        Adaptive Retry Strategy:
        1. First attempt: Short timeout (10% of total) - fast path for uncontested locks
        2. On timeout: Check for stale locks and clean up if found
        3. Subsequent attempts: Exponential backoff with longer per-attempt timeouts
        4. Final attempt: Full remaining timeout

        This strategy optimizes for:
        - Fast acquisition when lock is free (common case)
        - Graceful handling of stale locks from crashed processes
        - Avoiding thundering herd with jittered backoff
        """
        self._acquisition_start = time.monotonic()
        self._acquisition_attempts = 0

        loop = asyncio.get_running_loop()
        self._loop_key = (id(loop), self._lock_key)
        process_lock = _PROCESS_LOCKS.get(self._loop_key)
        if process_lock is None:
            process_lock = asyncio.Lock()
            _PROCESS_LOCKS[self._loop_key] = process_lock
        current_task = asyncio.current_task()
        owner_id = _PROCESS_LOCK_OWNERS.get(self._loop_key)
        current_task_id = id(current_task) if current_task else id(self)
        if owner_id == current_task_id:
            raise RuntimeError(f"Re-entrant AsyncFileLock acquisition detected for {self._path}")
        self._process_lock = process_lock
        await self._process_lock.acquire()
        self._process_lock_held = True
        _PROCESS_LOCK_OWNERS[self._loop_key] = current_task_id
        try:
            total_timeout = self._timeout if self._timeout > 0 else 60.0
            remaining = total_timeout

            for attempt in range(self._max_retries + 1):
                self._acquisition_attempts = attempt + 1

                # Adaptive timeout per attempt:
                # - First attempt: 10% of total (fast path)
                # - Middle attempts: progressively longer
                # - Last attempt: all remaining time
                if attempt == 0:
                    per_attempt_timeout = min(total_timeout * 0.1, 5.0)  # 10%, max 5s
                elif attempt == self._max_retries:
                    per_attempt_timeout = remaining  # Use all remaining
                else:
                    # Exponential growth: 0.5s, 1s, 2s, 4s, ...
                    per_attempt_timeout = min(0.5 * (2 ** attempt), remaining)

                try:
                    if self._timeout <= 0:
                        await _to_thread(self._lock.acquire)
                    else:
                        await _to_thread(self._lock.acquire, per_attempt_timeout)
                    self._held = True
                    await _to_thread(self._write_metadata)

                    # Log successful acquisition if it took retries
                    if attempt > 0:
                        elapsed = time.monotonic() - self._acquisition_start
                        _logger.info(
                            "file_lock.acquired_after_retry",
                            extra={
                                "path": str(self._path),
                                "attempts": attempt + 1,
                                "elapsed_seconds": round(elapsed, 2),
                            },
                        )
                    return None

                except Timeout:
                    elapsed = time.monotonic() - self._acquisition_start
                    remaining = total_timeout - elapsed

                    if remaining <= 0 or attempt >= self._max_retries:
                        # Final attempt failed - try one last stale cleanup
                        cleaned = await _to_thread(self._cleanup_if_stale)
                        if cleaned:
                            # Stale lock was cleaned - try once more with short timeout
                            try:
                                await _to_thread(self._lock.acquire, 1.0)
                                self._held = True
                                await _to_thread(self._write_metadata)
                                _logger.info(
                                    "file_lock.acquired_after_stale_cleanup",
                                    extra={"path": str(self._path)},
                                )
                                return None
                            except Timeout:
                                pass  # Fall through to timeout error
                        raise TimeoutError(
                            f"Timed out acquiring lock {self._path} after {elapsed:.2f}s "
                            f"({attempt + 1} attempts). No stale owner detected."
                        ) from None

                    # Check for stale lock before retrying
                    cleaned = await _to_thread(self._cleanup_if_stale)
                    if cleaned:
                        _logger.info(
                            "file_lock.stale_cleaned",
                            extra={"path": str(self._path), "attempt": attempt + 1},
                        )
                        # Don't add backoff delay - immediately retry after cleanup
                        continue

                    # Add jittered backoff before retry (0.05s to 0.5s)
                    backoff = min(0.05 * (2 ** attempt), 0.5)
                    jitter = backoff * 0.25 * (2 * random.random() - 1)
                    await asyncio.sleep(backoff + jitter)

        except BaseException:
            # Best-effort cleanup on any failure (including cancellation) to avoid leaking
            # lock file handles and process-level locks.
            if self._held:
                task = asyncio.create_task(_to_thread(self._lock.release))
                try:
                    await asyncio.shield(task)
                except BaseException:
                    with contextlib.suppress(Exception):
                        await task
                self._held = False
                for cleanup_coro in (
                    _to_thread(self._metadata_path.unlink, missing_ok=True),
                    _to_thread(self._path.unlink, missing_ok=True),
                ):
                    task = asyncio.create_task(cleanup_coro)
                    try:
                        await asyncio.shield(task)
                    except BaseException:
                        with contextlib.suppress(Exception):
                            await task

            if self._loop_key is not None:
                _PROCESS_LOCK_OWNERS.pop(self._loop_key, None)
            if self._process_lock_held and self._process_lock:
                self._process_lock.release()
                self._process_lock_held = False
            if (
                self._loop_key is not None
                and self._process_lock
                and not self._process_lock.locked()
            ):
                _PROCESS_LOCKS.pop(self._loop_key, None)
            self._process_lock = None
            raise

    def _cleanup_if_stale(self) -> bool:
        """Remove lock and metadata when the lock is stale.

        A lock is considered stale if EITHER:
        1. The owning process no longer exists, OR
        2. The lock age exceeds the stale timeout

        This ensures locks are cleaned up promptly when either condition is met.
        """
        if not self._path.exists():
            return False
        now = time.time()
        metadata: dict[str, Any] = {}
        if self._metadata_path.exists():
            try:
                metadata = json.loads(self._metadata_path.read_text(encoding="utf-8"))
            except Exception:
                metadata = {}
        pid_val = metadata.get("pid")
        pid_int: int | None = None
        if pid_val is not None:
            with contextlib.suppress(Exception):
                pid_int = int(pid_val)
        owner_alive = self._pid_alive(pid_int) if pid_int else False
        created_ts = metadata.get("created_ts")
        age = None
        if isinstance(created_ts, (int, float)):
            age = now - float(created_ts)
        else:
            with contextlib.suppress(Exception):
                age = now - self._path.stat().st_mtime

        # Lock is stale if EITHER the owner is dead OR the age exceeds timeout
        # Special case: if stale_timeout is 0, only check owner liveness (ignore age)
        is_stale = False
        if not owner_alive:
            # Owner process is dead - lock is stale regardless of age
            is_stale = True
        elif self._stale_timeout > 0 and isinstance(age, (int, float)) and age >= self._stale_timeout:
            # Lock is too old - stale regardless of owner status
            # (only if stale_timeout > 0, otherwise age check is disabled)
            is_stale = True

        if not is_stale:
            return False

        # Clean up stale lock
        with contextlib.suppress(Exception):
            self._path.unlink(missing_ok=True)
        with contextlib.suppress(Exception):
            self._metadata_path.unlink(missing_ok=True)
        return True

    def _write_metadata(self) -> None:
        payload = {
            "pid": self._pid,
            "created_ts": time.time(),
        }
        self._metadata_path.write_text(json.dumps(payload), encoding="utf-8")
        return None

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object) -> None:
        if self._held:
            # Release/unlink must be cancellation-safe; otherwise cancelled tasks can leak
            # lock file handles and wedge subsequent operations.
            for cleanup_coro in (
                _to_thread(self._lock.release),
                asyncio.sleep(0.01) if sys.platform == "win32" else None,
                _to_thread(self._metadata_path.unlink, missing_ok=True),
                _to_thread(self._path.unlink, missing_ok=True),
            ):
                if cleanup_coro is None:
                    continue
                task = asyncio.create_task(cleanup_coro)
                try:
                    await asyncio.shield(task)
                except BaseException:
                    with contextlib.suppress(Exception):
                        await task
            self._held = False

        # Clean up process-level locks
        if self._loop_key is not None:
            _PROCESS_LOCK_OWNERS.pop(self._loop_key, None)
        if self._process_lock_held and self._process_lock:
            self._process_lock.release()
            self._process_lock_held = False
        if (
            self._loop_key is not None
            and self._process_lock
            and not self._process_lock.locked()
        ):
            _PROCESS_LOCKS.pop(self._loop_key, None)
        self._process_lock = None
        self._loop_key = None
        return None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """Check if a process with the given PID is alive (cross-platform)."""
        if pid <= 0:
            return False

        # Try psutil first if available (most reliable cross-platform method)
        try:
            import psutil
            return bool(psutil.pid_exists(pid))
        except ImportError:
            pass

        # Platform-specific fallbacks
        if sys.platform == 'win32':
            # Windows: Use ctypes to call OpenProcess
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                SYNCHRONIZE = 0x00100000
                # Try to open the process with minimal permissions
                handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
                if handle:
                    kernel32.CloseHandle(handle)
                    return True
                return False
            except Exception:
                # If ctypes fails, assume process doesn't exist
                return False
        else:
            # Unix/Linux/macOS: Use os.kill(pid, 0)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                # Process exists but we don't have permission to signal it
                return True
            except OSError:
                return False
            return True


@asynccontextmanager
async def archive_write_lock(archive: ProjectArchive, *, timeout_seconds: float = 60.0) -> AsyncIterator[None]:
    """Context manager for safely mutating archive surfaces."""
    lock = AsyncFileLock(archive.lock_path, timeout_seconds=timeout_seconds)
    await lock.__aenter__()
    exc_type: type[BaseException] | None = None
    exc: BaseException | None = None
    tb: object | None = None
    try:
        yield
    except BaseException as raised:
        exc_type = type(raised)
        exc = raised
        tb = raised.__traceback__
        raise
    finally:
        # Ensure lock release even under task cancellation (Python 3.14: CancelledError is BaseException).
        task = asyncio.create_task(lock.__aexit__(exc_type, exc, tb))
        try:
            await asyncio.shield(task)
        except BaseException:
            with contextlib.suppress(Exception):
                await task


T = TypeVar('T')

async def _to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(func, *args, **kwargs)


def _ensure_str(value: str | bytes) -> str:
    """Ensure a value is a string, decoding bytes if necessary."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def collect_lock_status(settings: Settings) -> dict[str, Any]:
    """Return structured metadata about active archive locks."""

    root = Path(settings.storage.root).expanduser().resolve()
    locks: list[dict[str, Any]] = []
    summary = {"total": 0, "active": 0, "stale": 0, "metadata_missing": 0}

    if root.exists():
        now = time.time()
        for lock_path in sorted(root.rglob("*.lock"), key=lambda p: str(p)):
            metadata_path = lock_path.parent / f"{lock_path.name}.owner.json"
            if not lock_path.exists():
                continue
            metadata_present = metadata_path.exists()
            if lock_path.name != ".archive.lock" and not metadata_present:
                continue

            info: dict[str, Any] = {
                "path": str(lock_path),
                "metadata_path": str(metadata_path) if metadata_present else None,
                "status": "held",
                "metadata_present": metadata_present,
                "category": "archive" if lock_path.name == ".archive.lock" else "custom",
            }

            with contextlib.suppress(Exception):
                stat = lock_path.stat()
                info["size"] = stat.st_size
                info["modified_ts"] = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()

            metadata: dict[str, Any] = {}
            if metadata_present:
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                except Exception:
                    metadata = {}
            info["metadata"] = metadata

            pid_val = metadata.get("pid")
            pid_int: int | None = None
            if pid_val is not None:
                with contextlib.suppress(Exception):
                    pid_int = int(pid_val)
            info["owner_pid"] = pid_int
            info["owner_alive"] = AsyncFileLock._pid_alive(pid_int) if pid_int else False

            created_ts = metadata.get("created_ts") if isinstance(metadata, dict) else None
            if isinstance(created_ts, (int, float)):
                info["created_ts"] = datetime.fromtimestamp(created_ts, tz=timezone.utc).isoformat()
                info["age_seconds"] = max(0.0, now - float(created_ts))
            else:
                info["created_ts"] = None
                info["age_seconds"] = None

            stale_threshold = AsyncFileLock(lock_path)._stale_timeout
            info["stale_timeout_seconds"] = stale_threshold
            age_val = info.get("age_seconds")
            # Lock is stale if EITHER the owner is dead OR the age exceeds timeout
            # Special case: if stale_timeout is 0, only check owner liveness (ignore age)
            is_stale = False
            if bool(metadata):
                if not info["owner_alive"]:
                    # Owner process is dead - lock is stale
                    is_stale = True
                elif stale_threshold > 0 and isinstance(age_val, (int, float)) and age_val >= stale_threshold:
                    # Lock is too old - stale
                    # (only if stale_timeout > 0, otherwise age check is disabled)
                    is_stale = True
            info["stale_suspected"] = is_stale

            summary["total"] += 1

            if is_stale:
                summary["stale"] += 1
            elif info["owner_alive"]:
                summary["active"] += 1
            if not metadata_present:
                summary["metadata_missing"] += 1

            locks.append(info)

    return {"locks": locks, "summary": summary}


async def ensure_archive_root(settings: Settings) -> tuple[Path, Repo]:
    repo_root = Path(settings.storage.root).expanduser().resolve()
    await _to_thread(repo_root.mkdir, parents=True, exist_ok=True)
    repo = await _ensure_repo(repo_root, settings)
    return repo_root, repo


async def ensure_archive(settings: Settings, slug: str) -> ProjectArchive:
    repo_root, repo = await ensure_archive_root(settings)
    project_root = repo_root / "projects" / slug
    await _to_thread(project_root.mkdir, parents=True, exist_ok=True)
    return ProjectArchive(
        settings=settings,
        slug=slug,
        root=project_root,
        repo=repo,
        # Use a per-project advisory lock to avoid cross-project contention
        lock_path=project_root / ".archive.lock",
        repo_root=repo_root,
    )


async def _ensure_repo(root: Path, settings: Settings) -> Repo:
    """Get or create a Repo for the given root, with caching to prevent file handle leaks.

    This function implements multiple layers of protection against file descriptor exhaustion:
    1. LRU cache limits total cached repos
    2. Semaphore limits concurrent repo operations
    3. Proactive cleanup runs before creating new repos when FD headroom is low
    """
    cache_key = str(root.resolve())

    # Fast path: check cache without lock using peek() which doesn't modify LRU order
    cached = _REPO_CACHE.peek(cache_key)
    if cached is not None:
        return cached

    # Acquire semaphore to limit concurrent repo operations
    semaphore = _get_repo_semaphore()
    async with semaphore:
        # Slow path: acquire lock and check/create
        async with _get_repo_cache_lock():
            # Double-check after acquiring lock, use get() to update LRU order
            cached = _REPO_CACHE.get(cache_key)
            if cached is not None:
                return cached

            # Proactive cleanup: ensure we have headroom before creating a new repo
            # This prevents hitting EMFILE by cleaning up before it's too late
            proactive_fd_cleanup(threshold=100)

            git_dir = root / ".git"
            if git_dir.exists():
                repo = Repo(str(root))
                _REPO_CACHE.put(cache_key, repo)
                return repo

            # Initialize new repo and put in cache while holding the lock.
            # Keep the returned Repo instance to avoid leaking an extra Repo handle.
            repo = await _to_thread(Repo.init, str(root))
            _REPO_CACHE.put(cache_key, repo)
            # Flag that this is a newly created repo needing initialization
            needs_init = True

        # Configure the repo outside the lock (idempotent operations)
        if needs_init:
            try:
                def _configure_repo() -> None:
                    with repo.config_writer() as cw:
                        cw.set_value("commit", "gpgsign", "false")
                await _to_thread(_configure_repo)
            except Exception:
                pass
            attributes_path = root / ".gitattributes"
            if not attributes_path.exists():
                await _write_text(attributes_path, "*.json text\n*.md text\n")
            await _commit(repo, settings, "chore: initialize archive", [".gitattributes"])
        return repo


async def write_agent_profile(archive: ProjectArchive, agent: dict[str, object]) -> None:
    profile_path = archive.root / "agents" / agent["name"].__str__() / "profile.json"
    await _write_json(profile_path, agent)
    rel = profile_path.relative_to(archive.repo_root).as_posix()
    await _commit(archive.repo, archive.settings, f"agent: profile {agent['name']}", [rel])


def _build_file_reservation_commit_message(entries: Sequence[tuple[str, str]]) -> str:
    first_agent, first_pattern = entries[0]
    if len(entries) == 1:
        return f"file_reservation: {first_agent} {first_pattern}"
    subject = f"file_reservation: {first_agent} {first_pattern} (+{len(entries) - 1} more)"
    lines = [f"- {agent} {pattern}" for agent, pattern in entries]
    return subject + "\n\n" + "\n".join(lines)


async def write_file_reservation_records(
    archive: ProjectArchive,
    file_reservations: Sequence[dict[str, object]],
) -> None:
    if not file_reservations:
        return
    rel_paths: list[str] = []
    entries: list[tuple[str, str]] = []
    for file_reservation in file_reservations:
        path_pattern = str(file_reservation.get("path_pattern") or file_reservation.get("path") or "").strip()
        if not path_pattern:
            raise ValueError("File reservation record must include 'path_pattern'.")
        normalized_file_reservation = dict(file_reservation)
        normalized_file_reservation["path_pattern"] = path_pattern
        normalized_file_reservation.pop("path", None)
        digest = hashlib.sha1(path_pattern.encode("utf-8")).hexdigest()
        # Legacy path: digest of path_pattern (kept to avoid stale artifacts in existing installs)
        legacy_path = archive.root / "file_reservations" / f"{digest}.json"
        await _write_json(legacy_path, normalized_file_reservation)
        rel_paths.append(legacy_path.relative_to(archive.repo_root).as_posix())

        # Stable per-reservation artifact to avoid collisions across shared reservations
        reservation_id = normalized_file_reservation.get("id")
        id_token = str(reservation_id).strip() if reservation_id is not None else ""
        if id_token.isdigit():
            id_path = archive.root / "file_reservations" / f"id-{id_token}.json"
            await _write_json(id_path, normalized_file_reservation)
            rel_paths.append(id_path.relative_to(archive.repo_root).as_posix())
        agent_name = str(normalized_file_reservation.get("agent", "unknown"))
        entries.append((agent_name, path_pattern))
    commit_message = _build_file_reservation_commit_message(entries)
    await _commit(archive.repo, archive.settings, commit_message, rel_paths)


async def write_file_reservation_record(archive: ProjectArchive, file_reservation: dict[str, object]) -> None:
    await write_file_reservation_records(archive, [file_reservation])


async def write_message_bundle(
    archive: ProjectArchive,
    message: dict[str, object],
    body_md: str,
    sender: str,
    recipients: Sequence[str],
    extra_paths: Sequence[str] | None = None,
    commit_text: str | None = None,
) -> None:
    timestamp_obj: Any = message.get("created") or message.get("created_ts")
    now: datetime
    timestamp_str: str  # Always define to avoid UnboundLocalError
    if isinstance(timestamp_obj, datetime):
        now = timestamp_obj
        timestamp_str = now.isoformat()
    elif isinstance(timestamp_obj, str) and timestamp_obj.strip():
        timestamp_str = timestamp_obj.strip()
        # Handle Z-suffixed timestamps (ISO 8601 UTC indicator)
        parse_str = timestamp_str
        if parse_str.endswith("Z"):
            parse_str = parse_str[:-1] + "+00:00"
        try:
            now = datetime.fromisoformat(parse_str)
        except ValueError:
            now = datetime.now(timezone.utc)
            timestamp_str = now.isoformat()
    else:
        now = datetime.now(timezone.utc)
        timestamp_str = now.isoformat()

    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        # Treat naive timestamps as UTC (matches SQLite naive-UTC convention)
        now = now.replace(tzinfo=timezone.utc)
    y_dir = now.strftime("%Y")
    m_dir = now.strftime("%m")

    canonical_dir = archive.root / "messages" / y_dir / m_dir
    outbox_dir = archive.root / "agents" / sender / "outbox" / y_dir / m_dir
    inbox_dirs = [archive.root / "agents" / r / "inbox" / y_dir / m_dir for r in recipients]

    rel_paths: list[str] = []

    await _to_thread(canonical_dir.mkdir, parents=True, exist_ok=True)
    await _to_thread(outbox_dir.mkdir, parents=True, exist_ok=True)
    for path in inbox_dirs:
        await _to_thread(path.mkdir, parents=True, exist_ok=True)

    frontmatter = json.dumps(message, indent=2, sort_keys=True)
    content = f"---json\n{frontmatter}\n---\n\n{body_md.strip()}\n"

    # Descriptive, ISO-prefixed filename: <ISO>__<subject-slug>__<id>.md
    created_iso = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    subject_value = str(message.get("subject", "")).strip() or "message"
    subject_slug = _SUBJECT_SLUG_RE.sub("-", subject_value).strip("-_").lower()[:80] or "message"
    id_suffix = str(message.get("id", ""))
    filename = (
        f"{created_iso}__{subject_slug}__{id_suffix}.md"
        if id_suffix
        else f"{created_iso}__{subject_slug}.md"
    )
    canonical_path = canonical_dir / filename
    await _write_text(canonical_path, content)
    rel_paths.append(canonical_path.relative_to(archive.repo_root).as_posix())

    outbox_path = outbox_dir / filename
    await _write_text(outbox_path, content)
    rel_paths.append(outbox_path.relative_to(archive.repo_root).as_posix())

    for inbox_dir in inbox_dirs:
        inbox_path = inbox_dir / filename
        await _write_text(inbox_path, content)
        rel_paths.append(inbox_path.relative_to(archive.repo_root).as_posix())

    # Update thread-level digest for human review if thread_id present
    thread_id_obj = message.get("thread_id")
    if isinstance(thread_id_obj, str) and thread_id_obj.strip():
        canonical_rel = canonical_path.relative_to(archive.repo_root).as_posix()
        digest_rel = await _update_thread_digest(
            archive,
            thread_id_obj.strip(),
            {
                "from": sender,
                "to": list(recipients),
                "subject": message.get("subject", "") or "",
                "created": timestamp_str,
            },
            body_md,
            canonical_rel,
        )
        if digest_rel:
            rel_paths.append(digest_rel)

    if extra_paths:
        rel_paths.extend(extra_paths)
    thread_key = message.get("thread_id") or message.get("id")
    if commit_text:
        commit_message = commit_text if commit_text.endswith("\n") else f"{commit_text}\n"
    else:
        commit_subject = f"mail: {sender} -> {', '.join(recipients)} | {message.get('subject', '')}"
        # Enriched commit body mirroring console logs
        commit_body_lines = [
            "TOOL: send_message",
            f"Agent: {sender}",
            f"Project: {message.get('project', '')}",
            f"Started: {timestamp_str}",
            "Status: SUCCESS",
            f"Thread: {thread_key}",
        ]
        commit_message = commit_subject + "\n\n" + "\n".join(commit_body_lines) + "\n"
    await _commit(archive.repo, archive.settings, commit_message, rel_paths)


async def _update_thread_digest(
    archive: ProjectArchive,
    thread_id: str,
    meta: dict[str, object],
    body_md: str,
    canonical_rel_path: str,
) -> str | None:
    """
    Append a compact entry to a thread-level digest file for human review.

    The digest lives at messages/threads/{thread_id}.md and contains an
    append-only sequence of sections linking to canonical messages.
    """
    if not validate_thread_id_format(thread_id):
        raise ValueError(
            "Invalid thread_id: must start with an alphanumeric character and contain only "
            "letters, numbers, '.', '_', or '-' (max 128)."
        )
    digest_dir = archive.root / "messages" / "threads"
    await _to_thread(digest_dir.mkdir, parents=True, exist_ok=True)
    digest_path = digest_dir / f"{thread_id}.md"

    # Ensure recipients list is typed as list[str] for join()
    to_value = meta.get("to")
    if isinstance(to_value, (list, tuple)):
        recipients_list: list[str] = [str(v) for v in to_value]
    elif isinstance(to_value, str):
        recipients_list = [to_value]
    else:
        recipients_list = []
    header = (
        f"## {meta.get('created', '')} — {meta.get('from', '')} → {', '.join(recipients_list)}\n\n"
    )
    link_line = f"[View canonical]({canonical_rel_path})\n\n"
    subject = str(meta.get("subject", "")).strip()
    subject_line = f"### {subject}\n\n" if subject else ""

    # Truncate body to a preview to keep digest readable
    preview = body_md.strip()
    if len(preview) > 1200:
        preview = preview[:1200].rstrip() + "\n..."

    entry = subject_line + header + link_line + preview + "\n\n---\n\n"

    # Append atomically
    def _append() -> None:
        mode = "a" if digest_path.exists() else "w"
        with digest_path.open(mode, encoding="utf-8") as f:
            if mode == "w":
                f.write(f"# Thread {thread_id}\n\n")
            f.write(entry)

    await _to_thread(_append)
    return digest_path.relative_to(archive.repo_root).as_posix()


def _resolve_archive_relative_path(archive: ProjectArchive, raw_path: str) -> Path:
    """Resolve a relative path safely inside the project archive root.

    Rejects directory traversal and ensures the resolved path stays within
    the project's archive root (defense-in-depth against symlink escapes).
    """
    normalized = (raw_path or "").strip().replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or normalized.startswith("..")
        or "/../" in normalized
        or normalized.endswith("/..")
        or normalized == ".."
    ):
        raise ValueError("Invalid path: directory traversal not allowed")

    safe_rel = normalized.lstrip("/")
    root = archive.root.resolve()
    candidate = (archive.root / safe_rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Invalid path: directory traversal not allowed") from exc
    return candidate


async def process_attachments(
    archive: ProjectArchive,
    body_md: str,
    attachment_paths: Iterable[str] | None,
    convert_markdown: bool,
    *,
    embed_policy: str = "auto",
) -> tuple[str, list[dict[str, object]], list[str]]:
    attachments_meta: list[dict[str, object]] = []
    commit_paths: list[str] = []
    updated_body = body_md
    # Respect explicit convert_markdown decision; embed_policy ("inline"/"file") forces conversion
    should_convert = convert_markdown or embed_policy in {"inline", "file"}
    if should_convert:
        updated_body = await _convert_markdown_images(
            archive, body_md, attachments_meta, commit_paths, embed_policy=embed_policy
        )
    else:
        # Even when not converting, surface inline data-uri images in attachments meta for visibility
        if "data:image" in body_md:
            for m in _IMAGE_PATTERN.finditer(body_md):
                raw_path = m.group("path")
                if raw_path.startswith("data:"):
                    try:
                        header = raw_path.split(",", 1)[0]
                        media_type = "image/webp"
                        if ";" in header:
                            mt = header[5:].split(";", 1)[0]
                            if mt:
                                media_type = mt
                        attachments_meta.append({"type": "inline", "media_type": media_type})
                    except Exception:
                        attachments_meta.append({"type": "inline"})
    if attachment_paths:
        for path in attachment_paths:
            p = Path(path)
            resolved = p.expanduser().resolve() if p.is_absolute() else _resolve_archive_relative_path(archive, path)
            meta, rel_path = await _store_image(archive, resolved, embed_policy=embed_policy)
            attachments_meta.append(meta)
            if rel_path:
                commit_paths.append(rel_path)
    return updated_body, attachments_meta, commit_paths


async def _convert_markdown_images(
    archive: ProjectArchive,
    body_md: str,
    meta: list[dict[str, object]],
    commit_paths: list[str],
    *,
    embed_policy: str = "auto",
) -> str:
    matches = list(_IMAGE_PATTERN.finditer(body_md))
    if not matches:
        return body_md
    result_parts: list[str] = []
    last_idx = 0
    for match in matches:
        path_start, path_end = match.span("path")
        result_parts.append(body_md[last_idx:path_start])
        raw_path = match.group("path")
        normalized_path = raw_path.strip()
        if raw_path.startswith("data:"):
            # Preserve inline data URI and record minimal metadata so callers can assert inline behavior
            try:
                header = normalized_path.split(",", 1)[0]
                media_type = "image/webp"
                if ";" in header:
                    mt = header[5:].split(";", 1)[0]
                    if mt:
                        media_type = mt
                meta.append({
                    "type": "inline",
                    "media_type": media_type,
                })
            except Exception:
                meta.append({"type": "inline"})
            result_parts.append(raw_path)
            last_idx = path_end
            continue
        file_path = Path(normalized_path)
        if file_path.is_absolute():
            file_path = file_path.expanduser().resolve()
        else:
            try:
                file_path = _resolve_archive_relative_path(archive, normalized_path)
            except ValueError:
                result_parts.append(raw_path)
                last_idx = path_end
                continue
        if not file_path.is_file():
            result_parts.append(raw_path)
            last_idx = path_end
            continue
        attachment_meta, rel_path = await _store_image(archive, file_path, embed_policy=embed_policy)
        replacement_value: str
        if attachment_meta["type"] == "inline":
            replacement_value = f"data:image/webp;base64,{attachment_meta['data_base64']}"
        else:
            replacement_value = str(attachment_meta["path"])
        leading_ws_len = len(raw_path) - len(raw_path.lstrip())
        trailing_ws_len = len(raw_path) - len(raw_path.rstrip())
        leading_ws = raw_path[:leading_ws_len] if leading_ws_len else ""
        trailing_ws = raw_path[len(raw_path) - trailing_ws_len :] if trailing_ws_len else ""
        result_parts.append(f"{leading_ws}{replacement_value}{trailing_ws}")
        meta.append(attachment_meta)
        if rel_path:
            commit_paths.append(rel_path)
        last_idx = path_end
    result_parts.append(body_md[last_idx:])
    return "".join(result_parts)


async def _store_image(archive: ProjectArchive, path: Path, *, embed_policy: str = "auto") -> tuple[dict[str, object], str | None]:
    data = await _to_thread(path.read_bytes)

    # Open image and convert, properly closing the original to prevent file handle leaks
    def _open_and_convert(p: Path) -> Image.Image:
        with Image.open(p) as pil:
            return pil.convert("RGBA" if pil.mode in ("LA", "RGBA") else "RGB")

    img = await _to_thread(_open_and_convert, path)
    try:
        width, height = img.size
        buffer_path = archive.attachments_dir
        await _to_thread(buffer_path.mkdir, parents=True, exist_ok=True)
        digest = hashlib.sha1(data).hexdigest()
        target_dir = buffer_path / digest[:2]
        await _to_thread(target_dir.mkdir, parents=True, exist_ok=True)
        target_path = target_dir / f"{digest}.webp"
        # Optionally store original alongside (in originals/)
        original_rel: str | None = None
        if archive.settings.storage.keep_original_images:
            originals_dir = archive.root / "attachments" / "originals" / digest[:2]
            await _to_thread(originals_dir.mkdir, parents=True, exist_ok=True)
            orig_ext = path.suffix.lower().lstrip(".") or "bin"
            orig_path = originals_dir / f"{digest}.{orig_ext}"
            if not orig_path.exists():
                await _to_thread(orig_path.write_bytes, data)
            original_rel = orig_path.relative_to(archive.repo_root).as_posix()
        if not target_path.exists():
            await _save_webp(img, target_path)
        new_bytes = await _to_thread(target_path.read_bytes)
        rel_path = target_path.relative_to(archive.repo_root).as_posix()
        # Update per-attachment manifest with metadata
        try:
            manifest_dir = archive.root / "attachments" / "_manifests"
            await _to_thread(manifest_dir.mkdir, parents=True, exist_ok=True)
            manifest_path = manifest_dir / f"{digest}.json"
            manifest_payload = {
                "sha1": digest,
                "webp_path": rel_path,
                "bytes_webp": len(new_bytes),
                "width": width,
                "height": height,
                "original_path": original_rel,
                "bytes_original": len(data),
                "original_ext": path.suffix.lower(),
            }
            await _write_json(manifest_path, manifest_payload)
            await _append_attachment_audit(
                archive,
                digest,
                {
                    "event": "stored",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "webp_path": rel_path,
                    "bytes_webp": len(new_bytes),
                    "original_path": original_rel,
                    "bytes_original": len(data),
                    "ext": path.suffix.lower(),
                },
            )
        except Exception:
            pass

        should_inline = False
        if embed_policy == "inline":
            should_inline = True
        elif embed_policy == "file":
            should_inline = False
        else:
            should_inline = len(new_bytes) <= archive.settings.storage.inline_image_max_bytes
        if should_inline:
            encoded = base64.b64encode(new_bytes).decode("ascii")
            return {
                "type": "inline",
                "media_type": "image/webp",
                "bytes": len(new_bytes),
                "width": width,
                "height": height,
                "sha1": digest,
                "data_base64": encoded,
            }, rel_path
        meta: dict[str, object] = {
            "type": "file",
            "media_type": "image/webp",
            "bytes": len(new_bytes),
            "path": rel_path,
            "width": width,
            "height": height,
            "sha1": digest,
        }
        if original_rel:
            meta["original_path"] = original_rel
        return meta, rel_path
    finally:
        # Close the converted image to prevent file handle leaks
        img.close()


async def _save_webp(img: Image.Image, path: Path) -> None:
    await _to_thread(img.save, path, format="WEBP", method=6, quality=80)


async def _write_text(path: Path, content: str) -> None:
    await _to_thread(path.parent.mkdir, parents=True, exist_ok=True)
    await _to_thread(path.write_text, content, encoding="utf-8")


async def _write_json(path: Path, payload: dict[str, object]) -> None:
    content = json.dumps(payload, indent=2, sort_keys=True)
    await _write_text(path, content + "\n")


async def _append_attachment_audit(archive: ProjectArchive, sha1: str, event: dict[str, object]) -> None:
    """Append a single JSON line audit record for an attachment digest.

    Creates attachments/_audit/<sha1>.log if missing. Best-effort; failures are ignored.
    """
    try:
        audit_dir = archive.root / "attachments" / "_audit"
        await _to_thread(audit_dir.mkdir, parents=True, exist_ok=True)
        audit_path = audit_dir / f"{sha1}.log"

        def _append_line() -> None:
            line = json.dumps(event, sort_keys=True)
            with audit_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

        await _to_thread(_append_line)
    except Exception:
        pass


def _commit_lock_path(repo_root: Path, rel_paths: Sequence[str]) -> Path:
    """Derive the commit lock path based on project-scoped rel_paths."""
    if not rel_paths:
        return repo_root / ".commit.lock"

    project_slug: str | None = None
    for rel_path in rel_paths:
        parts = PurePosixPath(rel_path).parts
        if len(parts) < 2 or parts[0] != "projects":
            project_slug = None
            break
        slug = parts[1]
        if project_slug is None:
            project_slug = slug
        elif project_slug != slug:
            project_slug = None
            break

    if project_slug:
        return repo_root / "projects" / project_slug / ".commit.lock"
    return repo_root / ".commit.lock"


def _is_git_index_lock_error(exc: BaseException) -> bool:
    """Check if exception is a git index.lock contention error.

    Git uses .git/index.lock for atomic index operations. When multiple
    processes/threads try to modify the index concurrently, the second one
    gets FileExistsError (errno 17) or an OSError wrapping it.
    """
    # Direct FileExistsError
    if isinstance(exc, FileExistsError) and exc.errno == 17:
        return True
    # OSError wrapping FileExistsError from gitdb.util.LockedFD
    if isinstance(exc, OSError):
        err_str = str(exc).lower()
        if "index.lock" in err_str or "lock at" in err_str:
            return True
        # Check __cause__ chain
        cause = exc.__cause__
        if cause is not None and _is_git_index_lock_error(cause):
            return True
    return False


def _try_clean_stale_git_lock(repo_root: Path, max_age_seconds: float = 300.0) -> bool:
    """Attempt to remove a stale .git/index.lock file.

    Returns True if a stale lock was removed, False otherwise.
    Only removes locks older than max_age_seconds to avoid removing active locks.
    """
    lock_path = repo_root / ".git" / "index.lock"
    if not lock_path.exists():
        return False
    try:
        mtime = lock_path.stat().st_mtime
        age = time.time() - mtime
        if age > max_age_seconds:
            lock_path.unlink(missing_ok=True)
            return True
    except Exception:
        pass
    return False


class GitIndexLockError(Exception):
    """Raised when git index.lock contention cannot be resolved after retries."""

    def __init__(self, message: str, lock_path: Path, attempts: int):
        super().__init__(message)
        self.lock_path = lock_path
        self.attempts = attempts


async def _commit_direct(
    repo_root: Path,
    settings: Settings,
    message: str,
    rel_paths: Sequence[str],
) -> None:
    """Perform a git commit directly without queue batching.

    This is the core commit implementation used by both the commit queue
    and direct commits. It handles:
    - Git index.lock contention with exponential backoff
    - Stale lock cleanup
    - EMFILE recovery
    - Trailer injection for agent/thread metadata

    Args:
        repo_root: Path to the git repository root
        settings: Application settings
        message: Commit message
        rel_paths: Relative paths to add and commit
    """
    import errno

    if not rel_paths:
        return

    actor = Actor(settings.storage.git_author_name, settings.storage.git_author_email)
    repo = Repo(str(repo_root))

    def _perform_commit(target_repo: Repo) -> None:
        target_repo.index.add(rel_paths)
        if target_repo.is_dirty(index=True, working_tree=True):
            # Append commit trailers with Agent and optional Thread if present in message text
            trailers: list[str] = []
            # Extract simple Agent/Thread heuristics from the message subject line
            # Expected message formats include:
            #   mail: <Agent> -> ... | <Subject>
            #   file_reservation: <Agent> ...
            try:
                # Avoid duplicating trailers if already embedded
                lower_msg = message.lower()
                have_agent_line = "\nagent:" in lower_msg
                if message.startswith("mail: ") and not have_agent_line:
                    head = message[len("mail: ") :]
                    agent_part = head.split("->", 1)[0].strip()
                    if agent_part:
                        trailers.append(f"Agent: {agent_part}")
                elif message.startswith("file_reservation: ") and not have_agent_line:
                    head = message[len("file_reservation: ") :]
                    agent_part = head.split(" ", 1)[0].strip()
                    if agent_part:
                        trailers.append(f"Agent: {agent_part}")
            except Exception:
                pass
            final_message = message
            if trailers:
                final_message = message + "\n\n" + "\n".join(trailers) + "\n"
            target_repo.index.commit(final_message, author=actor, committer=actor)

    commit_lock_path = _commit_lock_path(repo_root, rel_paths)
    await _to_thread(commit_lock_path.parent.mkdir, parents=True, exist_ok=True)

    async with AsyncFileLock(commit_lock_path):
        attempt_repo = repo
        # Maximum retries for git index.lock contention (happens with concurrent agents)
        max_index_lock_retries = 5
        index_lock_attempts = 0
        last_index_lock_exc: OSError | None = None
        did_last_resort_clean = False

        # +2 to allow: normal retries + 1 potential EMFILE recovery + 1 last resort after stale lock clean
        for attempt in range(max(2, max_index_lock_retries + 2)):
            try:
                await _to_thread(_perform_commit, attempt_repo)
                break
            except OSError as exc:
                # Handle EMFILE (too many open files)
                if exc.errno == errno.EMFILE and attempt < 1:
                    # Low ulimit environments (e.g., macOS CI) can hit EMFILE when spawning git subprocesses.
                    # Best-effort recovery: free cached repos, GC stray Repo handles, and retry with a fresh Repo.
                    with contextlib.suppress(Exception):
                        clear_repo_cache()
                    with contextlib.suppress(Exception):
                        import gc

                        gc.collect()
                    await asyncio.sleep(0.05)
                    with contextlib.suppress(Exception):
                        attempt_repo.close()
                    attempt_repo = Repo(str(repo_root))
                    continue

                # Handle git index.lock contention (concurrent git operations)
                if _is_git_index_lock_error(exc):
                    index_lock_attempts += 1
                    last_index_lock_exc = exc

                    if index_lock_attempts > max_index_lock_retries:
                        # Already exhausted normal retries
                        if not did_last_resort_clean:
                            # Try cleaning stale lock as last resort (lower threshold: 60s instead of 5min)
                            cleaned = _try_clean_stale_git_lock(repo_root, max_age_seconds=60.0)
                            if cleaned:
                                did_last_resort_clean = True
                                continue  # Try one more time after cleaning
                        # Give up with a helpful error
                        lock_path = repo_root / ".git" / "index.lock"
                        raise GitIndexLockError(
                            f"Git index.lock contention after {index_lock_attempts} retries. "
                            f"Another git operation may be in progress. "
                            f"If this persists, manually remove: {lock_path}",
                            lock_path=lock_path,
                            attempts=index_lock_attempts,
                        ) from exc

                    # Exponential backoff: 0.1s, 0.2s, 0.4s, 0.8s, 1.6s
                    # (index_lock_attempts is 1-indexed here, so 2^0=1 -> 0.1s first)
                    delay = 0.1 * (2 ** (index_lock_attempts - 1))
                    await asyncio.sleep(delay)

                    # Try cleaning stale lock (only if older than 5 minutes)
                    with contextlib.suppress(Exception):
                        _try_clean_stale_git_lock(repo_root, max_age_seconds=300.0)
                    continue

                # Other OSError - re-raise
                raise
        else:
            # Loop exhausted without break - should only happen for unexpected error patterns
            if last_index_lock_exc is not None:
                lock_path = repo_root / ".git" / "index.lock"
                raise GitIndexLockError(
                    f"Git index.lock contention after {index_lock_attempts} retries. "
                    f"Another git operation may be in progress. "
                    f"If this persists, manually remove: {lock_path}",
                    lock_path=lock_path,
                    attempts=index_lock_attempts,
                ) from last_index_lock_exc
            raise RuntimeError("git commit failed after recovery attempts")

        if attempt_repo is not repo:
            with contextlib.suppress(Exception):
                attempt_repo.close()

    # Close the repo we opened
    with contextlib.suppress(Exception):
        repo.close()


async def _commit(
    repo: Repo,
    settings: Settings,
    message: str,
    rel_paths: Sequence[str],
    *,
    use_queue: bool = True,
) -> None:
    """Commit changes to a git repository.

    This is the main entry point for committing changes. It wraps _commit_direct
    and optionally uses the commit queue for batching under high load.

    Args:
        repo: Git repository object
        settings: Application settings
        message: Commit message
        rel_paths: Relative paths to add and commit
        use_queue: If True, use commit queue for potential batching (default True)
    """
    if not rel_paths:
        return

    working_tree = repo.working_tree_dir
    if working_tree is None:
        raise ValueError("Repository has no working tree directory")
    repo_root = Path(working_tree).resolve()

    if use_queue:
        # Use commit queue for batching under high load
        queue = await _get_commit_queue()
        await queue.enqueue(repo_root, settings, message, rel_paths)
    else:
        # Direct commit without queue
        await _commit_direct(repo_root, settings, message, rel_paths)


async def heal_archive_locks(settings: Settings) -> dict[str, Any]:
    """Scan the archive root for stale lock artifacts and clean them."""

    root = Path(settings.storage.root).expanduser().resolve()
    await _to_thread(root.mkdir, parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "locks_scanned": 0,
        "locks_removed": [],
        "metadata_removed": [],
    }
    if not root.exists():
        return summary

    for lock_path_item in sorted(root.rglob("*.lock"), key=str):
        # rglob returns Path objects at runtime; cast for type checker
        lock_path = cast(Path, lock_path_item)
        summary["locks_scanned"] += 1
        try:
            lock = AsyncFileLock(lock_path, timeout_seconds=0.0, stale_timeout_seconds=0.0)
            removed = await _to_thread(lock._cleanup_if_stale)
            if removed:
                summary["locks_removed"].append(str(lock_path))
        except FileNotFoundError:
            continue

    for metadata_path_item in sorted(root.rglob("*.lock.owner.json"), key=str):
        # rglob returns Path objects at runtime; cast for type checker
        metadata_path = cast(Path, metadata_path_item)
        name = metadata_path.name
        if not name.endswith(".owner.json"):
            continue
        lock_candidate = metadata_path.parent / name[: -len(".owner.json")]
        if lock_candidate.exists():
            continue
        try:
            await _to_thread(metadata_path.unlink)
            summary["metadata_removed"].append(str(metadata_path))
        except FileNotFoundError:
            continue
        except PermissionError:
            continue

    return summary


# ==================================================================================
# Git Archive Visualization & Analysis Helpers
# ==================================================================================


async def get_recent_commits(
    repo: Repo,
    limit: int = 50,
    project_slug: str | None = None,
    path_filter: str | None = None,
) -> list[dict[str, Any]]:
    """
    Get recent commits from the Git repository.

    Args:
        repo: GitPython Repo object
        limit: Maximum number of commits to return
        project_slug: Optional slug to filter commits for specific project
        path_filter: Optional path pattern to filter commits

    Returns:
        List of commit dicts with keys: sha, short_sha, author, email, date,
        relative_date, subject, body, files_changed, insertions, deletions
    """
    def _get_commits() -> list[dict[str, Any]]:
        commits = []
        path_spec = None

        if project_slug:
            path_spec = f"projects/{project_slug}"
        elif path_filter:
            path_spec = path_filter

        # Get commits, optionally filtered by path (explicit kwargs for better typing)
        if path_spec:
            iterator = repo.iter_commits(paths=[path_spec], max_count=limit)
        else:
            iterator = repo.iter_commits(max_count=limit)

        for commit in iterator:
            # Parse commit stats
            files_changed = len(commit.stats.files)
            insertions = commit.stats.total["insertions"]
            deletions = commit.stats.total["deletions"]

            # Calculate relative date
            commit_time = datetime.fromtimestamp(commit.authored_date, tz=timezone.utc)
            now = datetime.now(timezone.utc)
            delta = now - commit_time

            if delta.days > 30:
                relative_date = commit_time.strftime("%b %d, %Y")
            elif delta.days > 0:
                relative_date = f"{delta.days} day{'s' if delta.days != 1 else ''} ago"
            elif delta.seconds > 3600:
                hours = delta.seconds // 3600
                relative_date = f"{hours} hour{'s' if hours != 1 else ''} ago"
            elif delta.seconds > 60:
                minutes = delta.seconds // 60
                relative_date = f"{minutes} minute{'s' if minutes != 1 else ''} ago"
            else:
                relative_date = "just now"

            message_str = _ensure_str(commit.message)
            commits.append({
                "sha": commit.hexsha,
                "short_sha": commit.hexsha[:8],
                "author": commit.author.name,
                "email": commit.author.email,
                "date": commit_time.isoformat(),
                "relative_date": relative_date,
                "subject": message_str.split("\n")[0],
                "body": message_str,
                "files_changed": files_changed,
                "insertions": insertions,
                "deletions": deletions,
            })

        return commits

    result: list[dict[str, Any]] = await _to_thread(_get_commits)
    return result


async def get_commit_detail(
    repo: Repo, sha: str, max_diff_size: int = 5 * 1024 * 1024
) -> dict[str, Any]:
    """
    Get detailed information about a specific commit including full diff.

    Args:
        repo: GitPython Repo object
        sha: Commit SHA (full or abbreviated)
        max_diff_size: Maximum diff size in bytes (default 5MB)

    Returns:
        Dict with commit metadata and diff information
    """
    def _get_detail() -> dict[str, Any]:
        # Validate SHA format (basic check)
        if not sha or not (7 <= len(sha) <= 40) or not all(c in "0123456789abcdef" for c in sha.lower()):
            raise ValueError("Invalid commit SHA format")

        commit = repo.commit(sha)

        # Get parent for diff (use empty tree if initial commit)
        if commit.parents:
            parent = commit.parents[0]
            diffs = parent.diff(commit, create_patch=True)
        else:
            # Initial commit - diff against empty tree
            diffs = commit.diff(None, create_patch=True)

        # Build unified diff string
        diff_text = ""
        changed_files = []

        for diff in diffs:
            # File metadata
            a_path = diff.a_path or "/dev/null"
            b_path = diff.b_path or "/dev/null"

            # Change type
            if diff.new_file:
                change_type = "added"
            elif diff.deleted_file:
                change_type = "deleted"
            elif diff.renamed_file:
                change_type = "renamed"
            else:
                change_type = "modified"

            changed_files.append({
                "path": b_path if b_path != "/dev/null" else a_path,
                "change_type": change_type,
                "a_path": a_path,
                "b_path": b_path,
            })

            # Get diff text with size limit
            if diff.diff:
                diff_bytes = diff.diff
                if isinstance(diff_bytes, bytes):
                    decoded_diff = diff_bytes.decode("utf-8", errors="replace")
                else:
                    decoded_diff = str(diff_bytes)
                if len(diff_text) + len(decoded_diff) > max_diff_size:
                    diff_text += "\n\n[... Diff truncated - exceeds size limit ...]\n"
                    break
                diff_text += decoded_diff

        # Parse commit body into message and trailers
        message_str = _ensure_str(commit.message)
        lines = message_str.split("\n")
        subject = lines[0] if lines else ""

        # Find where trailers start (Git trailers are at end after blank line)
        # We scan backwards to find the trailer block
        body_lines: list[str] = []
        trailer_lines: list[str] = []

        rest_lines = lines[1:] if len(lines) > 1 else []
        if not rest_lines:
            body = ""
            body_lines = []
            trailer_lines = []
        else:
            # Find trailer block by scanning from end
            # Git trailers must be consecutive lines at the end
            # First, skip trailing blank lines to find last content
            end_idx = len(rest_lines) - 1
            while end_idx >= 0 and not rest_lines[end_idx].strip():
                end_idx -= 1

            # Now scan backwards collecting consecutive trailer-looking lines
            trailer_start_idx = end_idx + 1  # Default: no trailers
            for i in range(end_idx, -1, -1):
                line = rest_lines[i]
                # Trailers have format "Key: Value" with specific pattern
                if line.strip() and ": " in line and not line.startswith(" "):
                    # This looks like a trailer, keep going
                    trailer_start_idx = i
                else:
                    # Not a trailer (blank or other content), stop
                    break

            # Git spec: trailers must be separated from body by blank line
            # If we found trailers, verify there's a blank line before them
            if trailer_start_idx <= end_idx:  # We found some trailers
                if trailer_start_idx > 0:
                    # Check if line before trailers is blank
                    if rest_lines[trailer_start_idx - 1].strip():
                        # No blank line separator - these aren't trailers!
                        trailer_start_idx = end_idx + 1
                        trailer_lines = []
                    else:
                        # Valid trailers with blank separator
                        trailer_lines = rest_lines[trailer_start_idx:end_idx + 1]
                else:
                    # Trailers start at beginning (no body) - this is valid
                    trailer_lines = rest_lines[trailer_start_idx:end_idx + 1]
            else:
                trailer_lines = []

            # Split body and trailers
            body_lines = rest_lines[:trailer_start_idx]

        body = "\n".join(body_lines).strip()

        # Parse trailers into dict (only first occurrence of ": " to handle multiple colons)
        trailers = {}
        for line in trailer_lines:
            if ": " in line:
                parts = line.split(": ", 1)  # Split on first ": " only
                if len(parts) == 2:
                    trailers[parts[0].strip()] = parts[1].strip()

        commit_time = datetime.fromtimestamp(commit.authored_date, tz=timezone.utc)

        return {
            "sha": commit.hexsha,
            "short_sha": commit.hexsha[:8],
            "author": commit.author.name,
            "email": commit.author.email,
            "date": commit_time.isoformat(),
            "subject": subject,
            "body": body,
            "trailers": trailers,
            "files_changed": changed_files,
            "diff": diff_text,
            "stats": {
                "files": len(commit.stats.files),
                "insertions": commit.stats.total["insertions"],
                "deletions": commit.stats.total["deletions"],
            },
        }

    result: dict[str, Any] = await _to_thread(_get_detail)
    return result


async def get_message_commit_sha(archive: ProjectArchive, message_id: int) -> str | None:
    """
    Find the commit SHA that created a specific message.

    Args:
        archive: ProjectArchive instance
        message_id: Message ID to look up

    Returns:
        Commit SHA string or None if not found
    """
    def _find_commit() -> str | None:
        # Find message file in archive
        messages_dir = archive.root / "messages"

        if not messages_dir.exists():
            return None

        # Search for file ending with __{message_id}.md (limit search depth for performance)
        pattern = f"__{message_id}.md"

        # Use iterdir with depth limit instead of rglob for better performance
        for year_dir in messages_dir.iterdir():
            if not year_dir.is_dir():
                continue
            for month_dir in year_dir.iterdir():
                if not month_dir.is_dir():
                    continue
                for md_file in month_dir.iterdir():
                    if md_file.is_file() and md_file.name.endswith(pattern):
                        try:
                            # Get relative path from repo root
                            rel_path = md_file.relative_to(archive.repo_root)

                            # Get FIRST commit that created this file (oldest, not most recent)
                            # iter_commits returns newest first, so we need to get all and take the last
                            # Limit to 1000 commits to prevent performance issues
                            commits_list = list(archive.repo.iter_commits(paths=[str(rel_path)], max_count=1000))
                            if commits_list:
                                # The last commit in the list is the oldest (first commit)
                                return commits_list[-1].hexsha
                        except (ValueError, StopIteration, FileNotFoundError, OSError):
                            # File may have been deleted or moved during iteration
                            continue

        return None

    result: str | None = await _to_thread(_find_commit)
    return result


async def get_archive_tree(
    archive: ProjectArchive,
    path: str = "",
    commit_sha: str | None = None,
) -> list[dict[str, Any]]:
    """
    Get directory tree structure from the Git archive.

    Args:
        archive: ProjectArchive instance
        path: Relative path within the project archive (e.g., "messages/2025")
        commit_sha: Optional commit SHA to view historical tree

    Returns:
        List of tree entries with keys: name, path, type (file/dir), size, mode
    """
    def _get_tree() -> list[dict[str, Any]]:
        # Sanitize path to prevent directory traversal
        if path:
            # Normalize path separators to forward slash
            normalized = path.replace("\\", "/")
            # Reject any path traversal patterns
            if (
                normalized.startswith("/")
                or normalized.startswith("..")
                or "/../" in normalized
                or normalized.endswith("/..")
                or normalized == ".."
            ):
                raise ValueError("Invalid path: directory traversal not allowed")
            safe_path = normalized.lstrip("/")
        else:
            safe_path = ""

        # Get commit (HEAD if not specified)
        if commit_sha:
            # Validate SHA format
            if not (7 <= len(commit_sha) <= 40) or not all(c in "0123456789abcdef" for c in commit_sha.lower()):
                raise ValueError("Invalid commit SHA format")
            commit = archive.repo.commit(commit_sha)
        else:
            commit = archive.repo.head.commit

        # Navigate to the requested path within project root
        project_rel = f"projects/{archive.slug}"
        tree_path = f"{project_rel}/{safe_path}" if safe_path else project_rel

        # Get tree object at path
        try:
            tree_obj = commit.tree / tree_path
        except KeyError:
            # Path doesn't exist
            return []

        # Ensure we have a tree object (not a blob)
        if not isinstance(tree_obj, Tree):
            return []

        entries = []
        for item in tree_obj:
            entry_type = "dir" if item.type == "tree" else "file"
            size = item.size if hasattr(item, "size") else 0

            entries.append({
                "name": item.name,
                "path": f"{path}/{item.name}" if path else item.name,
                "type": entry_type,
                "size": size,
                "mode": item.mode,
            })

        # Sort: directories first, then files, both alphabetically
        entries.sort(key=lambda x: (x["type"] != "dir", str(x["name"]).lower()))

        return entries

    result: list[dict[str, Any]] = await _to_thread(_get_tree)
    return result


async def get_file_content(
    archive: ProjectArchive,
    path: str,
    commit_sha: str | None = None,
    max_size_bytes: int = 10 * 1024 * 1024,  # 10MB default limit
) -> str | None:
    """
    Get file content from the Git archive.

    Args:
        archive: ProjectArchive instance
        path: Relative path within the project archive
        commit_sha: Optional commit SHA to view historical content
        max_size_bytes: Maximum file size to read (prevents DoS)

    Returns:
        File content as string, or None if not found
    """
    def _get_content() -> str | None:
        # Sanitize path to prevent directory traversal
        if path:
            # Normalize path separators to forward slash
            normalized = path.replace("\\", "/")
            # Reject any path traversal patterns
            if (
                normalized.startswith("/")
                or normalized.startswith("..")
                or "/../" in normalized
                or normalized.endswith("/..")
                or normalized == ".."
            ):
                raise ValueError("Invalid path: directory traversal not allowed")
            safe_path = normalized.lstrip("/")
        else:
            return None

        if commit_sha:
            # Validate SHA format
            if not (7 <= len(commit_sha) <= 40) or not all(c in "0123456789abcdef" for c in commit_sha.lower()):
                raise ValueError("Invalid commit SHA format")
            commit = archive.repo.commit(commit_sha)
        else:
            commit = archive.repo.head.commit

        project_rel = f"projects/{archive.slug}/{safe_path}"

        try:
            obj = commit.tree / project_rel
            # Check if it's a file (blob), not a directory (tree)
            if obj.type != "blob":
                raise ValueError("Path is a directory, not a file")
            # Check size before reading
            if obj.size > max_size_bytes:
                raise ValueError(f"File too large: {obj.size} bytes (max {max_size_bytes})")

            stream = obj.data_stream
            try:
                return str(stream.read().decode("utf-8", errors="replace"))
            finally:
                stream.close()
        except KeyError:
            return None

    result: str | None = await _to_thread(_get_content)
    return result


async def get_agent_communication_graph(
    repo: Repo,
    project_slug: str,
    limit: int = 200,
) -> dict[str, Any]:
    """
    Analyze commit history to build an agent communication network graph.

    Args:
        repo: GitPython Repo object
        project_slug: Project slug to analyze
        limit: Maximum number of commits to analyze

    Returns:
        Dict with keys: nodes (list of agent dicts), edges (list of connection dicts)
    """
    def _analyze_graph() -> dict[str, Any]:
        path_spec = f"projects/{project_slug}/messages"

        # Track agent message counts and connections
        agent_stats: dict[str, dict[str, int]] = {}
        connections: dict[tuple[str, str], int] = {}

        for commit in repo.iter_commits(paths=[path_spec], max_count=limit):
            # Parse commit message to extract sender and recipients
            # Format: "mail: Sender -> Recipient1, Recipient2 | Subject"
            message_str = _ensure_str(commit.message)
            subject = message_str.split("\n")[0]

            if not subject.startswith("mail: "):
                continue

            # Extract sender and recipients
            try:
                rest = subject[len("mail: "):]
                sender_part, _ = rest.split(" | ", 1) if " | " in rest else (rest, "")

                if " -> " not in sender_part:
                    continue

                sender, recipients_str = sender_part.split(" -> ", 1)
                sender = str(sender).strip()
                recipients = [r.strip() for r in recipients_str.split(",")]

                # Update sender stats
                if sender not in agent_stats:
                    agent_stats[sender] = {"sent": 0, "received": 0}
                agent_stats[sender]["sent"] = agent_stats[sender].get("sent", 0) + 1

                # Update recipient stats and connections
                for recipient in recipients:
                    if not recipient:
                        continue

                    recipient = str(recipient)
                    if recipient not in agent_stats:
                        agent_stats[recipient] = {"sent": 0, "received": 0}
                    agent_stats[recipient]["received"] = agent_stats[recipient].get("received", 0) + 1

                    # Track connection
                    conn_key: tuple[str, str] = (sender, recipient)
                    connections[conn_key] = int(connections.get(conn_key, 0)) + 1

            except Exception:
                # Skip malformed commit messages
                continue

        # Build nodes list
        nodes = []
        for agent_name, stats in agent_stats.items():
            total = stats["sent"] + stats["received"]
            nodes.append({
                "id": agent_name,
                "label": agent_name,
                "sent": stats["sent"],
                "received": stats["received"],
                "total": total,
            })

        # Build edges list
        edges = []
        for (sender, recipient), count in connections.items():
            edges.append({
                "from": sender,
                "to": recipient,
                "count": count,
            })

        return {
            "nodes": nodes,
            "edges": edges,
        }

    result: dict[str, Any] = await _to_thread(_analyze_graph)
    return result


async def get_timeline_commits(
    repo: Repo,
    project_slug: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    Get commits formatted for timeline visualization with Mermaid.js.

    Args:
        repo: GitPython Repo object
        project_slug: Project slug to analyze
        limit: Maximum number of commits

    Returns:
        List of commit dicts with timeline-specific metadata
    """
    def _get_timeline() -> list[dict[str, Any]]:
        path_spec = f"projects/{project_slug}"

        timeline = []
        for commit in repo.iter_commits(paths=[path_spec], max_count=limit):
            message_str = _ensure_str(commit.message)
            subject = message_str.split("\n")[0]
            commit_time = datetime.fromtimestamp(commit.authored_date, tz=timezone.utc)

            # Classify commit type
            commit_type = "other"
            sender = None
            recipients = []

            if subject.startswith("mail: "):
                commit_type = "message"
                # Parse sender and recipients
                try:
                    rest = subject[len("mail: "):]
                    sender_part, _ = rest.split(" | ", 1) if " | " in rest else (rest, "")
                    if " -> " in sender_part:
                        sender, recipients_str = sender_part.split(" -> ", 1)
                        sender = sender.strip()
                        recipients = [r.strip() for r in recipients_str.split(",")]
                except Exception:
                    pass
            elif subject.startswith("file_reservation: "):
                commit_type = "file_reservation"
            elif subject.startswith("chore: "):
                commit_type = "chore"

            timeline.append({
                "sha": commit.hexsha,
                "short_sha": commit.hexsha[:8],
                "date": commit_time.isoformat(),
                "timestamp": commit.authored_date,
                "subject": subject,
                "type": commit_type,
                "sender": sender,
                "recipients": recipients,
                "author": commit.author.name,
            })

        # Sort by timestamp (oldest first for timeline)
        def _get_timestamp(x: dict[str, Any]) -> int:
            ts = x.get("timestamp", 0)
            return int(ts) if isinstance(ts, (int, float)) else 0
        timeline.sort(key=_get_timestamp)

        return timeline

    result: list[dict[str, Any]] = await _to_thread(_get_timeline)
    return result


async def get_historical_inbox_snapshot(
    archive: ProjectArchive,
    agent_name: str,
    timestamp: str,
    limit: int = 100,
) -> dict[str, Any]:
    """
    Get historical snapshot of agent inbox at specific timestamp.

    Traverses Git history to find the commit closest to (but not after)
    the specified timestamp, then lists all message files in the agent's
    inbox directory at that point in history.

    Args:
        archive: ProjectArchive instance with Git repo
        agent_name: Agent name to get inbox for
        timestamp: ISO 8601 timestamp (e.g., "2024-01-15T10:30:00")
        limit: Maximum messages to return (capped at 500)

    Returns:
        Dict with keys:
            - messages: List of message dicts with id, subject, date, from, importance
            - snapshot_time: ISO timestamp of the actual commit used
            - commit_sha: Git commit hash
            - requested_time: The original requested timestamp
    """
    # Cap limit for safety
    limit = max(1, min(limit, 500))

    def _get_snapshot() -> dict[str, Any]:
        try:
            # Parse timestamp - handle both with and without timezone
            timestamp_clean = timestamp.replace('Z', '+00:00')
            target_time = datetime.fromisoformat(timestamp_clean)

            # If naive datetime (no timezone), assume UTC
            # This handles datetime-local input which doesn't include timezone
            if target_time.tzinfo is None:
                target_time = target_time.replace(tzinfo=timezone.utc)

            target_timestamp = target_time.timestamp()
        except (ValueError, AttributeError) as e:
            return {
                "messages": [],
                "snapshot_time": None,
                "commit_sha": None,
                "requested_time": timestamp,
                "error": f"Invalid timestamp format: {e}"
            }

        # Get agent inbox directory at that commit
        inbox_path = f"projects/{archive.slug}/agents/{agent_name}/inbox"

        # Find commit closest to (but not after) target timestamp
        closest_commit = None
        try:
            commit_iter = archive.repo.iter_commits(max_count=10000, paths=[inbox_path])
        except Exception:
            commit_iter = archive.repo.iter_commits(max_count=10000)
        for commit in commit_iter:
            if commit.authored_date <= target_timestamp:
                closest_commit = commit
                break

        if not closest_commit:
            # Fall back to full history when the inbox path has never been touched
            for commit in archive.repo.iter_commits(max_count=10000):
                if commit.authored_date <= target_timestamp:
                    closest_commit = commit
                    break

        if not closest_commit:
            # No commits before this time
            return {
                "messages": [],
                "snapshot_time": None,
                "commit_sha": None,
                "requested_time": timestamp,
                "note": "No commits found before this timestamp"
            }

        messages = []
        try:
            # Navigate to the inbox folder in the commit tree
            tree = closest_commit.tree
            for part in inbox_path.split("/"):
                tree = tree / part

            # Recursively traverse inbox subdirectories (YYYY/MM/) to find message files
            def traverse_tree(subtree: Any, depth: int = 0) -> None:
                """Recursively traverse git tree looking for .md files"""
                if depth > 3:  # Safety limit: inbox/YYYY/MM is 2 levels, add buffer
                    return

                for item in subtree:
                    if item.type == "blob" and item.name.endswith(".md"):
                        # Parse filename: YYYY-MM-DDTHH-MM-SSZ__subject-slug__id.md
                        parts = item.name.rsplit("__", 2)

                        if len(parts) >= 2:
                            date_str = parts[0]
                            # Handle both 2-part and 3-part filenames
                            if len(parts) == 3:
                                subject_slug = parts[1]
                                msg_id = parts[2].replace(".md", "")
                            else:
                                # 2-part filename: date__subject.md
                                subject_slug = parts[1].replace(".md", "")
                                msg_id = "unknown"

                            # Convert slug back to readable subject
                            subject = subject_slug.replace("-", " ").replace("_", " ").title()

                            # Read file content to get From field and other metadata
                            from_agent = "unknown"
                            importance = "normal"

                            try:
                                stream = item.data_stream
                                try:
                                    blob_content = stream.read().decode('utf-8', errors='ignore')
                                finally:
                                    stream.close()

                                # Parse JSON frontmatter (format: ---json\n{...}\n---)
                                if blob_content.startswith('---json\n') or blob_content.startswith('---json\r\n'):
                                    # Find the closing --- delimiter
                                    end_marker = blob_content.find('\n---\n', 8)
                                    if end_marker == -1:
                                        end_marker = blob_content.find('\r\n---\r\n', 8)

                                    if end_marker > 0:
                                        # Extract JSON between markers
                                        # '---json\n' is 8 chars, '---json\r\n' is 9 chars
                                        json_start = 8 if blob_content.startswith('---json\n') else 9
                                        json_str = blob_content[json_start:end_marker]

                                        try:
                                            metadata = json.loads(json_str)
                                            # Extract sender from 'from' field
                                            if 'from' in metadata:
                                                from_agent = str(metadata['from'])
                                            # Extract importance
                                            if 'importance' in metadata:
                                                importance = str(metadata['importance'])
                                            # Extract actual subject
                                            if 'subject' in metadata:
                                                actual_subject = str(metadata['subject']).strip()
                                                if actual_subject:
                                                    subject = actual_subject
                                        except (json.JSONDecodeError, KeyError, TypeError):
                                            pass  # Use defaults if JSON parsing fails

                            except Exception:
                                pass  # Use defaults if parsing fails

                            messages.append({
                                "id": msg_id,
                                "subject": subject,
                                "date": date_str,
                                "from": from_agent,
                                "importance": importance,
                            })

                            if len(messages) >= limit:
                                return  # Stop when we hit the limit

                    elif item.type == "tree":
                        # Recursively traverse subdirectory
                        traverse_tree(item, depth + 1)
                        if len(messages) >= limit:
                            return  # Stop when we hit the limit

            # Start recursive traversal
            traverse_tree(tree)

        except (KeyError, AttributeError):
            # Inbox directory didn't exist at that time
            pass

        # Sort messages by date (newest first)
        messages.sort(key=lambda m: m["date"], reverse=True)

        return {
            "messages": messages,
            "snapshot_time": closest_commit.authored_datetime.isoformat(),
            "commit_sha": closest_commit.hexsha,
            "requested_time": timestamp,
        }

    result: dict[str, Any] = await _to_thread(_get_snapshot)
    return result


# =============================================================================
# Doctor Backup Infrastructure
# =============================================================================


@dataclass(slots=True)
class BackupManifest:
    """Manifest describing a diagnostic backup."""

    version: int
    created_at: str
    reason: str
    database_path: str | None
    project_bundles: list[str]
    storage_root: str
    restore_instructions: str


async def create_diagnostic_backup(
    settings: Settings,
    project_slug: str | None = None,
    backup_dir: Path | None = None,
    reason: str = "doctor-repair",
) -> Path:
    """Create a timestamped backup before repair operations.

    Format: Git bundle + SQLite copy (fast, complete, restorable)

    Args:
        settings: Application settings
        project_slug: Specific project to backup, or None for all projects
        backup_dir: Directory to store backup, or None for default
        reason: Reason for backup (included in manifest)

    Returns:
        Path to backup directory containing:
        - {project_slug}.bundle (git bundle of project archive)
        - database.sqlite3 (copy of full database)
        - manifest.json (what was backed up, when, why, restore instructions)
    """
    import shutil

    from .db import get_database_path

    # Create backup directory
    if backup_dir is None:
        backup_dir = Path(settings.storage.root) / "backups"
    backup_dir = backup_dir.expanduser().resolve()

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    backup_path = backup_dir / f"{timestamp}_{reason}"
    backup_path.mkdir(parents=True, exist_ok=True)

    project_bundles: list[str] = []
    database_copied: str | None = None

    # Get the archive repo
    archive_root = Path(settings.storage.root).expanduser().resolve()
    if not archive_root.exists():
        raise ValueError(f"Storage root does not exist: {archive_root}")

    # Copy database
    db_path = get_database_path(settings)
    if db_path and db_path.exists():
        db_backup = backup_path / "database.sqlite3"

        def _copy_db() -> None:
            # Use shutil.copy2 to preserve metadata
            shutil.copy2(db_path, db_backup)
            # Also copy WAL and SHM files if they exist
            wal_path = db_path.with_suffix(".sqlite3-wal")
            shm_path = db_path.with_suffix(".sqlite3-shm")
            if wal_path.exists():
                shutil.copy2(wal_path, backup_path / wal_path.name)
            if shm_path.exists():
                shutil.copy2(shm_path, backup_path / shm_path.name)

        await _to_thread(_copy_db)
        database_copied = str(db_backup)

    # Create git bundles for projects
    repo_path = archive_root
    if repo_path.exists() and (repo_path / ".git").exists():

        def _create_bundles() -> list[str]:
            bundles: list[str] = []
            repo = Repo(repo_path)
            try:
                if project_slug:
                    # Single project bundle
                    bundle_path = backup_path / f"{project_slug}.bundle"
                    try:
                        # Create bundle of the entire repo (includes all history)
                        repo.git.bundle("create", str(bundle_path), "--all")
                        bundles.append(str(bundle_path))
                    except Exception:
                        pass  # Skip if bundle creation fails
                else:
                    # Bundle entire archive
                    bundle_path = backup_path / "archive.bundle"
                    try:
                        repo.git.bundle("create", str(bundle_path), "--all")
                        bundles.append(str(bundle_path))
                    except Exception:
                        pass
            finally:
                repo.close()

            return bundles

        project_bundles = await _to_thread(_create_bundles)

    # Write manifest
    manifest = BackupManifest(
        version=1,
        created_at=datetime.now(timezone.utc).isoformat(),
        reason=reason,
        database_path=database_copied,
        project_bundles=project_bundles,
        storage_root=str(archive_root),
        restore_instructions=(
            "To restore:\n"
            "1. Stop any running MCP Agent Mail processes\n"
            "2. Copy database.sqlite3 back to original location\n"
            "3. Use 'git clone --bare <bundle> <target>' to restore archive\n"
            "4. Restart MCP Agent Mail"
        ),
    )

    manifest_path = backup_path / "manifest.json"

    def _write_manifest() -> None:
        with manifest_path.open("w", encoding="utf-8") as f:
            json.dump(
                {
                    "version": manifest.version,
                    "created_at": manifest.created_at,
                    "reason": manifest.reason,
                    "database_path": manifest.database_path,
                    "project_bundles": manifest.project_bundles,
                    "storage_root": manifest.storage_root,
                    "restore_instructions": manifest.restore_instructions,
                },
                f,
                indent=2,
            )

    await _to_thread(_write_manifest)

    return backup_path


async def list_backups(settings: Settings) -> list[dict[str, Any]]:
    """List all available diagnostic backups.

    Returns:
        List of backup info dicts with path, created_at, reason, size
    """
    backup_dir = Path(settings.storage.root).expanduser().resolve() / "backups"
    if not backup_dir.exists():
        return []

    backups: list[dict[str, Any]] = []

    def _scan_backups() -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for entry in sorted(backup_dir.iterdir(), reverse=True):
            if entry.is_dir():
                manifest_path = entry / "manifest.json"
                if manifest_path.exists():
                    try:
                        with manifest_path.open(encoding="utf-8") as f:
                            manifest_data = json.load(f)
                        # Calculate total size
                        total_size = sum(
                            p.stat().st_size for p in entry.rglob("*") if p.is_file()
                        )
                        results.append({
                            "path": str(entry),
                            "created_at": manifest_data.get("created_at"),
                            "reason": manifest_data.get("reason"),
                            "size_bytes": total_size,
                            "has_database": manifest_data.get("database_path") is not None,
                            "bundle_count": len(manifest_data.get("project_bundles", [])),
                        })
                    except (json.JSONDecodeError, OSError):
                        pass
        return results

    backups = await _to_thread(_scan_backups)
    return backups


async def restore_from_backup(
    settings: Settings,
    backup_path: Path,
    target_project: str | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Restore from a diagnostic backup.

    Args:
        settings: Application settings
        backup_path: Path to backup directory
        target_project: Specific project to restore, or None for all
        dry_run: If True, only report what would be restored

    Returns:
        Dict with restoration results
    """
    import shutil

    from .db import get_database_path

    manifest_path = backup_path / "manifest.json"
    if not manifest_path.exists():
        raise ValueError(f"No manifest.json found in {backup_path}")

    def _read_manifest() -> dict[str, Any]:
        with manifest_path.open(encoding="utf-8") as f:
            return json.load(f)

    manifest_data = await _to_thread(_read_manifest)

    results: dict[str, Any] = {
        "backup_path": str(backup_path),
        "created_at": manifest_data.get("created_at"),
        "reason": manifest_data.get("reason"),
        "dry_run": dry_run,
        "database_restored": False,
        "bundles_restored": [],
        "errors": [],
    }

    if dry_run:
        results["would_restore_database"] = manifest_data.get("database_path") is not None
        results["would_restore_bundles"] = manifest_data.get("project_bundles", [])
        return results

    # Restore database
    db_backup = backup_path / "database.sqlite3"
    if db_backup.exists():
        db_path = get_database_path(settings)
        if db_path:
            try:

                def _restore_db() -> None:
                    # Backup current DB first (safety)
                    if db_path.exists():
                        shutil.copy2(db_path, db_path.with_suffix(".sqlite3.pre-restore"))
                    shutil.copy2(db_backup, db_path)
                    # Also restore WAL and SHM if present in backup
                    for suffix in ["-wal", "-shm"]:
                        backup_file = backup_path / f"database.sqlite3{suffix}"
                        target_file = db_path.with_suffix(f".sqlite3{suffix}")
                        if backup_file.exists():
                            shutil.copy2(backup_file, target_file)

                await _to_thread(_restore_db)
                results["database_restored"] = True
            except Exception as e:
                results["errors"].append(f"Database restore failed: {e}")

    # Restore git bundles
    bundles = manifest_data.get("project_bundles", [])
    archive_root = Path(settings.storage.root).expanduser().resolve()

    def _restore_bundle(bundle_to_restore: Path, target_root: Path) -> None:
        """Restore a git bundle to target directory."""
        # Backup current archive
        if target_root.exists():
            backup_archive = target_root.with_suffix(".pre-restore")
            if backup_archive.exists():
                shutil.rmtree(backup_archive)
            shutil.copytree(target_root, backup_archive)
            shutil.rmtree(target_root)

        # Clone from bundle
        Repo.clone_from(str(bundle_to_restore), str(target_root))

    for bundle_path_str in bundles:
        bundle_path = Path(bundle_path_str)
        if not bundle_path.exists():
            # Try relative to backup_path
            bundle_path = backup_path / bundle_path.name
        if not bundle_path.exists():
            results["errors"].append(f"Bundle not found: {bundle_path_str}")
            continue

        try:
            await _to_thread(_restore_bundle, bundle_path, archive_root)
            results["bundles_restored"].append(str(bundle_path))
        except Exception as e:
            results["errors"].append(f"Bundle restore failed for {bundle_path}: {e}")

    return results


# -------------------------------------------------------------------------------------------------
# Push Notifications: Signal file approach for local deployments
# -------------------------------------------------------------------------------------------------
# When enabled, write a signal file when a message is delivered to an agent's inbox.
# Agents can watch these files using inotify/FSEvents/kqueue for instant notifications
# without polling. This is useful for local multi-agent workflows.
#
# Signal file path: {signals_dir}/projects/{project_slug}/agents/{agent_name}.signal
# Signal file contents: JSON with message metadata (id, from, subject, importance, timestamp)

# Debounce tracking: (project_slug, agent_name) -> last_signal_time
_SIGNAL_DEBOUNCE: dict[tuple[str, str], float] = {}


async def emit_notification_signal(
    settings: Settings,
    project_slug: str,
    agent_name: str,
    message_metadata: dict[str, Any] | None = None,
) -> bool:
    """Emit a notification signal for an agent in a project.

    This creates/updates a signal file that agents can watch for incoming messages.
    The signal file contains metadata about the notification for context.

    Args:
        settings: Application settings (must have notifications enabled)
        project_slug: Project identifier
        agent_name: Target agent name
        message_metadata: Optional dict with message info (id, from, subject, importance)

    Returns:
        True if signal was emitted, False if notifications disabled or debounced
    """
    if not settings.notifications.enabled:
        return False

    # Debounce check: skip if we signaled this agent recently
    debounce_key = (project_slug, agent_name)
    debounce_ms = settings.notifications.debounce_ms
    now_ms = time.time() * 1000

    last_signal = _SIGNAL_DEBOUNCE.get(debounce_key, 0)
    if now_ms - last_signal < debounce_ms:
        return False  # Too soon, skip

    _SIGNAL_DEBOUNCE[debounce_key] = now_ms

    # Build signal file path
    signals_dir = Path(settings.notifications.signals_dir).expanduser().resolve()
    signal_path = signals_dir / "projects" / project_slug / "agents" / f"{agent_name}.signal"

    # Prepare signal content
    signal_data: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "project": project_slug,
        "agent": agent_name,
    }

    if settings.notifications.include_metadata and message_metadata:
        signal_data["message"] = {
            "id": message_metadata.get("id"),
            "from": message_metadata.get("from"),
            "subject": message_metadata.get("subject"),
            "importance": message_metadata.get("importance", "normal"),
        }

    # Write signal file
    def _write_signal() -> None:
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        signal_path.write_text(json.dumps(signal_data, indent=2), encoding="utf-8")

    try:
        await _to_thread(_write_signal)
        return True
    except Exception:
        # Signal emission is best-effort; don't fail message delivery
        return False


async def clear_notification_signal(
    settings: Settings,
    project_slug: str,
    agent_name: str,
) -> bool:
    """Clear notification signal for an agent (called when inbox is read).

    This removes the signal file to indicate the agent has acknowledged notifications.

    Args:
        settings: Application settings
        project_slug: Project identifier
        agent_name: Target agent name

    Returns:
        True if signal was cleared, False if file didn't exist or error
    """
    if not settings.notifications.enabled:
        return False

    signals_dir = Path(settings.notifications.signals_dir).expanduser().resolve()
    signal_path = signals_dir / "projects" / project_slug / "agents" / f"{agent_name}.signal"

    def _clear_signal() -> bool:
        if signal_path.exists():
            signal_path.unlink()
            return True
        return False

    try:
        return await _to_thread(_clear_signal)
    except Exception:
        return False


def list_pending_signals(settings: Settings, project_slug: str | None = None) -> list[dict[str, Any]]:
    """List all pending notification signals.

    Args:
        settings: Application settings
        project_slug: Optional filter by project

    Returns:
        List of signal info dicts with project, agent, and metadata
    """
    if not settings.notifications.enabled:
        return []

    signals_dir = Path(settings.notifications.signals_dir).expanduser().resolve()
    if not signals_dir.exists():
        return []

    results: list[dict[str, Any]] = []
    projects_dir = signals_dir / "projects"

    if not projects_dir.exists():
        return []

    project_dirs = [projects_dir / project_slug] if project_slug else list(projects_dir.iterdir())

    for proj_dir in project_dirs:
        if not proj_dir.is_dir():
            continue
        agents_dir = proj_dir / "agents"
        if not agents_dir.exists():
            continue

        for signal_file in agents_dir.glob("*.signal"):
            try:
                data = json.loads(signal_file.read_text(encoding="utf-8"))
                results.append(data)
            except Exception:
                # Corrupted signal file; include minimal info
                results.append({
                    "project": proj_dir.name,
                    "agent": signal_file.stem,
                    "error": "Failed to parse signal file",
                })

    return results
