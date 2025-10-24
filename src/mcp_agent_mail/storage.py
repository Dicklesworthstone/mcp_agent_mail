"""Filesystem and Git archive helpers for MCP Agent Mail."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from filelock import FileLock
from git import Actor, Repo
from PIL import Image

from .config import Settings

_IMAGE_PATTERN = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<path>[^)]+)\)")


@dataclass(slots=True)
class ProjectArchive:
    settings: Settings
    slug: str
    root: Path
    repo: Repo
    lock_path: Path

    @property
    def attachments_dir(self) -> Path:
        return self.root / "attachments"


class AsyncFileLock:
    def __init__(self, path: Path) -> None:
        self._lock = FileLock(str(path))

    async def __aenter__(self) -> None:
        await asyncio.to_thread(self._lock.acquire)

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._lock.release()


async def _to_thread(func, /, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


async def ensure_archive(settings: Settings, slug: str) -> ProjectArchive:
    root = Path(settings.storage.root).expanduser().resolve() / slug
    await _to_thread(root.mkdir, parents=True, exist_ok=True)
    repo = await _ensure_repo(root, settings)
    return ProjectArchive(settings=settings, slug=slug, root=root, repo=repo, lock_path=root / ".archive.lock")


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
    await _commit(archive.repo, archive.settings, f"agent: profile {agent['name']}", [profile_path.relative_to(archive.root).as_posix()])


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
        [claim_path.relative_to(archive.root).as_posix()],
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

    filename = f"{message['id']}.md"
    canonical_path = canonical_dir / filename
    await _write_text(canonical_path, content)
    rel_paths.append(canonical_path.relative_to(archive.root).as_posix())

    outbox_path = outbox_dir / filename
    await _write_text(outbox_path, content)
    rel_paths.append(outbox_path.relative_to(archive.root).as_posix())

    for inbox_dir in inbox_dirs:
        inbox_path = inbox_dir / filename
        await _write_text(inbox_path, content)
        rel_paths.append(inbox_path.relative_to(archive.root).as_posix())

    # Update thread-level digest for human review if thread_id present
    thread_id_obj = message.get("thread_id")
    if isinstance(thread_id_obj, str) and thread_id_obj.strip():
        canonical_rel = canonical_path.relative_to(archive.root).as_posix()
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
    commit_subject = f"mail: {sender} -> {', '.join(recipients)} | {message['subject']}"
    commit_message = commit_subject + "\n\n" + f"Agent: {sender}\n" + f"Thread: {thread_key}\n"
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
    return digest_path.relative_to(archive.root).as_posix()


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
        original_rel = orig_path.relative_to(archive.root).as_posix()
    if not target_path.exists():
        await _save_webp(img, target_path)
    new_bytes = await _to_thread(target_path.read_bytes)
    rel_path = target_path.relative_to(archive.root).as_posix()
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
