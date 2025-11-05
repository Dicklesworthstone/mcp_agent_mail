"""Utilities for exporting MCP Agent Mail data into shareable static bundles."""

from __future__ import annotations

import base64
import configparser
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from importlib import abc, resources
from pathlib import Path
from typing import Any, Optional, Sequence, cast
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from sqlalchemy.engine import make_url

from .config import get_settings


class ShareExportError(RuntimeError):
    """Raised when share export steps fail."""


SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ghp_[A-Za-z0-9]{36,}", re.IGNORECASE),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}", re.IGNORECASE),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}", re.IGNORECASE),
    re.compile(r"sk-[A-Za-z0-9]{20,}", re.IGNORECASE),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{16,}"),
    re.compile(r"eyJ[0-9A-Za-z_-]+\.[0-9A-Za-z_-]+\.[0-9A-Za-z_-]+"),  # JWT tokens
)

ATTACHMENT_REDACT_KEYS: frozenset[str] = frozenset(
    {
        "download_url",
        "headers",
        "authorization",
        "signed_url",
        "bearer_token",
    }
)

PSEUDONYM_PREFIX = "agent-"
PSEUDONYM_LENGTH = 12
INLINE_ATTACHMENT_THRESHOLD = 64 * 1024  # 64 KiB
DETACH_ATTACHMENT_THRESHOLD = 25 * 1024 * 1024  # 25 MiB
DEFAULT_CHUNK_THRESHOLD = 20 * 1024 * 1024  # 20 MiB
DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB


@dataclass(slots=True, frozen=True)
class ProjectRecord:
    id: int
    slug: str
    human_key: str


@dataclass(slots=True, frozen=True)
class ProjectScopeResult:
    projects: list[ProjectRecord]
    removed_count: int


@dataclass(slots=True, frozen=True)
class ScrubSummary:
    preset: str
    pseudonym_salt: str
    agents_total: int
    agents_pseudonymized: int
    ack_flags_cleared: int
    recipients_cleared: int
    file_reservations_removed: int
    agent_links_removed: int
    secrets_replaced: int
    attachments_sanitized: int
    bodies_redacted: int
    attachments_cleared: int


@dataclass(slots=True, frozen=True)
class HostingHint:
    key: str
    title: str
    summary: str
    instructions: list[str]
    signals: list[str]


SCRUB_PRESETS: dict[str, dict[str, Any]] = {
    "standard": {
        "description": "Default redaction: pseudonymise agents, clear ack/read state, scrub common secrets; retain message bodies and attachments.",
        "redact_body": False,
        "body_placeholder": None,
        "drop_attachments": False,
    },
    "strict": {
        "description": "High-scrub: replace message bodies with placeholders and omit all attachments from the snapshot.",
        "redact_body": True,
        "body_placeholder": "[Message body redacted]",
        "drop_attachments": True,
    },
}


HOSTING_GUIDES: dict[str, dict[str, object]] = {
    "github_pages": {
        "title": "GitHub Pages",
        "summary": "Deploy the bundle via docs/ or gh-pages branch with correct MIME types.",
        "instructions": [
            "Copy `viewer/`, `manifest.json`, and `mailbox.sqlite3` into your `docs/` folder or gh-pages branch.",
            "Add a `.nojekyll` file so `.wasm` assets are served, and ensure `.wasm` is mapped to `application/wasm` (via `static.json` or repository settings).",
            "Commit and push, then confirm GitHub Pages is enabled for the repository branch."
        ],
    },
    "cloudflare_pages": {
        "title": "Cloudflare Pages",
        "summary": "Deploy with wrangler or Pages UI and enable COOP/COEP headers for sqlite-wasm.",
        "instructions": [
            "Ensure `wrangler.toml` references the bundle directory (or upload the ZIP directly via the dashboard).",
            "Add headers: `Cross-Origin-Opener-Policy: same-origin` and `Cross-Origin-Embedder-Policy: require-corp` to unlock sqlite-wasm fast-path.",
            "For attachments >25 MiB, push them to R2 and reference the signed URLs in the manifest."
        ],
    },
    "netlify": {
        "title": "Netlify",
        "summary": "Use Netlify Drop or git deployment with matching COOP/COEP headers.",
        "instructions": [
            "Add or update `netlify.toml` with custom headers for COOP/COEP (apply to `/*`).",
            "Deploy the bundle directory (or ZIP) via CLI or the Netlify UI.",
            "Verify `.wasm` assets are served with `application/wasm` using Netlify's response headers tooling."
        ],
    },
    "s3": {
        "title": "Amazon S3 / Generic S3-Compatible",
        "summary": "Upload the bundle to a bucket with proper Content-Types or front with CloudFront.",
        "instructions": [
            "Upload the bundle directory to your bucket (e.g., via `aws s3 sync`).",
            "Set `Content-Type` metadata: `.wasm` → `application/wasm`, SQLite files → `application/octet-stream`.",
            "When fronted by CloudFront, configure response headers for COOP/COEP and caching policies."
        ],
    },
}

GENERIC_HOSTING_NOTES: list[str] = [
    "Serve the directory via any static host that honours `Content-Type` metadata (e.g., nginx, Vercel static, Firebase Hosting).",
    "Ensure `.wasm` files return `application/wasm` and SQLite databases return `application/octet-stream` or `application/vnd.sqlite3`.",
    "When sqlite-wasm cannot run (missing COOP/COEP), the viewer will fall back to streaming mode; document the expected performance for your release.",
]


def _find_repo_root(start: Path) -> Optional[Path]:
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _read_git_remotes(repo_root: Path) -> list[str]:
    config_path = repo_root / ".git" / "config"
    if not config_path.exists():
        return []
    parser = configparser.ConfigParser()
    try:
        parser.read(config_path)
    except Exception:
        return []
    urls: list[str] = []
    for section in parser.sections():
        if section.startswith("remote"):
            url = parser[section].get("url")
            if url:
                urls.append(url)
    return urls


def detect_hosting_hints(output_dir: Path) -> list[HostingHint]:
    signals: dict[str, list[str]] = defaultdict(list)
    repo_root = _find_repo_root(Path.cwd())
    remote_urls: list[str] = []
    if repo_root:
        remote_urls = _read_git_remotes(repo_root)
        workflows_dir = repo_root / ".github" / "workflows"
        if workflows_dir.exists():
            for workflow in workflows_dir.glob("*.yml"):
                text = workflow.read_text(encoding="utf-8", errors="ignore")
                if "github-pages" in text or "pages" in workflow.name.lower():
                    signals["github_pages"].append(f"Workflow {workflow.name} references Pages")
                    break
        if (repo_root / "wrangler.toml").exists():
            signals["cloudflare_pages"].append("Found wrangler.toml")
        if (repo_root / "netlify.toml").exists():
            signals["netlify"].append("Found netlify.toml")
        if (repo_root / "deploy" / "s3").exists() or (repo_root / "deploy" / "aws").exists():
            signals["s3"].append("Detected deploy scripts referencing S3/AWS")

    for url in remote_urls:
        lower = url.lower()
        if "github.com" in lower:
            signals["github_pages"].append(f"Git remote: {url}")
        if "cloudflare" in lower:
            signals["cloudflare_pages"].append(f"Git remote: {url}")
        if "netlify" in lower:
            signals["netlify"].append(f"Git remote: {url}")
        if "amazonaws" in lower or "s3" in lower:
            signals["s3"].append(f"Git remote: {url}")

    env = os.environ
    if env.get("GITHUB_REPOSITORY"):
        signals["github_pages"].append("GITHUB_REPOSITORY env set")
    if env.get("CF_PAGES") or env.get("CF_ACCOUNT_ID"):
        signals["cloudflare_pages"].append("Cloudflare Pages environment variables detected")
    if env.get("NETLIFY") or env.get("NETLIFY_SITE_ID"):
        signals["netlify"].append("Netlify environment variables detected")
    if env.get("AWS_S3_BUCKET") or env.get("AWS_BUCKET"):
        signals["s3"].append("AWS S3 bucket environment detected")

    if repo_root:
        docs_dir = repo_root / "docs"
        if docs_dir.exists():
            try:
                if output_dir.is_relative_to(docs_dir):
                    signals["github_pages"].append("Export path inside docs/ directory")
            except AttributeError:
                try:
                    output_dir.relative_to(docs_dir)
                    signals["github_pages"].append("Export path inside docs/ directory")
                except ValueError:
                    pass
            except ValueError:
                pass

    hints: list[HostingHint] = []
    for key, evidence in signals.items():
        guide = HOSTING_GUIDES.get(key)
        if not guide:
            continue
        instructions = cast(list[str], guide["instructions"])
        hints.append(
            HostingHint(
                key=key,
                title=str(guide["title"]),
                summary=str(guide["summary"]),
                instructions=list(instructions),
                signals=evidence,
            )
        )

    preferred_order = ["github_pages", "cloudflare_pages", "netlify", "s3"]
    hints.sort(key=lambda hint: preferred_order.index(hint.key) if hint.key in preferred_order else len(preferred_order))
    return hints


def build_how_to_deploy(hosting_hints: Sequence[HostingHint]) -> str:
    sections: list[str] = []
    sections.append("# HOW_TO_DEPLOY\n")
    sections.append("## Quick Local Preview\n")
    sections.append("1. Run `uv run python -m mcp_agent_mail.cli share preview ./` from this bundle directory.")
    sections.append("2. Open the printed URL (default `http://127.0.0.1:9000/`).")
    sections.append("3. Press Ctrl+C to stop the preview server when finished.\n")

    if hosting_hints:
        sections.append("## Detected Hosting Targets\n")
        for hint in hosting_hints:
            signals_text = "; ".join(hint.signals)
            sections.append(f"- **{hint.title}**: {hint.summary} _(signals: {signals_text})_")
        sections.append("")
    else:
        sections.append("## Detected Hosting Targets\n- No specific hosts detected. Review the guides below.\n")

    used_keys = {hint.key for hint in hosting_hints}
    ordered_keys = [hint.key for hint in hosting_hints] + [key for key in HOSTING_GUIDES if key not in used_keys]
    for key in ordered_keys:
        guide = HOSTING_GUIDES[key]
        detected_flag = " (detected)" if key in used_keys else " (guide)"
        sections.append(f"## {guide['title']}{detected_flag}\n")
        for step in cast(list[str], guide["instructions"]):
            sections.append(f"- {step}")
        sections.append("")

    sections.append("## Generic Static Hosts\n")
    for note in GENERIC_HOSTING_NOTES:
        sections.append(f"- {note}")
    sections.append("")
    sections.append("Review `manifest.json` before publication to confirm the included projects, hashing, and scrubbing policies.")

    return "\n".join(sections)


def export_viewer_data(snapshot_path: Path, output_dir: Path, *, limit: int = 500) -> dict[str, Any]:
    viewer_data_dir = output_dir / "viewer" / "data"
    viewer_data_dir.mkdir(parents=True, exist_ok=True)

    messages: list[dict[str, Any]] = []
    total_messages = 0

    with sqlite3.connect(str(snapshot_path)) as conn:
        conn.row_factory = sqlite3.Row
        total_messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        rows = conn.execute(
            "SELECT id, subject, body_md, created_ts, importance, project_id FROM messages ORDER BY created_ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        for row in rows:
            body = row["body_md"] or ""
            snippet = body.strip().replace("\n", " ")[:280]
            messages.append(
                {
                    "id": row["id"],
                    "subject": row["subject"],
                    "created_ts": row["created_ts"],
                    "importance": row["importance"],
                    "project_id": row["project_id"],
                    "snippet": snippet,
                }
            )

    messages_path = viewer_data_dir / "messages.json"
    messages_path.write_text(json.dumps(messages, indent=2), encoding="utf-8")

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "message_count": total_messages,
        "messages_cached": len(messages),
    }
    meta_path = viewer_data_dir / "meta.json"
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return {"messages": str(messages_path.relative_to(output_dir)), "meta": str(meta_path.relative_to(output_dir))}


def sign_manifest(manifest_path: Path, signing_key_path: Path, output_path: Path, *, public_out: Optional[Path] = None) -> dict[str, str]:
    try:
        from nacl.signing import SigningKey  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise ShareExportError(
            "PyNaCl is required for Ed25519 signing. Install it with `uv add PyNaCl`."
        ) from exc

    manifest_bytes = manifest_path.read_bytes()
    key_raw = signing_key_path.read_bytes()
    if len(key_raw) not in (32, 64):
        raise ShareExportError("Signing key must be 32-byte seed or 64-byte expanded Ed25519 key.")
    signing_key = SigningKey(key_raw[:32])
    signature = signing_key.sign(manifest_bytes).signature
    public_key = signing_key.verify_key.encode()

    payload = {
        "algorithm": "ed25519",
        "signature": base64.b64encode(signature).decode("ascii"),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "public_key": base64.b64encode(public_key).decode("ascii"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    sig_path = output_path / "manifest.sig.json"
    _write_json_file(sig_path, payload)

    if public_out is not None:
        public_out.write_text(base64.b64encode(public_key).decode("ascii"), encoding="utf-8")

    return payload


def encrypt_bundle(bundle_path: Path, recipients: Sequence[str]) -> Optional[Path]:
    if not recipients:
        return None
    age_exe = shutil.which("age")
    if not age_exe:
        raise ShareExportError("`age` CLI not found in PATH. Install age to enable bundle encryption.")

    encrypted_path = bundle_path.with_suffix(bundle_path.suffix + ".age")
    cmd = [age_exe]
    for recipient in recipients:
        cmd.extend(["-r", recipient])
    cmd.extend(["-o", str(encrypted_path), str(bundle_path)])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ShareExportError(f"age encryption failed: {result.stderr.strip()}")
    return encrypted_path


def resolve_sqlite_database_path(database_url: Optional[str] = None) -> Path:
    """Return the resolved filesystem path to the SQLite database.

    Parameters
    ----------
    database_url:
        Optional explicit database URL. When omitted, the value is loaded from settings.

    Returns
    -------
    Path
        Absolute path to the SQLite database file.

    Raises
    ------
    ShareExportError
        If the configured database is not SQLite or the path cannot be resolved.
    """
    settings = get_settings()
    url = make_url(database_url or settings.database.url)
    if not url.get_backend_name().startswith("sqlite"):
        raise ShareExportError(
            f"Static mailbox export currently supports SQLite only (got backend '{url.get_backend_name()}')."
        )
    database_path = url.database
    if not database_path:
        raise ShareExportError("SQLite database path is empty; cannot resolve file on disk.")
    path = Path(database_path).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def create_sqlite_snapshot(source: Path, destination: Path, *, checkpoint: bool = True) -> Path:
    """Materialize a consistent single-file snapshot from a WAL-enabled SQLite database.

    Parameters
    ----------
    source:
        Path to the original SQLite database (journal mode may be WAL).
    destination:
        Path where the compact snapshot should be written. Parent directories are created automatically.
    checkpoint:
        When True, issue a passive WAL checkpoint before copying to minimise pending frames.

    Returns
    -------
    Path
        The path to the snapshot file.
    """
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)

    # Never delete an existing snapshot automatically; require caller to choose a new path.
    if destination.exists():
        raise ShareExportError(
            f"Destination snapshot already exists at {destination}. Choose a new path or remove it manually."
        )

    with sqlite3.connect(str(source)) as source_conn:
        if checkpoint:
            try:
                source_conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
            except sqlite3.Error as exc:  # pragma: no cover - defensive
                raise ShareExportError(f"Failed to run WAL checkpoint: {exc}") from exc
        try:
            with sqlite3.connect(str(destination)) as dest_conn:
                source_conn.backup(dest_conn)
        except sqlite3.Error as exc:
            raise ShareExportError(f"Failed to create SQLite snapshot: {exc}") from exc
    return destination


def _format_in_clause(count: int) -> str:
    return ",".join("?" for _ in range(count))


def apply_project_scope(snapshot_path: Path, identifiers: Sequence[str]) -> ProjectScopeResult:
    """Restrict the snapshot to the requested projects and return retained records."""

    with sqlite3.connect(str(snapshot_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")

        rows = conn.execute("SELECT id, slug, human_key FROM projects").fetchall()
        if not rows:
            raise ShareExportError("Snapshot does not contain any projects to export.")

        projects = [ProjectRecord(int(row["id"]), row["slug"], row["human_key"]) for row in rows]

        if not identifiers:
            return ProjectScopeResult(projects=projects, removed_count=0)

        lookup: dict[str, ProjectRecord] = {}
        for record in projects:
            lookup[record.slug.lower()] = record
            lookup[record.human_key.lower()] = record

        selected: list[ProjectRecord] = []
        for identifier in identifiers:
            key = identifier.strip().lower()
            if not key:
                continue
            record = lookup.get(key)
            if record is None:
                raise ShareExportError(f"Project identifier '{identifier}' not found in snapshot.")
            if record not in selected:
                selected.append(record)

        if not selected:
            raise ShareExportError("No matching projects found for provided filters.")

        allowed_ids = [record.id for record in selected]
        disallowed_ids = [record.id for record in projects if record.id not in allowed_ids]
        if not disallowed_ids:
            return ProjectScopeResult(projects=selected, removed_count=0)

        placeholders = _format_in_clause(len(allowed_ids))
        params = tuple(allowed_ids)

        # Remove dependent records referencing disallowed projects.
        # First handle relationship tables that reference projects in multiple columns.
        conn.execute(
            f"DELETE FROM agent_links WHERE a_project_id NOT IN ({placeholders}) OR b_project_id NOT IN ({placeholders})",
            params + params,
        )
        conn.execute(
            f"DELETE FROM project_sibling_suggestions WHERE project_a_id NOT IN ({placeholders}) OR project_b_id NOT IN ({placeholders})",
            params + params,
        )

        # Collect message ids slated for removal to clean recipient table explicitly.
        to_remove_messages = conn.execute(
            f"SELECT id FROM messages WHERE project_id NOT IN ({placeholders})",
            params,
        ).fetchall()
        if to_remove_messages:
            msg_placeholders = _format_in_clause(len(to_remove_messages))
            conn.execute(
                f"DELETE FROM message_recipients WHERE message_id IN ({msg_placeholders})",
                tuple(int(row["id"]) for row in to_remove_messages),
            )

        conn.execute(
            f"DELETE FROM messages WHERE project_id NOT IN ({placeholders})",
            params,
        )
        conn.execute(
            f"DELETE FROM file_reservations WHERE project_id NOT IN ({placeholders})",
            params,
        )
        conn.execute(
            f"DELETE FROM agents WHERE project_id NOT IN ({placeholders})",
            params,
        )
        conn.execute(
            f"DELETE FROM projects WHERE id NOT IN ({placeholders})",
            params,
        )

        conn.commit()

        return ProjectScopeResult(projects=selected, removed_count=len(disallowed_ids))


def _scrub_text(value: str) -> tuple[str, int]:
    replacements = 0
    updated = value
    for pattern in SECRET_PATTERNS:
        updated, count = pattern.subn("[REDACTED]", updated)
        replacements += count
    return updated, replacements


def _normalize_scrub_preset(preset: str) -> str:
    key = (preset or "standard").strip().lower()
    if key not in SCRUB_PRESETS:
        raise ShareExportError(
            f"Unknown scrub preset '{preset}'. Supported presets: {', '.join(SCRUB_PRESETS)}"
        )
    return key


def _scrub_structure(value: Any) -> tuple[Any, int, int]:
    """Recursively scrub secrets from attachment metadata structures.

    Returns the sanitized value, number of secret replacements, and keys removed.
    """

    if isinstance(value, str):
        new_value, replacements = _scrub_text(value)
        return new_value, replacements, 0
    if isinstance(value, list):
        total_replacements = 0
        total_removed = 0
        sanitized_list = []
        for item in value:
            sanitized_item, item_replacements, item_removed = _scrub_structure(item)
            sanitized_list.append(sanitized_item)
            total_replacements += item_replacements
            total_removed += item_removed
        return sanitized_list, total_replacements, total_removed
    if isinstance(value, dict):
        total_replacements = 0
        total_removed = 0
        sanitized_dict: dict[str, Any] = {}
        for key, item in value.items():
            if key in ATTACHMENT_REDACT_KEYS:
                if item not in (None, "", [], {}):
                    total_removed += 1
                continue
            sanitized_item, item_replacements, item_removed = _scrub_structure(item)
            sanitized_dict[key] = sanitized_item
            total_replacements += item_replacements
            total_removed += item_removed
        return sanitized_dict, total_replacements, total_removed
    return value, 0, 0


def scrub_snapshot(
    snapshot_path: Path,
    *,
    preset: str = "standard",
    export_salt: Optional[bytes] = None,
) -> ScrubSummary:
    """Apply in-place redactions to the snapshot and return a summary."""

    preset_key = _normalize_scrub_preset(preset)
    preset_opts = SCRUB_PRESETS[preset_key]

    salt = export_salt or secrets.token_bytes(32)
    pseudonym_salt = base64.urlsafe_b64encode(salt).decode("ascii")

    bodies_redacted = 0
    attachments_cleared = 0

    with sqlite3.connect(str(snapshot_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")

        agents = conn.execute("SELECT id, name FROM agents ORDER BY id").fetchall()
        agents_total = len(agents)
        pseudonym_count = 0
        used_aliases: set[str] = set()

        for row in agents:
            raw_name = row["name"]
            digest = hmac.new(salt, raw_name.encode("utf-8"), hashlib.sha256).hexdigest()
            alias_core = digest[:PSEUDONYM_LENGTH]
            alias = f"{PSEUDONYM_PREFIX}{alias_core}"
            extra_index = PSEUDONYM_LENGTH
            while alias in used_aliases:
                alias_core = digest[:extra_index]
                alias = f"{PSEUDONYM_PREFIX}{alias_core}"
                extra_index += 1
                if extra_index > len(digest):
                    alias = f"{PSEUDONYM_PREFIX}{alias_core}{secrets.token_hex(2)}"
                    break
            used_aliases.add(alias)
            if alias != raw_name:
                pseudonym_count += 1
            conn.execute(
                "UPDATE agents SET name = ?, contact_policy = 'redacted' WHERE id = ?",
                (alias, row["id"]),
            )

        ack_cursor = conn.execute("UPDATE messages SET ack_required = 0")
        ack_flags_cleared = ack_cursor.rowcount or 0

        recipients_cursor = conn.execute("UPDATE message_recipients SET read_ts = NULL, ack_ts = NULL")
        recipients_cleared = recipients_cursor.rowcount or 0

        file_res_cursor = conn.execute("DELETE FROM file_reservations")
        file_res_removed = file_res_cursor.rowcount or 0

        agent_links_cursor = conn.execute("DELETE FROM agent_links")
        agent_links_removed = agent_links_cursor.rowcount or 0

        secrets_replaced = 0
        attachments_sanitized = 0

        message_rows = conn.execute("SELECT id, subject, body_md, attachments FROM messages").fetchall()
        for msg in message_rows:
            subject, subj_replacements = _scrub_text(msg["subject"])
            body, body_replacements = _scrub_text(msg["body_md"])
            secrets_replaced += subj_replacements + body_replacements
            attachments_value = msg["attachments"]
            attachments_updated = False
            attachment_replacements = 0
            attachment_keys_removed = 0
            if attachments_value:
                if isinstance(attachments_value, str):
                    try:
                        attachments_data = json.loads(attachments_value)
                    except json.JSONDecodeError:
                        attachments_data = attachments_value
                else:
                    attachments_data = attachments_value
                if preset_opts["drop_attachments"] and attachments_data:
                    attachments_data = []
                    attachments_cleared += 1
                    attachments_updated = True
                sanitized, rep_count, removed_count = _scrub_structure(attachments_data)
                attachment_replacements += rep_count
                attachment_keys_removed += removed_count
                if sanitized != attachments_data:
                    attachments_data = sanitized
                    attachments_updated = True
                if attachments_updated:
                    sanitized_json = json.dumps(attachments_data, separators=(",", ":"), sort_keys=True)
                    conn.execute(
                        "UPDATE messages SET attachments = ? WHERE id = ?",
                        (sanitized_json, msg["id"]),
                    )
            if subject != msg["subject"]:
                conn.execute("UPDATE messages SET subject = ? WHERE id = ?", (subject, msg["id"]))
            if preset_opts["redact_body"]:
                body = preset_opts.get("body_placeholder") or "[Message body redacted]"
                if msg["body_md"] != body:
                    bodies_redacted += 1
                    conn.execute("UPDATE messages SET body_md = ? WHERE id = ?", (body, msg["id"]))
            elif body != msg["body_md"]:
                conn.execute("UPDATE messages SET body_md = ? WHERE id = ?", (body, msg["id"]))
            secrets_replaced += attachment_replacements
            if attachments_updated or attachment_replacements or attachment_keys_removed:
                attachments_sanitized += 1

        conn.commit()

    return ScrubSummary(
        preset=preset_key,
        pseudonym_salt=pseudonym_salt,
        agents_total=agents_total,
        agents_pseudonymized=pseudonym_count,
        ack_flags_cleared=ack_flags_cleared,
        recipients_cleared=recipients_cleared,
        file_reservations_removed=file_res_removed,
        agent_links_removed=agent_links_removed,
        secrets_replaced=secrets_replaced,
        attachments_sanitized=attachments_sanitized,
        bodies_redacted=bodies_redacted,
        attachments_cleared=attachments_cleared,
    )


def bundle_attachments(
    snapshot_path: Path,
    output_dir: Path,
    *,
    storage_root: Path,
    inline_threshold: int = INLINE_ATTACHMENT_THRESHOLD,
    detach_threshold: int = DETACH_ATTACHMENT_THRESHOLD,
) -> dict[str, Any]:
    """Materialize attachment assets referenced by the snapshot into the bundle."""

    storage_root = storage_root.resolve()
    attachments_dir = output_dir / "attachments"
    attachments_dir.mkdir(parents=True, exist_ok=True)
    bundles: dict[str, Path] = {}
    manifest_items: list[dict[str, Any]] = []
    inline_count = 0
    copied_count = 0
    externalized_count = 0
    missing_count = 0
    bytes_copied = 0

    with sqlite3.connect(str(snapshot_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, attachments FROM messages").fetchall()
        for row in rows:
            raw_attachments = row["attachments"]
            if not raw_attachments:
                continue
            if isinstance(raw_attachments, str):
                try:
                    attachments_list = json.loads(raw_attachments)
                except json.JSONDecodeError:
                    attachments_list = []
            else:
                attachments_list = raw_attachments
            if not isinstance(attachments_list, list):
                continue
            updated_list: list[Any] = []
            changed = False
            for entry in attachments_list:
                if not isinstance(entry, dict):
                    updated_list.append(entry)
                    continue
                entry_type = entry.get("type")
                if entry_type != "file":
                    updated_list.append(entry)
                    continue
                original_path = entry.get("path")
                media_type = entry.get("media_type", "application/octet-stream")
                sha_hint = entry.get("sha256") or entry.get("sha1")
                if not original_path:
                    updated_list.append(entry)
                    continue
                source_path = Path(original_path)
                if not source_path.is_absolute():
                    source_path = (storage_root / original_path).resolve()
                if not source_path.is_file():
                    missing_count += 1
                    manifest_items.append(
                        {
                            "message_id": int(row["id"]),
                            "mode": "missing",
                            "original_path": original_path,
                            "sha_hint": sha_hint,
                            "media_type": media_type,
                        }
                    )
                    updated_list.append(
                        {
                            "type": "missing",
                            "original_path": original_path,
                            "media_type": media_type,
                            "sha_hint": sha_hint,
                        }
                    )
                    changed = True
                    continue

                data = source_path.read_bytes()
                size = len(data)
                sha256 = hashlib.sha256(data).hexdigest()
                ext = source_path.suffix or ".bin"
                media_record = {
                    "message_id": int(row["id"]),
                    "sha256": sha256,
                    "media_type": media_type,
                    "original_path": original_path,
                    "bytes": size,
                }

                if size <= inline_threshold:
                    encoded = base64.b64encode(data).decode("ascii")
                    updated_list.append(
                        {
                            "type": "inline",
                            "media_type": media_type,
                            "bytes": size,
                            "sha256": sha256,
                            "data_uri": f"data:{media_type};base64,{encoded}",
                        }
                    )
                    media_record["mode"] = "inline"
                    manifest_items.append(media_record)
                    inline_count += 1
                    changed = True
                    continue

                if size >= detach_threshold:
                    media_record["mode"] = "external"
                    media_record["note"] = "Attachment exceeds detach threshold; not bundled."
                    manifest_items.append(media_record)
                    updated_list.append(
                        {
                            "type": "external",
                            "media_type": media_type,
                            "bytes": size,
                            "sha256": sha256,
                            "original_path": original_path,
                            "note": "Requires manual hosting (exceeds bundle threshold).",
                        }
                    )
                    externalized_count += 1
                    changed = True
                    continue

                rel_path = bundles.get(sha256)
                if rel_path is None:
                    rel_path = Path("attachments") / sha256[:2] / f"{sha256}{ext}"
                    dest_path = output_dir / rel_path
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    if not dest_path.exists():
                        dest_path.write_bytes(data)
                        bytes_copied += size
                    bundles[sha256] = rel_path
                media_record["mode"] = "file"
                media_record["bundle_path"] = rel_path.as_posix()
                manifest_items.append(media_record)
                updated_list.append(
                    {
                        "type": "file",
                        "media_type": media_type,
                        "bytes": size,
                        "sha256": sha256,
                        "path": rel_path.as_posix(),
                    }
                )
                copied_count += 1
                if sha_hint and sha_hint != sha256:
                    media_record["sha_hint"] = sha_hint
                changed = True
            if changed:
                conn.execute(
                    "UPDATE messages SET attachments = ? WHERE id = ?",
                    (json.dumps(updated_list, separators=(",", ":"), sort_keys=True), row["id"]),
                )
        conn.commit()

    return {
        "stats": {
            "inline": inline_count,
            "copied": copied_count,
            "externalized": externalized_count,
            "missing": missing_count,
            "bytes_copied": bytes_copied,
        },
        "config": {
            "inline_threshold": inline_threshold,
            "detach_threshold": detach_threshold,
        },
        "items": manifest_items,
    }


def maybe_chunk_database(
    snapshot_path: Path,
    output_dir: Path,
    *,
    threshold_bytes: int = DEFAULT_CHUNK_THRESHOLD,
    chunk_bytes: int = DEFAULT_CHUNK_SIZE,
) -> Optional[dict[str, Any]]:
    if chunk_bytes <= 0:
        raise ShareExportError("chunk_bytes must be greater than 0 when chunking the database.")

    size = snapshot_path.stat().st_size
    if size <= threshold_bytes:
        return None

    chunk_dir = output_dir / "chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)

    with snapshot_path.open("rb") as src:
        index = 0
        while True:
            chunk = src.read(chunk_bytes)
            if not chunk:
                break
            chunk_path = chunk_dir / f"{index:05d}.bin"
            chunk_path.write_bytes(chunk)
            index += 1

    config = {
        "version": 1,
        "chunk_size": chunk_bytes,
        "chunk_count": index,
        "pattern": "chunks/{index:05d}.bin",
        "original_bytes": size,
    }
    _write_json_file(output_dir / "mailbox.sqlite3.config.json", config)
    return config


def copy_viewer_assets(output_dir: Path) -> None:
    """Copy the packaged viewer assets into the export output directory."""

    viewer_root = output_dir / "viewer"
    viewer_root.mkdir(parents=True, exist_ok=True)

    package_root = resources.files("mcp_agent_mail.viewer_assets")

    def _walk(node: abc.Traversable, relative: Path) -> None:  # type: ignore[attr-defined]
        for child in node.iterdir():
            child_relative = relative / child.name
            if child.is_dir():
                _walk(child, child_relative)
            else:
                destination = viewer_root / child_relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(child.read_bytes())

    _walk(package_root, Path())


def prepare_output_directory(directory: Path) -> Path:
    """Ensure the export directory exists and is empty before writing bundle artefacts."""
    resolved = directory.resolve()
    if resolved.exists():
        if not resolved.is_dir():
            raise ShareExportError(f"Export path {resolved} exists and is not a directory.")
        if any(resolved.iterdir()):
            raise ShareExportError(f"Export path {resolved} is not empty; choose a new directory.")
    else:
        resolved.mkdir(parents=True, exist_ok=False)
    return resolved


def _write_text_file(path: Path, content: str) -> None:
    """Write UTF-8 text without clobbering existing files."""
    if path.exists():
        raise ShareExportError(f"Refusing to overwrite existing file: {path}")
    path.write_text(content, encoding="utf-8")


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    """Serialize JSON with stable formatting."""
    if path.exists():
        raise ShareExportError(f"Refusing to overwrite existing file: {path}")
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_bundle_scaffolding(
    output_dir: Path,
    *,
    snapshot: Path,
    scope: ProjectScopeResult,
    project_filters: Sequence[str],
    scrub_summary: ScrubSummary,
    attachments_manifest: dict[str, Any],
    chunk_manifest: Optional[dict[str, Any]],
    hosting_hints: Sequence[HostingHint],
    viewer_data: Optional[dict[str, Any]],
    exporter_version: str = "prototype",
) -> None:
    """Create manifest and helper docs around the freshly minted snapshot."""

    project_entries = [
        {"slug": record.slug, "human_key": record.human_key}
        for record in scope.projects
    ]

    manifest = {
        "schema_version": "0.1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "exporter_version": exporter_version,
        "database": {
            "path": snapshot.name,
            "size_bytes": snapshot.stat().st_size,
            "sha256": _compute_sha256(snapshot),
            "chunked": bool(chunk_manifest),
            "chunk_manifest": chunk_manifest,
        },
        "project_scope": {
            "requested": list(project_filters),
            "included": project_entries,
            "removed_count": scope.removed_count,
        },
        "scrub": asdict(scrub_summary),
        "attachments": attachments_manifest,
        "hosting": {
            "detected": [
                {
                    "id": hint.key,
                    "title": hint.title,
                    "summary": hint.summary,
                    "signals": hint.signals,
                }
                for hint in hosting_hints
            ],
        },
        "notes": [
            "Prototype manifest. Future revisions will embed scrub configuration, viewer asset hashes, and attachment manifests.",
            "Viewer scaffold with diagnostics is bundled; SPA search/thread views arrive in upcoming milestones.",
        ],
    }
    if viewer_data:
        manifest["viewer"] = viewer_data
    _write_json_file(output_dir / "manifest.json", manifest)

    readme_content = (
        "MCP Agent Mail Static Export (Prototype)\n"
        "=======================================\n\n"
        "This bundle contains a scrubbed SQLite snapshot (`mailbox.sqlite3`), optional chunk manifest, attachments, and a minimal viewer scaffold (`viewer/`).\n"
        "Run `uv run python -m mcp_agent_mail.cli share preview .` from this directory to launch the local preview, or open `viewer/index.html` after hosting the bundle on a static site as described in `HOW_TO_DEPLOY.md`.\n"
        "Use `manifest.json` to audit included projects, scrub statistics, hosting hints, and attachment packaging details.\n"
    )
    if hosting_hints:
        readme_content += "\nDetected hosting targets in this environment:\n"
        for hint in hosting_hints:
            signals_text = "; ".join(hint.signals)
            readme_content += f"- {hint.title}: {hint.summary} (signals: {signals_text})\n"
    _write_text_file(output_dir / "README.txt", readme_content)

    how_to_deploy = build_how_to_deploy(hosting_hints)
    _write_text_file(output_dir / "HOW_TO_DEPLOY.md", how_to_deploy)


__all__ = [
    "SCRUB_PRESETS",
    "HostingHint",
    "ShareExportError",
    "apply_project_scope",
    "build_how_to_deploy",
    "bundle_attachments",
    "copy_viewer_assets",
    "create_sqlite_snapshot",
    "detect_hosting_hints",
    "encrypt_bundle",
    "export_viewer_data",
    "maybe_chunk_database",
    "package_directory_as_zip",
    "prepare_output_directory",
    "resolve_sqlite_database_path",
    "scrub_snapshot",
    "sign_manifest",
    "write_bundle_scaffolding",
]


def package_directory_as_zip(source_dir: Path, destination: Path) -> Path:
    """Create a deterministic ZIP archive of *source_dir* at *destination*.

    The archive includes regular files only (directories are implied) and records
    POSIX permissions while normalising timestamps for reproducibility.
    """

    source = source_dir.resolve()
    if not source.is_dir():
        raise ShareExportError(f"ZIP source must be a directory (got {source}).")

    dest = destination.resolve()
    if dest.exists():
        raise ShareExportError(f"Cannot overwrite existing archive {dest}; choose a new filename.")

    dest.parent.mkdir(parents=True, exist_ok=True)

    with ZipFile(dest, mode="x", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(p for p in source.rglob("*") if p.is_file()):
            relative = path.relative_to(source)
            zip_path = relative.as_posix()

            info = ZipInfo(zip_path)
            info.compress_type = ZIP_DEFLATED
            info.date_time = (1980, 1, 1, 0, 0, 0)
            mode = path.stat().st_mode & 0o777
            info.external_attr = (mode << 16)

            with path.open("rb") as data, archive.open(info, "w") as zip_file:
                shutil.copyfileobj(data, zip_file, length=1 << 20)

    return dest
