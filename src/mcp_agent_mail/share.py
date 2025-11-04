"""Utilities for exporting MCP Agent Mail data into shareable static bundles."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence
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
    pseudonym_salt: str
    agents_total: int
    agents_pseudonymized: int
    ack_flags_cleared: int
    recipients_cleared: int
    file_reservations_removed: int
    agent_links_removed: int
    secrets_replaced: int
    attachments_sanitized: int


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


def scrub_snapshot(snapshot_path: Path, *, export_salt: Optional[bytes] = None) -> ScrubSummary:
    """Apply in-place redactions to the snapshot and return a summary."""

    salt = export_salt or secrets.token_bytes(32)
    pseudonym_salt = base64.urlsafe_b64encode(salt).decode("ascii")

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
                sanitized, rep_count, removed_count = _scrub_structure(attachments_data)
                attachment_replacements += rep_count
                attachment_keys_removed += removed_count
                if sanitized != attachments_data:
                    attachments_updated = True
                    sanitized_json = json.dumps(sanitized, separators=(",", ":"), sort_keys=True)
                    conn.execute(
                        "UPDATE messages SET attachments = ? WHERE id = ?",
                        (sanitized_json, msg["id"]),
                    )
            if subject != msg["subject"]:
                conn.execute("UPDATE messages SET subject = ? WHERE id = ?", (subject, msg["id"]))
            if body != msg["body_md"]:
                conn.execute("UPDATE messages SET body_md = ? WHERE id = ?", (body, msg["id"]))
            secrets_replaced += attachment_replacements
            if attachments_updated or attachment_replacements or attachment_keys_removed:
                attachments_sanitized += 1

        conn.commit()

    return ScrubSummary(
        pseudonym_salt=pseudonym_salt,
        agents_total=agents_total,
        agents_pseudonymized=pseudonym_count,
        ack_flags_cleared=ack_flags_cleared,
        recipients_cleared=recipients_cleared,
        file_reservations_removed=file_res_removed,
        agent_links_removed=agent_links_removed,
        secrets_replaced=secrets_replaced,
        attachments_sanitized=attachments_sanitized,
    )


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
        },
        "project_scope": {
            "requested": list(project_filters),
            "included": project_entries,
            "removed_count": scope.removed_count,
        },
        "scrub": asdict(scrub_summary),
        "attachments": [],
        "notes": [
            "Prototype manifest. Future revisions will embed scrub configuration, viewer asset hashes, and attachment manifests.",
        ],
    }
    _write_json_file(output_dir / "manifest.json", manifest)

    readme_content = (
        "MCP Agent Mail Static Export (Prototype)\n"
        "=======================================\n\n"
        "This bundle currently contains a raw SQLite snapshot (`mailbox.sqlite3`) and a manifest describing the export.\n"
        "Redaction, attachment packaging, and viewer assets will be added in subsequent iterations.\n"
        "Use the CLI `share preview` command (upcoming) or load the database with the static viewer once it is bundled.\n"
    )
    _write_text_file(output_dir / "README.txt", readme_content)

    how_to_deploy = (
        "# HOW_TO_DEPLOY (Prototype)\n\n"
        "1. Host the entire directory on a static file server (e.g., `python -m http.server` for local testing).\n"
        "2. Ensure `mailbox.sqlite3` and `manifest.json` remain alongside future `viewer/` assets.\n"
        "3. When viewer assets are available, copy the generated bundle to your hosting provider (GitHub Pages, Cloudflare Pages, Netlify).\n"
        "4. Review the manifest to confirm included projects and verify the SHA-256 hash before publication.\n"
        "\n"
        "More automated deployment guidance will be generated once the export pipeline emits full viewer packages.\n"
    )
    _write_text_file(output_dir / "HOW_TO_DEPLOY.md", how_to_deploy)


__all__ = [
    "ShareExportError",
    "apply_project_scope",
    "create_sqlite_snapshot",
    "package_directory_as_zip",
    "prepare_output_directory",
    "resolve_sqlite_database_path",
    "scrub_snapshot",
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
