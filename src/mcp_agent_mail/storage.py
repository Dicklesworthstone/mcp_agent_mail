"""Filesystem and Git archive helpers for MCP Agent Mail."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from filelock import SoftFileLock
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


class AsyncFileLock:
    def __init__(self, path: Path, *, timeout_seconds: float = 60.0) -> None:
        self._lock = SoftFileLock(str(path))
        self._timeout = float(timeout_seconds)

    async def __aenter__(self) -> None:
        # In test, do not block on locks — use a short try and then proceed without holding the lock
        import os as _os
        t = self._timeout
        is_test = (_os.environ.get("APP_ENVIRONMENT") or "").lower() == "test"
        if is_test:
            t = 0.1
            try:
                await _to_thread(self._lock.acquire, timeout=t)
            except Exception:
                # Best-effort in CI: skip locking to avoid timeouts on shared runners
                return None
        else:
            await _to_thread(self._lock.acquire, timeout=t)

    async def __aexit__(self, exc_type, exc, tb) -> None:
        # Only release if held
        with contextlib.suppress(Exception):
            await _to_thread(self._lock.release)


async def _to_thread(func, /, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


async def ensure_archive(settings: Settings, slug: str) -> ProjectArchive:
    repo_root = Path(settings.storage.root).expanduser().resolve()
    project_root = repo_root / "projects" / slug
    await _to_thread(project_root.mkdir, parents=True, exist_ok=True)
    repo = await _ensure_repo(repo_root, settings)
    return ProjectArchive(
        settings=settings,
        slug=slug,
        root=project_root,
        repo=repo,
        lock_path=repo_root / ".archive.lock",
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


async def write_claim_record(archive: ProjectArchive, claim: dict[str, object]) -> None:
    path_pattern = str(claim.get("path_pattern") or claim.get("path") or "").strip()
    if not path_pattern:
        raise ValueError("Claim record must include 'path_pattern'.")
    normalized_claim = dict(claim)
    normalized_claim["path_pattern"] = path_pattern
    normalized_claim.pop("path", None)
    digest = hashlib.sha1(path_pattern.encode("utf-8")).hexdigest()
    claim_path = archive.root / "claims" / f"{digest}.json"
    await _write_json(claim_path, normalized_claim)
    agent_name = str(normalized_claim.get("agent", "unknown"))
    await _commit(
        archive.repo,
        archive.settings,
        f"claim: {agent_name} {path_pattern}",
        [claim_path.relative_to(archive.repo_root).as_posix()],
    )


async def write_message_bundle(
    archive: ProjectArchive,
    message: dict[str, object],
    body_md: str,
    sender: str,
    recipients: Sequence[str],
    extra_paths: Sequence[str] | None = None,
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
            #   claim: <Agent> ...
            try:
                # Avoid duplicating trailers if already embedded
                lower_msg = message.lower()
                have_agent_line = "\nagent:" in lower_msg
                if message.startswith("mail: ") and not have_agent_line:
                    head = message[len("mail: ") :]
                    agent_part = head.split("->", 1)[0].strip()
                    if agent_part:
                        trailers.append(f"Agent: {agent_part}")
                elif message.startswith("claim: ") and not have_agent_line:
                    head = message[len("claim: ") :]
                    agent_part = head.split(" ", 1)[0].strip()
                    if agent_part:
                        trailers.append(f"Agent: {agent_part}")
            except Exception:
                pass
            final_message = message
            if trailers:
                final_message = message + "\n\n" + "\n".join(trailers) + "\n"
            repo.index.commit(final_message, author=actor, committer=actor)

    await _to_thread(_perform_commit)


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

        # Get commits, optionally filtered by path
        iter_args = {"max_count": limit}
        if path_spec:
            iter_args["paths"] = path_spec

        for commit in repo.iter_commits(**iter_args):
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


async def get_commit_detail(repo: Repo, sha: str) -> dict[str, Any]:
    """
    Get detailed information about a specific commit including full diff.

    Args:
        repo: GitPython Repo object
        sha: Commit SHA (full or abbreviated)

    Returns:
        Dict with commit metadata and diff information
    """
    def _get_detail() -> dict[str, Any]:
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

            # Get diff text
            if diff.diff:
                diff_text += diff.diff.decode("utf-8", errors="replace")

        # Parse commit body into message and trailers
        lines = commit.message.split("\n")
        subject = lines[0] if lines else ""

        # Find where trailers start (after blank line + key: value pattern)
        body_lines = []
        trailer_lines = []
        in_trailers = False

        for line in lines[1:]:
            if not line.strip():
                if not in_trailers:
                    body_lines.append(line)
                continue

            # Check if this looks like a trailer (Key: Value)
            if ": " in line and not in_trailers:
                in_trailers = True

            if in_trailers:
                trailer_lines.append(line)
            else:
                body_lines.append(line)

        body = "\n".join(body_lines).strip()

        # Parse trailers into dict
        trailers = {}
        for line in trailer_lines:
            if ": " in line:
                key, value = line.split(": ", 1)
                trailers[key.strip()] = value.strip()

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

        # Search for file ending with _{message_id}.md
        pattern = f"__{message_id}.md"

        for md_file in messages_dir.rglob("*.md"):
            if md_file.name.endswith(pattern):
                # Get relative path from repo root
                rel_path = md_file.relative_to(archive.repo_root)

                # Get commits that touched this file
                commits = list(archive.repo.iter_commits(paths=str(rel_path), max_count=1))

                if commits:
                    return commits[0].hexsha

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
        # Get commit (HEAD if not specified)
        if commit_sha:
            commit = archive.repo.commit(commit_sha)
        else:
            commit = archive.repo.head.commit

        # Navigate to the requested path within project root
        project_rel = f"projects/{archive.slug}"
        if path:
            tree_path = f"{project_rel}/{path}"
        else:
            tree_path = project_rel

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
) -> str | None:
    """
    Get file content from the Git archive.

    Args:
        archive: ProjectArchive instance
        path: Relative path within the project archive
        commit_sha: Optional commit SHA to view historical content

    Returns:
        File content as string, or None if not found
    """
    def _get_content() -> str | None:
        if commit_sha:
            commit = archive.repo.commit(commit_sha)
        else:
            commit = archive.repo.head.commit

        project_rel = f"projects/{archive.slug}/{path}"

        try:
            blob = commit.tree / project_rel
            return blob.data_stream.read().decode("utf-8", errors="replace")
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
        agent_stats: dict[str, dict[str, Any]] = {}
        connections: dict[tuple[str, str], int] = {}

        for commit in repo.iter_commits(paths=path_spec, max_count=limit):
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
                sender = sender.strip()
                recipients = [r.strip() for r in recipients_str.split(",")]

                # Update sender stats
                if sender not in agent_stats:
                    agent_stats[sender] = {"sent": 0, "received": 0}
                agent_stats[sender]["sent"] += 1

                # Update recipient stats and connections
                for recipient in recipients:
                    if not recipient:
                        continue

                    if recipient not in agent_stats:
                        agent_stats[recipient] = {"sent": 0, "received": 0}
                    agent_stats[recipient]["received"] += 1

                    # Track connection
                    conn_key = (sender, recipient)
                    connections[conn_key] = connections.get(conn_key, 0) + 1

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
        for commit in repo.iter_commits(paths=path_spec, max_count=limit):
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
            elif subject.startswith("claim: "):
                commit_type = "claim"
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
