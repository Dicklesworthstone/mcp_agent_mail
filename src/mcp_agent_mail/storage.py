"""Filesystem and Git archive helpers for MCP Agent Mail."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from filelock import SoftFileLock, Timeout
from git import Actor, Repo
from PIL import Image

from .config import Settings

_IMAGE_PATTERN = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<path>[^)]+)\)")


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


class AsyncFileLock:
    """Async-friendly wrapper around SoftFileLock with metadata tracking."""

    def __init__(
        self,
        path: Path,
        *,
        timeout_seconds: float = 60.0,
        stale_timeout_seconds: float = 180.0,
    ) -> None:
        self._path = Path(path)
        self._lock = SoftFileLock(str(self._path))
        self._timeout = float(timeout_seconds)
        self._stale_timeout = float(max(stale_timeout_seconds, 0.0))
        self._pid = os.getpid()
        self._metadata_path = self._path.parent / f"{self._path.name}.owner.json"
        self._held = False
        self._lock_key = str(self._path.resolve())
        self._loop_key: tuple[int, str] | None = None
        self._process_lock: asyncio.Lock | None = None
        self._process_lock_held = False

    async def __aenter__(self) -> None:
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
            while True:
                try:
                    if self._timeout <= 0:
                        await _to_thread(self._lock.acquire)
                    else:
                        await _to_thread(self._lock.acquire, self._timeout)
                    self._held = True
                    await _to_thread(self._write_metadata)
                    break
                except Timeout:
                    cleaned = await _to_thread(self._cleanup_if_stale)
                    if cleaned:
                        continue
                    raise TimeoutError(
                        f"Timed out acquiring lock {self._path} after {self._timeout:.2f}s "
                        "and no stale owner detected."
                    ) from None
        except Exception:
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
        return None

    def _cleanup_if_stale(self) -> bool:
        """Remove lock and metadata when the previous holder is gone and the lock is stale."""
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
        if owner_alive:
            return False
        if isinstance(age, (int, float)) and age < self._stale_timeout:
            return False
        with contextlib.suppress(Exception):
            self._path.unlink()
        with contextlib.suppress(Exception):
            self._metadata_path.unlink()
        return True

    def _write_metadata(self) -> None:
        payload = {
            "pid": self._pid,
            "created_ts": time.time(),
        }
        self._metadata_path.write_text(json.dumps(payload), encoding="utf-8")
        return None

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._held:
            with contextlib.suppress(Exception):
                await _to_thread(self._lock.release)
            with contextlib.suppress(Exception):
                await _to_thread(self._metadata_path.unlink)
            with contextlib.suppress(Exception):
                await _to_thread(self._path.unlink)
            self._held = False
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
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True


@asynccontextmanager
async def archive_write_lock(archive: ProjectArchive, *, timeout_seconds: float = 60.0):
    """Context manager for safely mutating archive surfaces."""

    lock = AsyncFileLock(archive.lock_path, timeout_seconds=timeout_seconds)
    await lock.__aenter__()
    try:
        yield
    except Exception as exc:
        await lock.__aexit__(type(exc), exc, exc.__traceback__)
        raise
    else:
        await lock.__aexit__(None, None, None)


async def _to_thread(func, /, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


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
            is_stale = bool(metadata) and not info["owner_alive"] and isinstance(age_val, (int, float)) and age_val >= stale_threshold
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
    git_dir = root / ".git"
    if git_dir.exists():
        return Repo(str(root))

    repo = await _to_thread(Repo.init, str(root))
    # Ensure deterministic, non-interactive commits (disable GPG signing)
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


async def write_file_reservation_record(archive: ProjectArchive, file_reservation: dict[str, object]) -> None:
    path_pattern = str(file_reservation.get("path_pattern") or file_reservation.get("path") or "").strip()
    if not path_pattern:
        raise ValueError("File reservation record must include 'path_pattern'.")
    normalized_file_reservation = dict(file_reservation)
    normalized_file_reservation["path_pattern"] = path_pattern
    normalized_file_reservation.pop("path", None)
    digest = hashlib.sha1(path_pattern.encode("utf-8")).hexdigest()
    file_reservation_path = archive.root / "file_reservations" / f"{digest}.json"
    await _write_json(file_reservation_path, normalized_file_reservation)
    agent_name = str(normalized_file_reservation.get("agent", "unknown"))
    await _commit(
        archive.repo,
        archive.settings,
        f"file_reservation: {agent_name} {path_pattern}",
        [file_reservation_path.relative_to(archive.repo_root).as_posix()],
    )


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
    timestamp_str = timestamp_obj if isinstance(timestamp_obj, str) else datetime.now(timezone.utc).isoformat()
    now = datetime.fromisoformat(timestamp_str)
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
    subject_slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", subject_value).strip("-_").lower()[:80] or "message"
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
    if convert_markdown and archive.settings.storage.convert_images:
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
            if not p.is_absolute():
                p = (archive.root / path).resolve()
            meta, rel_path = await _store_image(archive, p, embed_policy=embed_policy)
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
        if not file_path.is_absolute():
            file_path = (archive.root / raw_path).resolve()
        if not file_path.is_file():
            result_parts.append(raw_path)
            last_idx = path_end
            continue
        attachment_meta, rel_path = await _store_image(archive, file_path, embed_policy=embed_policy)
        if attachment_meta["type"] == "inline":
            replacement_value = f"data:image/webp;base64,{attachment_meta['data_base64']}"
        else:
            replacement_value = attachment_meta["path"]
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
    pil = await _to_thread(Image.open, path)
    img = pil.convert("RGBA" if pil.mode in ("LA", "RGBA") else "RGB")
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


async def _commit(repo: Repo, settings: Settings, message: str, rel_paths: Sequence[str]) -> None:
    if not rel_paths:
        return
    actor = Actor(settings.storage.git_author_name, settings.storage.git_author_email)

    def _perform_commit() -> None:
        repo.index.add(rel_paths)
        if repo.is_dirty(index=True, working_tree=True):
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
            repo.index.commit(final_message, author=actor, committer=actor)
    # Serialize commits across all projects sharing the same Git repo to avoid index races
    commit_lock_path = Path(repo.working_tree_dir).resolve() / ".commit.lock"
    async with AsyncFileLock(commit_lock_path):
        await _to_thread(_perform_commit)


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

    for lock_path in sorted(root.rglob("*.lock"), key=str):
        summary["locks_scanned"] += 1
        try:
            lock = AsyncFileLock(lock_path, timeout_seconds=0.0, stale_timeout_seconds=0.0)
            removed = await _to_thread(lock._cleanup_if_stale)
            if removed:
                summary["locks_removed"].append(str(lock_path))
        except FileNotFoundError:
            continue

    for metadata_path in sorted(root.rglob("*.lock.owner.json"), key=str):
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

            commits.append({
                "sha": commit.hexsha,
                "short_sha": commit.hexsha[:8],
                "author": commit.author.name,
                "email": commit.author.email,
                "date": commit_time.isoformat(),
                "relative_date": relative_date,
                "subject": commit.message.split("\n")[0],
                "body": commit.message,
                "files_changed": files_changed,
                "insertions": insertions,
                "deletions": deletions,
            })

        return commits

    return await _to_thread(_get_commits)


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
                decoded_diff = diff.diff.decode("utf-8", errors="replace")
                if len(diff_text) + len(decoded_diff) > max_diff_size:
                    diff_text += "\n\n[... Diff truncated - exceeds size limit ...]\n"
                    break
                diff_text += decoded_diff

        # Parse commit body into message and trailers
        lines = commit.message.split("\n")
        subject = lines[0] if lines else ""

        # Find where trailers start (Git trailers are at end after blank line)
        # We scan backwards to find the trailer block
        body_lines = []
        trailer_lines = []

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

    return await _to_thread(_get_detail)


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

    return await _to_thread(_find_commit)


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
            tree = commit.tree / tree_path
        except KeyError:
            # Path doesn't exist
            return []

        entries = []
        for item in tree:
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
        entries.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))

        return entries

    return await _to_thread(_get_tree)


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
            return obj.data_stream.read().decode("utf-8", errors="replace")
        except KeyError:
            return None

    return await _to_thread(_get_content)


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
            subject = commit.message.split("\n")[0]

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

    return await _to_thread(_analyze_graph)


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
            subject = commit.message.split("\n")[0]
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
        timeline.sort(key=lambda x: x["timestamp"])

        return timeline

    return await _to_thread(_get_timeline)


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

        # Find commit closest to (but not after) target timestamp
        closest_commit = None
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

        # Get agent inbox directory at that commit
        inbox_path = f"projects/{archive.slug}/agents/{agent_name}/inbox"

        messages = []
        try:
            # Navigate to the inbox folder in the commit tree
            tree = closest_commit.tree
            for part in inbox_path.split("/"):
                tree = tree / part

            # Recursively traverse inbox subdirectories (YYYY/MM/) to find message files
            def traverse_tree(subtree, depth=0):
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
                                blob_content = item.data_stream.read().decode('utf-8', errors='ignore')

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

    return await _to_thread(_get_snapshot)
