"""Utilities for exporting MCP Agent Mail data into shareable static bundles."""

from __future__ import annotations

import base64
import binascii
import configparser
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from collections import defaultdict
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, cast
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
INDEX_REDIRECT_HTML = """<!doctype html>
<html lang="en">

<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="refresh" content="0; url=./viewer/" />
  <title>MCP Agent Mail Viewer</title>
  <link rel="canonical" href="./viewer/" />
  <style>
    :root {
      color-scheme: light dark;
    }

    body {
      margin: 0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0f172a;
      color: #f8fafc;
      display: grid;
      place-items: center;
      min-height: 100vh;
    }

    main {
      text-align: center;
      padding: 2.5rem;
      border-radius: 1.25rem;
      background: rgba(15, 23, 42, 0.85);
      box-shadow: 0 25px 50px -12px rgba(15, 23, 42, 0.65);
      max-width: 32rem;
      backdrop-filter: blur(18px);
    }

    h1 {
      margin-bottom: 1rem;
      font-size: clamp(1.75rem, 5vw, 2.5rem);
      font-weight: 700;
      color: #e0e7ff;
    }

    p {
      margin: 0.75rem 0;
      line-height: 1.6;
      color: rgba(226, 232, 240, 0.88);
    }

    a {
      color: #6366f1;
      text-decoration: none;
      font-weight: 600;
    }

    a:hover {
      text-decoration: underline;
    }
  </style>
</head>

<body>
  <main>
    <h1>MCP Agent Mail Viewer</h1>
    <p>You are being redirected to the hosted viewer experience.</p>
    <p>If you are not redirected automatically, <a href="./viewer/">click here to open the viewer</a>.</p>
  </main>
  <script>
    try {
      const target = new URL("./viewer/", window.location.href);
      window.location.replace(target.toString());
    } catch (error) {
      window.location.href = "./viewer/";
    }
  </script>
</body>

</html>
"""


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
        "description": "Default redaction: clear ack/read state, scrub common secrets (API keys, tokens); retain agent names, message bodies and attachments.",
        "redact_body": False,
        "body_placeholder": None,
        "drop_attachments": False,
        "scrub_secrets": True,
        "clear_ack_state": True,
        "clear_recipients": True,
        "clear_file_reservations": True,
        "clear_agent_links": True,
    },
    "strict": {
        "description": "High-scrub: replace message bodies with placeholders and omit all attachments from the snapshot.",
        "redact_body": True,
        "body_placeholder": "[Message body redacted]",
        "drop_attachments": True,
        "scrub_secrets": True,
        "clear_ack_state": True,
        "clear_recipients": True,
        "clear_file_reservations": True,
        "clear_agent_links": True,
    },
    "archive": {
        "description": "Lossless snapshot for disaster recovery: preserve ack/read state, recipients, attachments, and body content while still running the standard cleanup pipeline.",
        "redact_body": False,
        "body_placeholder": None,
        "drop_attachments": False,
        "scrub_secrets": False,
        "clear_ack_state": False,
        "clear_recipients": False,
        "clear_file_reservations": False,
        "clear_agent_links": False,
    },
}


HOSTING_GUIDES: dict[str, dict[str, object]] = {
    "github_pages": {
        "title": "GitHub Pages",
        "summary": "Deploy the bundle via docs/ or gh-pages branch with coi-serviceworker.js for cross-origin isolation.",
        "instructions": [
            "Copy `viewer/`, `manifest.json`, and `mailbox.sqlite3` into your `docs/` folder or gh-pages branch.",
            "Add a `.nojekyll` file so `.wasm` assets are served with correct MIME types.",
            "**CRITICAL**: Edit `viewer/index.html` and uncomment the line `<script src=\"./coi-serviceworker.js\"></script>` (around line 63).",
            "This service worker enables Cross-Origin-Isolation (COOP/COEP headers) required for OPFS caching and optimal sqlite-wasm performance.",
            "GitHub Pages does not support the `_headers` file, so the service worker intercepts requests and adds the required headers.",
            "Commit and push, then enable GitHub Pages for your repository branch in repository settings.",
            "On first visit, the page will reload automatically once the service worker activates (this is normal behavior).",
            "Verify isolation: open browser DevTools console and check that `window.crossOriginIsolated === true`."
        ],
    },
    "cloudflare_pages": {
        "title": "Cloudflare Pages",
        "summary": "Deploy with wrangler or Pages UI; the included _headers file automatically enables COOP/COEP.",
        "instructions": [
            "Ensure `wrangler.toml` references the bundle directory (or upload the ZIP directly via the dashboard).",
            "The included `_headers` file will automatically apply `Cross-Origin-Opener-Policy: same-origin` and `Cross-Origin-Embedder-Policy: require-corp` headers.",
            "These headers are required for OPFS caching and optimal sqlite-wasm performance.",
            "Verify isolation: open browser DevTools console and check that `window.crossOriginIsolated === true`.",
            "For attachments >25 MiB, push them to R2 and reference the signed URLs in the manifest."
        ],
    },
    "netlify": {
        "title": "Netlify",
        "summary": "Use Netlify Drop or git deployment; the included _headers file automatically enables COOP/COEP.",
        "instructions": [
            "The included `_headers` file will automatically apply `Cross-Origin-Opener-Policy: same-origin` and `Cross-Origin-Embedder-Policy: require-corp` headers.",
            "These headers are required for OPFS caching and optimal sqlite-wasm performance.",
            "Deploy the bundle directory (or ZIP) via CLI or the Netlify UI.",
            "Verify isolation: open browser DevTools console and check that `window.crossOriginIsolated === true`.",
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
    "For optimal performance, enable Cross-Origin-Isolation (COOP/COEP headers). The included `_headers` file is automatically applied by Cloudflare Pages and Netlify.",
    "If your host doesn't support `_headers` (e.g., GitHub Pages), uncomment the `coi-serviceworker.js` script in `viewer/index.html` to enable isolation via service worker.",
    "If cross-origin isolation is unavailable, the viewer will show a warning banner with platform-specific instructions and fall back to streaming mode.",
    "Verify isolation: open browser DevTools console and check that `window.crossOriginIsolated === true`.",
]


@dataclass(slots=True, frozen=True)
class SnapshotContext:
    snapshot_path: Path
    scope: ProjectScopeResult
    scrub_summary: ScrubSummary
    fts_enabled: bool


@dataclass(slots=True, frozen=True)
class BundleArtifacts:
    attachments_manifest: dict[str, Any]
    chunk_manifest: Optional[dict[str, Any]]
    viewer_data: Optional[dict[str, Any]]


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


def export_viewer_data(
    snapshot_path: Path,
    output_dir: Path,
    *,
    limit: int = 500,
    fts_enabled: bool = False,
) -> dict[str, Any]:
    viewer_data_dir = output_dir / "viewer" / "data"
    viewer_data_dir.mkdir(parents=True, exist_ok=True)

    messages: list[dict[str, Any]] = []
    total_messages = 0

    conn = sqlite3.connect(str(snapshot_path))
    try:
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
    finally:
        conn.close()

    messages_path = viewer_data_dir / "messages.json"
    messages_path.write_text(json.dumps(messages, indent=2), encoding="utf-8")

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "message_count": total_messages,
        "messages_cached": len(messages),
        "fts_enabled": fts_enabled,
    }
    meta_path = viewer_data_dir / "meta.json"
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return {
        "messages": str(messages_path.relative_to(output_dir)),
        "meta": str(meta_path.relative_to(output_dir)),
        "meta_info": meta,
    }


def sign_manifest(
    manifest_path: Path,
    signing_key_path: Path,
    output_path: Path,
    *,
    public_out: Optional[Path] = None,
    overwrite: bool = False,
) -> dict[str, str]:
    try:
        from nacl.signing import SigningKey
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise ShareExportError(
            "PyNaCl is required for Ed25519 signing. Install it with `uv add PyNaCl`."
        ) from exc

    # Expand and validate manifest path
    manifest_path = manifest_path.expanduser().resolve()
    if not manifest_path.exists():
        raise ShareExportError(f"Manifest file not found: {manifest_path}")
    if not manifest_path.is_file():
        raise ShareExportError(f"Manifest path must be a file: {manifest_path}")

    # Expand and validate signing key path
    signing_key_path = signing_key_path.expanduser().resolve()
    if not signing_key_path.exists():
        raise ShareExportError(f"Signing key file not found: {signing_key_path}")
    if not signing_key_path.is_file():
        raise ShareExportError(f"Signing key path must be a file: {signing_key_path}")

    try:
        manifest_bytes = manifest_path.read_bytes()
    except (IOError, OSError) as exc:
        raise ShareExportError(f"Failed to read manifest file {manifest_path}: {exc}") from exc

    try:
        key_raw = signing_key_path.read_bytes()
    except (IOError, OSError) as exc:
        raise ShareExportError(f"Failed to read signing key file {signing_key_path}: {exc}") from exc

    if len(key_raw) not in (32, 64):
        raise ShareExportError("Signing key must be 32-byte seed or 64-byte expanded Ed25519 key.")

    try:
        signing_key = SigningKey(key_raw[:32])
        signature = signing_key.sign(manifest_bytes).signature
        public_key = signing_key.verify_key.encode()
    except Exception as exc:
        raise ShareExportError(f"Failed to sign manifest with Ed25519 key: {exc}") from exc

    payload = {
        "algorithm": "ed25519",
        "signature": base64.b64encode(signature).decode("ascii"),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "public_key": base64.b64encode(public_key).decode("ascii"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    sig_path = output_path / "manifest.sig.json"
    try:
        if overwrite and sig_path.exists():
            sig_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        else:
            _write_json_file(sig_path, payload)
    except ShareExportError:
        raise  # Re-raise ShareExportError as-is
    except Exception as exc:
        raise ShareExportError(f"Failed to write signature file: {exc}") from exc

    if public_out is not None:
        # Expand and validate public key output path
        public_out = public_out.expanduser().resolve()
        if public_out.exists():
            raise ShareExportError(f"Public key output file already exists: {public_out}")

        # Ensure parent directory exists
        try:
            public_out.parent.mkdir(parents=True, exist_ok=True)
        except (IOError, OSError) as exc:
            raise ShareExportError(f"Failed to create parent directory for public key: {exc}") from exc

        try:
            public_out.write_text(base64.b64encode(public_key).decode("ascii"), encoding="utf-8")
        except (IOError, OSError) as exc:
            raise ShareExportError(f"Failed to write public key to {public_out}: {exc}") from exc

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

    source_conn = sqlite3.connect(str(source))
    try:
        if checkpoint:
            try:
                source_conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
            except sqlite3.Error as exc:  # pragma: no cover - defensive
                raise ShareExportError(f"Failed to run WAL checkpoint: {exc}") from exc
        dest_conn = sqlite3.connect(str(destination))
        try:
            source_conn.backup(dest_conn)
        except sqlite3.Error as exc:
            raise ShareExportError(f"Failed to create SQLite snapshot: {exc}") from exc
        finally:
            dest_conn.close()
    finally:
        source_conn.close()
    return destination


def _format_in_clause(count: int) -> str:
    return ",".join("?" for _ in range(count))


def apply_project_scope(snapshot_path: Path, identifiers: Sequence[str]) -> ProjectScopeResult:
    """Restrict the snapshot to the requested projects and return retained records."""

    conn = sqlite3.connect(str(snapshot_path))
    try:
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
        selected_ids: set[int] = set()  # O(1) membership check instead of O(n) list scan
        for identifier in identifiers:
            key = identifier.strip().lower()
            if not key:
                continue
            found_record = lookup.get(key)
            if found_record is None:
                raise ShareExportError(f"Project identifier '{identifier}' not found in snapshot.")
            if found_record.id not in selected_ids:
                selected_ids.add(found_record.id)
                selected.append(found_record)

        if not selected:
            raise ShareExportError("No matching projects found for provided filters.")

        allowed_ids = {record.id for record in selected}  # Set for O(1) membership check
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
    finally:
        conn.close()


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
    clear_ack_state = bool(preset_opts.get("clear_ack_state", True))
    clear_recipients = bool(preset_opts.get("clear_recipients", True))
    clear_file_reservations = bool(preset_opts.get("clear_file_reservations", True))
    clear_agent_links = bool(preset_opts.get("clear_agent_links", True))
    scrub_secrets = bool(preset_opts.get("scrub_secrets", True))

    bodies_redacted = 0
    attachments_cleared = 0

    conn = sqlite3.connect(str(snapshot_path))
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")

        # Agent names are already meaningless pseudonyms in our system.
        # Do not rewrite or scrub agent names.
        agents_total = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        agents_pseudonymized = 0

        if clear_ack_state:
            ack_cursor = conn.execute("UPDATE messages SET ack_required = 0")
            ack_flags_cleared = ack_cursor.rowcount or 0
        else:
            ack_flags_cleared = 0

        if clear_recipients:
            recipients_cursor = conn.execute(
                "UPDATE message_recipients SET read_ts = NULL, ack_ts = NULL"
            )
            recipients_cleared = recipients_cursor.rowcount or 0
        else:
            recipients_cleared = 0

        if clear_file_reservations:
            file_res_cursor = conn.execute("DELETE FROM file_reservations")
            file_res_removed = file_res_cursor.rowcount or 0
        else:
            file_res_removed = 0

        if clear_agent_links:
            agent_links_cursor = conn.execute("DELETE FROM agent_links")
            agent_links_removed = agent_links_cursor.rowcount or 0
        else:
            agent_links_removed = 0

        secrets_replaced = 0
        attachments_sanitized = 0

        message_rows = conn.execute("SELECT id, subject, body_md, attachments FROM messages").fetchall()
        for msg in message_rows:
            subject_original = msg["subject"] or ""
            body_original = msg["body_md"] or ""
            if scrub_secrets:
                subject, subj_replacements = _scrub_text(subject_original)
                body, body_replacements = _scrub_text(body_original)
            else:
                subject = subject_original
                body = body_original
                subj_replacements = 0
                body_replacements = 0
            secrets_replaced += subj_replacements + body_replacements
            attachments_value = msg["attachments"]
            attachments_updated = False
            attachment_replacements = 0
            attachment_keys_removed = 0
            if attachments_value:
                if isinstance(attachments_value, str):
                    try:
                        parsed = json.loads(attachments_value)
                    except json.JSONDecodeError:
                        parsed = []
                        attachments_updated = True
                    if isinstance(parsed, list):
                        attachments_data = parsed
                    else:
                        attachments_data = []
                        attachments_updated = True
                elif isinstance(attachments_value, list):
                    attachments_data = attachments_value
                else:
                    attachments_data = []
                    attachments_updated = True
            else:
                attachments_data = []
            if preset_opts["drop_attachments"] and attachments_data:
                attachments_data = []
                attachments_cleared += 1
                attachments_updated = True
            if scrub_secrets and attachments_data:
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
    finally:
        conn.close()

    return ScrubSummary(
        preset=preset_key,
        pseudonym_salt=preset_key,
        agents_total=agents_total,
        agents_pseudonymized=int(agents_pseudonymized),
        ack_flags_cleared=ack_flags_cleared,
        recipients_cleared=recipients_cleared,
        file_reservations_removed=file_res_removed,
        agent_links_removed=agent_links_removed,
        secrets_replaced=secrets_replaced,
        attachments_sanitized=attachments_sanitized,
        bodies_redacted=bodies_redacted,
        attachments_cleared=attachments_cleared,
    )


def build_search_indexes(snapshot_path: Path) -> bool:
    """Create or refresh FTS5 indexes for full-text search. Returns True on success."""

    conn = sqlite3.connect(str(snapshot_path))
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_messages USING fts5(
                subject,
                body,
                importance UNINDEXED,
                project_slug UNINDEXED,
                thread_key UNINDEXED,
                created_ts UNINDEXED
            )
            """
        )
        conn.execute("DELETE FROM fts_messages")
        # Detect presence of thread_id column to avoid compile-time failures on older snapshots
        cols = [row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()]
        has_thread_id = "thread_id" in {c.lower() for c in cols}
        if has_thread_id:
            conn.execute(
                """
                INSERT INTO fts_messages(rowid, subject, body, importance, project_slug, thread_key, created_ts)
                SELECT
                    m.id,
                    COALESCE(m.subject, ''),
                    COALESCE(m.body_md, ''),
                    COALESCE(m.importance, ''),
                    COALESCE(p.slug, ''),
                    CASE
                        WHEN m.thread_id IS NULL OR m.thread_id = '' THEN printf('msg:%d', m.id)
                        ELSE m.thread_id
                    END,
                    COALESCE(m.created_ts, '')
                FROM messages AS m
                LEFT JOIN projects AS p ON p.id = m.project_id
                """
            )
        else:
            conn.execute(
                """
                INSERT INTO fts_messages(rowid, subject, body, importance, project_slug, thread_key, created_ts)
                SELECT
                    m.id,
                    COALESCE(m.subject, ''),
                    COALESCE(m.body_md, ''),
                    COALESCE(m.importance, ''),
                    COALESCE(p.slug, ''),
                    printf('msg:%d', m.id),
                    COALESCE(m.created_ts, '')
                FROM messages AS m
                LEFT JOIN projects AS p ON p.id = m.project_id
                """
            )
        conn.execute("INSERT INTO fts_messages(fts_messages) VALUES('optimize')")
        conn.commit()
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def finalize_snapshot_for_export(snapshot_path: Path) -> None:
    """Apply SQL hygiene optimizations to improve bundle size and httpvfs performance.

    Executes the optimization sequence from the sharing plan:
    - PRAGMA journal_mode=DELETE (single-file mode)
    - PRAGMA page_size=1024 (httpvfs-friendly page size)
    - VACUUM (compact database, improve locality)
    - PRAGMA optimize (update query planner statistics)
    """
    conn = sqlite3.connect(str(snapshot_path))
    try:
        # Convert to DELETE mode for single-file simplicity (no -wal/-shm files)
        conn.execute("PRAGMA journal_mode=DELETE")

        # Set page size for better httpvfs streaming performance
        # Must be done before VACUUM to take effect
        conn.execute("PRAGMA page_size=1024")

        # Compact database and improve page locality
        conn.execute("VACUUM")

        # Update planner statistics after schema/index changes
        conn.execute("PRAGMA analysis_limit=400")
        conn.execute("ANALYZE")

        # Update query planner statistics for optimal execution plans
        conn.execute("PRAGMA optimize")

        conn.commit()
    finally:
        conn.close()


def build_materialized_views(snapshot_path: Path) -> None:
    """Create materialized views and covering indexes for httpvfs performance.

    Creates pre-computed tables optimized for common viewer queries:
    - message_overview_mv: Denormalized message list with sender info
    - attachments_by_message_mv: Flattened attachments for easier querying

    Also creates covering indexes to enable efficient httpvfs range scans.
    """
    conn = sqlite3.connect(str(snapshot_path))
    try:
        # Ensure recipients table exists to satisfy LEFT JOIN in the view creation
        conn.execute("CREATE TABLE IF NOT EXISTS message_recipients (message_id INTEGER, agent_id INTEGER)")
        # Message overview materialized view
        # Denormalizes messages with sender names for efficient list rendering
        cols = [row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()]
        colset = {c.lower() for c in cols}
        has_thread_id = "thread_id" in colset
        has_sender_id = "sender_id" in colset
        if has_thread_id:
            conn.executescript(
                """
                DROP TABLE IF EXISTS message_overview_mv;
                CREATE TABLE message_overview_mv AS
                SELECT
                    m.id,
                    m.project_id,
                    m.thread_id,
                    m.subject,
                    m.importance,
                    m.ack_required,
                    m.created_ts,
                    {sender_expr} AS sender_name,
                    LENGTH(m.body_md) AS body_length,
                    json_array_length(m.attachments) AS attachment_count,
                    SUBSTR(COALESCE(m.body_md, ''), 1, 280) AS latest_snippet,
                    COALESCE(r.recipients, '') AS recipients
                FROM messages m
                {sender_join}
                LEFT JOIN (
                    SELECT
                        mr.message_id,
                        GROUP_CONCAT(COALESCE(ag.name, ''), ', ') AS recipients
                    FROM message_recipients mr
                    LEFT JOIN agents ag ON ag.id = mr.agent_id
                    GROUP BY mr.message_id
                ) r ON r.message_id = m.id
                ORDER BY m.created_ts DESC;

                -- Covering indexes for common query patterns
                CREATE INDEX idx_msg_overview_created ON message_overview_mv(created_ts DESC);
                CREATE INDEX idx_msg_overview_thread ON message_overview_mv(thread_id, created_ts DESC);
                CREATE INDEX idx_msg_overview_project ON message_overview_mv(project_id, created_ts DESC);
                CREATE INDEX idx_msg_overview_importance ON message_overview_mv(importance, created_ts DESC);
                """
                .format(
                    sender_expr=("a.name" if has_sender_id else "''"),
                    sender_join=("JOIN agents a ON m.sender_id = a.id" if has_sender_id else ""),
                )
            )
        else:
            conn.executescript(
                """
                DROP TABLE IF EXISTS message_overview_mv;
                CREATE TABLE message_overview_mv AS
                SELECT
                    m.id,
                    m.project_id,
                    printf('msg:%d', m.id) AS thread_id,
                    m.subject,
                    m.importance,
                    m.ack_required,
                    m.created_ts,
                    {sender_expr} AS sender_name,
                    LENGTH(m.body_md) AS body_length,
                    json_array_length(m.attachments) AS attachment_count,
                    SUBSTR(COALESCE(m.body_md, ''), 1, 280) AS latest_snippet,
                    COALESCE(r.recipients, '') AS recipients
                FROM messages m
                {sender_join}
                LEFT JOIN (
                    SELECT
                        mr.message_id,
                        GROUP_CONCAT(COALESCE(ag.name, ''), ', ') AS recipients
                    FROM message_recipients mr
                    LEFT JOIN agents ag ON ag.id = mr.agent_id
                    GROUP BY mr.message_id
                ) r ON r.message_id = m.id
                ORDER BY m.created_ts DESC;

                -- Covering indexes for common query patterns
                CREATE INDEX idx_msg_overview_created ON message_overview_mv(created_ts DESC);
                CREATE INDEX idx_msg_overview_thread ON message_overview_mv(thread_id, created_ts DESC);
                CREATE INDEX idx_msg_overview_project ON message_overview_mv(project_id, created_ts DESC);
                CREATE INDEX idx_msg_overview_importance ON message_overview_mv(importance, created_ts DESC);
                """
                .format(
                    sender_expr=("a.name" if has_sender_id else "''"),
                    sender_join=("JOIN agents a ON m.sender_id = a.id" if has_sender_id else ""),
                )
            )

        # Attachments by message materialized view
        # Flattens JSON attachments array for easier filtering and counting
        if has_thread_id:
            conn.executescript(
                """
                DROP TABLE IF EXISTS attachments_by_message_mv;
                CREATE TABLE attachments_by_message_mv AS
                SELECT
                    m.id AS message_id,
                    m.project_id,
                    m.thread_id,
                    m.created_ts,
                    json_extract(value, '$.type') AS attachment_type,
                    json_extract(value, '$.media_type') AS media_type,
                    json_extract(value, '$.path') AS path,
                    CAST(json_extract(value, '$.bytes') AS INTEGER) AS size_bytes
                FROM messages m,
                     json_each(m.attachments)
                WHERE m.attachments != '[]';

                -- Indexes for attachment queries
                CREATE INDEX idx_attach_by_msg ON attachments_by_message_mv(message_id);
                CREATE INDEX idx_attach_by_type ON attachments_by_message_mv(attachment_type, created_ts DESC);
                CREATE INDEX idx_attach_by_project ON attachments_by_message_mv(project_id, created_ts DESC);
                """
            )
        else:
            conn.executescript(
                """
                DROP TABLE IF EXISTS attachments_by_message_mv;
                CREATE TABLE attachments_by_message_mv AS
                SELECT
                    m.id AS message_id,
                    m.project_id,
                    NULL AS thread_id,
                    m.created_ts,
                    json_extract(value, '$.type') AS attachment_type,
                    json_extract(value, '$.media_type') AS media_type,
                    json_extract(value, '$.path') AS path,
                    CAST(json_extract(value, '$.bytes') AS INTEGER) AS size_bytes
                FROM messages m,
                     json_each(m.attachments)
                WHERE m.attachments != '[]';

                -- Indexes for attachment queries
                CREATE INDEX idx_attach_by_msg ON attachments_by_message_mv(message_id);
                CREATE INDEX idx_attach_by_type ON attachments_by_message_mv(attachment_type, created_ts DESC);
                CREATE INDEX idx_attach_by_project ON attachments_by_message_mv(project_id, created_ts DESC);
                """
            )

        # FTS search overview materialized view
        # Pre-computes search result snippets and highlights for efficient rendering
        # Only created if FTS5 is available
        try:
            conn.execute("SELECT 1 FROM fts_messages LIMIT 1")
            conn.executescript(
                """
                DROP TABLE IF EXISTS fts_search_overview_mv;
                CREATE TABLE fts_search_overview_mv AS
                SELECT
                    m.rowid,
                    m.id,
                    m.subject,
                    m.created_ts,
                    m.importance,
                    a.name AS sender_name,
                    SUBSTR(m.body_md, 1, 200) AS snippet
                FROM messages m
                JOIN agents a ON m.sender_id = a.id
                ORDER BY m.created_ts DESC;

                -- Index for FTS result lookups
                CREATE INDEX idx_fts_overview_rowid ON fts_search_overview_mv(rowid);
                CREATE INDEX idx_fts_overview_created ON fts_search_overview_mv(created_ts DESC);
                """
            )
        except sqlite3.OperationalError:
            # FTS5 not available or not configured, skip this view
            pass

        conn.commit()
    finally:
        conn.close()


def create_performance_indexes(snapshot_path: Path) -> None:
    """Create covering indexes on hot message lookup paths used by the static viewer."""

    conn = sqlite3.connect(str(snapshot_path))
    try:
        # Ensure derived lowercase columns exist for case-insensitive search
        for column in ("subject_lower", "sender_lower"):
            with suppress(sqlite3.OperationalError):
                conn.execute(f"ALTER TABLE messages ADD COLUMN {column} TEXT")

        cols = [row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()]
        colset = {c.lower() for c in cols}
        has_sender_id = "sender_id" in colset
        if has_sender_id:
            conn.execute(
                """
                UPDATE messages
                SET
                    subject_lower = LOWER(COALESCE(subject, '')),
                    sender_lower = LOWER(
                        COALESCE(
                            (SELECT name FROM agents WHERE agents.id = messages.sender_id),
                            ''
                        )
                    )
                """
            )
        else:
            conn.execute(
                """
                UPDATE messages
                SET
                    subject_lower = LOWER(COALESCE(subject, '')),
                    sender_lower = ''
                """
            )

        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_messages_created_ts
              ON messages(created_ts DESC);

            CREATE INDEX IF NOT EXISTS idx_messages_subject_lower
              ON messages(subject_lower);

            CREATE INDEX IF NOT EXISTS idx_messages_sender_lower
              ON messages(sender_lower);
            """
        )
        # Conditional indexes for optional columns
        with suppress(sqlite3.OperationalError):
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_id, created_ts DESC)")
        # thread_id index if column exists
        with suppress(sqlite3.OperationalError):
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, created_ts DESC)")

        conn.commit()
    finally:
        conn.close()


def summarize_snapshot(
    snapshot_path: Path,
    *,
    storage_root: Path,
    inline_threshold: int = INLINE_ATTACHMENT_THRESHOLD,
    detach_threshold: int = DETACH_ATTACHMENT_THRESHOLD,
) -> dict[str, Any]:
    storage_root = storage_root.resolve()
    conn = sqlite3.connect(str(snapshot_path))
    try:
        conn.row_factory = sqlite3.Row
        total_messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        try:
            total_threads = conn.execute(
                """
                SELECT COUNT(DISTINCT(
                    CASE WHEN thread_id IS NULL OR thread_id = ''
                         THEN printf('msg:%d', id)
                         ELSE thread_id
                    END
                ))
                FROM messages
                """
            ).fetchone()[0]
        except sqlite3.OperationalError:
            total_threads = total_messages
        projects = [
            {"slug": row["slug"], "human_key": row["human_key"]}
            for row in conn.execute("SELECT slug, human_key FROM projects ORDER BY slug")
        ]
        importance_counts = {
            (row["importance"] or "normal"): row["count"]
            for row in conn.execute(
                "SELECT COALESCE(importance, 'normal') AS importance, COUNT(*) AS count FROM messages GROUP BY COALESCE(importance, 'normal')"
            )
        }

        attachments_stats = {
            "total": 0,
            "inline_candidates": 0,
            "external_candidates": 0,
            "missing": 0,
            "largest_bytes": 0,
        }

        rows = conn.execute("SELECT id, attachments FROM messages").fetchall()
        for row in rows:
            raw = row["attachments"]
            if not raw:
                continue
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                continue
            if not isinstance(data, list):
                continue
            for entry in data:
                if not isinstance(entry, dict) or entry.get("type") != "file":
                    continue
                attachments_stats["total"] += 1
                original_path = entry.get("path") or entry.get("original_path")
                if not original_path:
                    attachments_stats["missing"] += 1
                    continue
                source_path = Path(original_path)
                if not source_path.is_absolute():
                    source_path = (storage_root / original_path).resolve()
                if not source_path.exists():
                    attachments_stats["missing"] += 1
                    continue
                try:
                    size = source_path.stat().st_size
                except OSError:
                    attachments_stats["missing"] += 1
                    continue
                attachments_stats["largest_bytes"] = max(attachments_stats["largest_bytes"], size)
                if size <= inline_threshold:
                    attachments_stats["inline_candidates"] += 1
                if size >= detach_threshold:
                    attachments_stats["external_candidates"] += 1
    finally:
        conn.close()

    return {
        "messages": int(total_messages),
        "threads": int(total_threads),
        "projects": projects,
        "importance": importance_counts,
        "attachments": attachments_stats,
    }


def create_snapshot_context(
    *,
    source_database: Path,
    snapshot_path: Path,
    project_filters: Sequence[str],
    scrub_preset: str,
) -> SnapshotContext:
    """Materialize and prepare a snapshot for export."""

    create_sqlite_snapshot(source_database, snapshot_path)
    scope = apply_project_scope(snapshot_path, project_filters)
    scrub_summary = scrub_snapshot(snapshot_path, preset=scrub_preset)
    fts_enabled = build_search_indexes(snapshot_path)
    build_materialized_views(snapshot_path)
    create_performance_indexes(snapshot_path)
    finalize_snapshot_for_export(snapshot_path)
    return SnapshotContext(
        snapshot_path=snapshot_path,
        scope=scope,
        scrub_summary=scrub_summary,
        fts_enabled=fts_enabled,
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

    conn = sqlite3.connect(str(snapshot_path))
    try:
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
    finally:
        conn.close()

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

    checksum_lines: list[str] = []

    with snapshot_path.open("rb") as src:
        index = 0
        while True:
            chunk = src.read(chunk_bytes)
            if not chunk:
                break
            chunk_path = chunk_dir / f"{index:05d}.bin"
            chunk_path.write_bytes(chunk)
            digest = hashlib.sha256(chunk).hexdigest()
            checksum_lines.append(f"{digest}  chunks/{chunk_path.name}\n")
            index += 1

    if checksum_lines:
        checksums_path = output_dir / "chunks.sha256"
        checksums_path.write_text("".join(checksum_lines), encoding="utf-8")

    config = {
        "version": 1,
        "chunk_size": chunk_bytes,
        "chunk_count": index,
        "pattern": "chunks/{index:05d}.bin",
        "original_bytes": size,
        "threshold_bytes": threshold_bytes,
    }
    _write_json_file(output_dir / "mailbox.sqlite3.config.json", config)
    return config


def build_bundle_assets(
    snapshot_path: Path,
    output_dir: Path,
    *,
    storage_root: Path,
    inline_threshold: int,
    detach_threshold: int,
    chunk_threshold: int,
    chunk_size: int,
    scope: ProjectScopeResult,
    project_filters: Sequence[str],
    scrub_summary: ScrubSummary,
    hosting_hints: Sequence[HostingHint],
    fts_enabled: bool,
    export_config: Mapping[str, Any],
    exporter_version: str = "prototype",
) -> BundleArtifacts:
    """Bundle attachments, viewer assets, and scaffolding for the export."""

    attachments_manifest = bundle_attachments(
        snapshot_path,
        output_dir,
        storage_root=storage_root,
        inline_threshold=inline_threshold,
        detach_threshold=detach_threshold,
    )
    chunk_manifest = maybe_chunk_database(
        snapshot_path,
        output_dir,
        threshold_bytes=chunk_threshold,
        chunk_bytes=chunk_size,
    )
    copy_viewer_assets(output_dir)
    viewer_data = export_viewer_data(snapshot_path, output_dir, fts_enabled=fts_enabled)
    write_bundle_scaffolding(
        output_dir,
        snapshot=snapshot_path,
        scope=scope,
        project_filters=project_filters,
        scrub_summary=scrub_summary,
        attachments_manifest=attachments_manifest,
        chunk_manifest=chunk_manifest,
        hosting_hints=hosting_hints,
        viewer_data=viewer_data,
        exporter_version=exporter_version,
        export_config=export_config,
    )
    return BundleArtifacts(
        attachments_manifest=attachments_manifest,
        chunk_manifest=chunk_manifest,
        viewer_data=viewer_data,
    )


def copy_viewer_assets(output_dir: Path) -> None:
    """Copy viewer assets into the export output directory.

    Prefer the live source tree (useful during development) and fall back to
    installed package resources when the source tree is not available.
    """

    viewer_root = output_dir / "viewer"
    viewer_root.mkdir(parents=True, exist_ok=True)

    # Attempt to copy from live source tree first
    source_tree = Path(__file__).parent / "viewer_assets"
    if source_tree.exists() and source_tree.is_dir():
        for src_path in sorted(p for p in source_tree.rglob("*") if p.is_file()):
            rel = src_path.relative_to(source_tree)
            dest = viewer_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src_path.read_bytes())
        return

    # Fallback to packaged resources
    _verify_viewer_vendor_assets()

    package_root = resources.files("mcp_agent_mail.viewer_assets")

    def _walk(node: Any, relative: Path) -> None:
        for child in node.iterdir():
            child_relative = relative / child.name
            if child.is_dir():
                _walk(child, child_relative)
            else:
                destination = viewer_root / child_relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(child.read_bytes())

    _walk(package_root, Path())


def _load_vendor_manifest() -> dict[str, Any]:
    manifest_path = resources.files("mcp_agent_mail.viewer_assets") / "vendor_manifest.json"
    try:
        with manifest_path.open("r", encoding="utf-8") as handle:
            return cast(dict[str, Any], json.load(handle))
    except FileNotFoundError as exc:  # pragma: no cover - packaging error
        raise ShareExportError("Viewer asset manifest missing; reinstall package.") from exc


def _verify_viewer_vendor_assets() -> None:
    manifest = _load_vendor_manifest()
    vendor_root = resources.files("mcp_agent_mail.viewer_assets") / "vendor"
    for manifest_group in manifest.values():
        files = manifest_group.get("files", {})
        for filename, meta in files.items():
            expected = meta.get("sha256")
            if not expected:
                continue
            asset_path = vendor_root / filename
            try:
                data = asset_path.read_bytes()
            except FileNotFoundError as exc:
                raise ShareExportError(
                    "Viewer vendor asset "
                    f"'{filename}' missing. Run scripts/update_sqlite_vendor.py to refresh assets."
                ) from exc
            digest = hashlib.sha256(data).hexdigest()
            if digest != expected:
                raise ShareExportError(
                    "Checksum mismatch for viewer vendor asset "
                    f"'{filename}'. Expected {expected}, got {digest}. "
                    "Run scripts/update_sqlite_vendor.py to refresh assets."
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


def _compute_sri(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except (IOError, OSError) as exc:
        raise ShareExportError(f"Failed to read {path} for SRI computation: {exc}") from exc
    encoded = base64.b64encode(digest.digest()).decode("ascii")
    return f"sha256-{encoded}"


def _build_viewer_sri(bundle_root: Path) -> dict[str, str]:
    viewer_root = bundle_root / "viewer"
    if not viewer_root.exists():
        return {}
    sri_map: dict[str, str] = {}
    for path in viewer_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in {".js", ".css", ".wasm"}:
            continue
        relative = path.relative_to(bundle_root).as_posix()
        sri_map[relative] = _compute_sri(path)
    return sri_map


def verify_bundle(bundle_path: Path, *, public_key: Optional[str] = None) -> dict[str, Any]:
    bundle_root = Path(bundle_path).expanduser().resolve()
    manifest_path = bundle_root / "manifest.json"
    if not manifest_path.exists():
        raise ShareExportError(f"manifest.json not found in bundle at {bundle_root}")

    try:
        manifest_bytes = manifest_path.read_bytes()
    except (IOError, OSError) as exc:
        raise ShareExportError(f"Failed to read manifest.json: {exc}") from exc

    try:
        manifest_data = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise ShareExportError(f"manifest.json is not valid JSON: {exc}") from exc

    viewer_section = cast(dict[str, Any], manifest_data.get("viewer", {}))
    sri_entries = cast(dict[str, str], viewer_section.get("sri", {}))
    sri_failures: list[str] = []
    for relative_path, expected in sri_entries.items():
        target = bundle_root / relative_path
        if not target.exists():
            sri_failures.append(f"Missing asset for SRI entry: {relative_path}")
            continue
        actual = _compute_sri(target)
        if actual != expected:
            sri_failures.append(
                f"SRI mismatch for {relative_path}: expected {expected}, got {actual}"
            )

    signature_checked = False
    signature_verified = False
    sig_path = bundle_root / "manifest.sig.json"
    if sig_path.exists() or public_key:
        if not sig_path.exists():
            raise ShareExportError("manifest.sig.json missing but a public key was provided for verification.")

        try:
            sig_payload = json.loads(sig_path.read_text(encoding="utf-8"))
        except (IOError, OSError) as exc:
            raise ShareExportError(f"Failed to read manifest.sig.json: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ShareExportError(f"manifest.sig.json is not valid JSON: {exc}") from exc

        key_b64 = public_key or sig_payload.get("public_key")
        signature_b64 = sig_payload.get("signature")
        if not key_b64 or not signature_b64:
            raise ShareExportError("manifest.sig.json missing public_key or signature fields.")
        try:
            from nacl.exceptions import BadSignatureError
            from nacl.signing import VerifyKey
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ShareExportError("PyNaCl is required to verify manifest signatures.") from exc

        try:
            verify_key = VerifyKey(base64.b64decode(key_b64))
        except (ValueError, binascii.Error) as exc:
            raise ShareExportError(f"Invalid base64 in public_key: {exc}") from exc

        try:
            signature_bytes = base64.b64decode(signature_b64)
        except (ValueError, binascii.Error) as exc:
            raise ShareExportError(f"Invalid base64 in signature: {exc}") from exc

        try:
            verify_key.verify(manifest_bytes, signature_bytes)
            signature_verified = True
        except BadSignatureError as exc:
            raise ShareExportError("Manifest signature verification failed.") from exc
        signature_checked = True

    if sri_failures:
        raise ShareExportError("\n".join(sri_failures))

    return {
        "bundle": str(bundle_root),
        "sri_checked": bool(sri_entries),
        "signature_checked": signature_checked,
        "signature_verified": signature_verified,
    }


def decrypt_with_age(
    encrypted_path: Path,
    output_path: Path,
    *,
    identity: Optional[Path] = None,
    passphrase: Optional[str] = None,
) -> None:
    age_exe = shutil.which("age")
    if not age_exe:
        raise ShareExportError("`age` CLI not found in PATH. Install age to decrypt bundles.")
    if identity and passphrase:
        raise ShareExportError("Provide either an identity file or a passphrase, not both.")
    if not identity and passphrase is None:
        raise ShareExportError("Decryption requires --identity or --passphrase.")

    # Expand and validate encrypted file path
    encrypted_path = encrypted_path.expanduser().resolve()
    if not encrypted_path.exists():
        raise ShareExportError(f"Encrypted file not found: {encrypted_path}")

    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [age_exe, "-d", "-o", str(output_path)]
    input_text: Optional[str] = None
    if identity:
        identity_path = identity.expanduser().resolve()
        if not identity_path.exists():
            raise ShareExportError(f"Identity file not found: {identity_path}")
        cmd.extend(["-i", str(identity_path)])
    elif passphrase is not None:
        cmd.append("-p")
        input_text = passphrase + "\n"

    cmd.append(str(encrypted_path))
    result = subprocess.run(cmd, capture_output=True, text=True, input=input_text)
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise ShareExportError(f"age decryption failed: {stderr}")


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
    export_config: Mapping[str, Any],
    exporter_version: str = "prototype",
) -> None:
    """Create manifest and helper docs around the freshly minted snapshot."""

    project_entries = [
        {"slug": record.slug, "human_key": record.human_key}
        for record in scope.projects
    ]

    viewer_sri = _build_viewer_sri(output_dir)

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
            "Prototype manifest. Viewer asset Subresource Integrity hashes recorded under viewer.sri.",
            "Viewer scaffold with diagnostics is bundled; SPA search/thread views arrive in upcoming milestones.",
        ],
    }
    viewer_meta: dict[str, Any] = dict(viewer_data or {})
    if viewer_meta:
        fts_flag = bool(viewer_meta.get("meta_info", {}).get("fts_enabled", False))
        database_section = cast(dict[str, Any], manifest["database"])
        database_section["fts_enabled"] = fts_flag
    if viewer_sri:
        viewer_meta.setdefault("sri", viewer_sri)
    if viewer_meta:
        manifest["viewer"] = viewer_meta
    elif viewer_sri:
        manifest["viewer"] = {"sri": viewer_sri}
    export_config_payload = dict(export_config)
    export_config_payload.setdefault("projects", list(project_filters))
    export_config_payload.setdefault("scrub_preset", scrub_summary.preset)
    manifest["export_config"] = {k: v for k, v in export_config_payload.items() if v is not None}
    _write_json_file(output_dir / "manifest.json", manifest)

    readme_lines = [
        "# MCP Agent Mail — Shared Mailbox Snapshot",
        "",
        "This repository hosts a static export of an MCP Agent Mail project. "
        "It contains a scrubbed SQLite snapshot plus a self-contained viewer so teammates can browse threads, "
        "attachments, and metadata without needing direct database access.",
        "",
        "## What's Included",
        "",
        "- `mailbox.sqlite3` — scrubbed mailbox database (agent names retained, ack/read state cleared).",
        "- `viewer/` — static web viewer (Alpine.js + Tailwind) with inbox, thread explorer, search, and attachment tooling.",
        "- `manifest.json` — machine-readable metadata (project scope, scrub stats, hosting hints, asset SRI hashes).",
        "- `_headers` — COOP/COEP headers for hosts that support Netlify-style header rules.",
        "- `.nojekyll` — disables GitHub Pages' Jekyll processing so `_headers` and `.wasm` assets are served untouched.",
        "- `HOW_TO_DEPLOY.md` — detailed deployment checklist for GitHub Pages, Cloudflare Pages, Netlify, or generic hosts.",
        "",
        "## Live Viewer",
        "",
        "The `viewer/index.html` application renders the exported mailbox locally or when deployed to static hosting. "
        "If this repo is published via GitHub Pages, the root `index.html` immediately redirects to the viewer.",
        "",
    ]
    if hosting_hints:
        readme_lines.append("Detected hosting targets:")
        for hint in hosting_hints:
            signals_text = "; ".join(hint.signals)
            readme_lines.append(f"- **{hint.title}** — {hint.summary} _(signals: {signals_text})_")
        readme_lines.append("")

    readme_lines.extend(
        [
            "## Quick Start",
            "",
            "1. **Install dependencies** (first time only):",
            "   ```bash",
            "   uv sync",
            "   ```",
            "2. **Rebuild or update the export** from the source project:",
            "   ```bash",
            "   uv run python -m mcp_agent_mail.cli share update /path/to/this/repo",
            "   ```",
            "3. **Preview locally**:",
            "   ```bash",
            "   uv run python -m mcp_agent_mail.cli share preview .",
            "   ```",
            "   The command serves the viewer at `http://127.0.0.1:9000/` with hot reload.",
            "4. **Deploy** using GitHub Pages (built into the `share wizard`) or manually follow `HOW_TO_DEPLOY.md`.",
            "",
            "## Regenerating a Fresh Snapshot",
            "",
            "From the MCP Agent Mail source checkout, run:",
            "",
            "```bash",
            "uv run python -m mcp_agent_mail.cli share export \\",
            "  --output /path/to/this/repo \\",
            "  --project <project-slug> \\",
            "  --scrub-preset standard \\",
            "  --no-zip",
            "```",
            "",
            "This overwrites the bundle (after you clean the repo) with the latest messages while preserving viewer assets.",
            "",
            "## Verifying Integrity",
            "",
            "- **Signed bundles**: If `manifest.sig.json` is present, verify with:",
            "  ```bash",
            "  uv run python -m mcp_agent_mail.cli share verify . --public-key $(cat signing-*.pub)",
            "  ```",
            "- **SRI hashes**: `manifest.json` records SHA256 digests for viewer assets; static hosts can pin hashes if desired.",
            "",
            "## Troubleshooting",
            "",
            "- **GitHub Pages shows 404**: confirm Pages is set to the `main` branch and root (`/`). "
            "The wizard calls `gh api repos/:owner/:repo/pages` automatically; re-run it if needed.",
            "- **`.wasm` served as text/plain**: ensure `.nojekyll` is present and `_headers` are respected, or configure MIME types manually.",
            "- **Viewer warns about OPFS**: host must send COOP/COEP headers. GitHub Pages requires the bundled `coi-serviceworker.js` to be uncommented (see `HOW_TO_DEPLOY.md`).",
            "",
            "## About MCP Agent Mail",
            "",
            "MCP Agent Mail is an asynchronous coordination layer for multi-agent coding workflows. "
            "It captures messages, attachments, and advisory file reservations so agents can collaborate safely. "
            "Static exports make it easy to share audit trails without granting direct database access.",
        ]
    )

    _write_text_file(output_dir / "README.md", "\n".join(readme_lines) + "\n")
    _write_text_file(output_dir / "index.html", INDEX_REDIRECT_HTML)
    _write_text_file(output_dir / ".nojekyll", "")

    how_to_deploy = build_how_to_deploy(hosting_hints)
    _write_text_file(output_dir / "HOW_TO_DEPLOY.md", how_to_deploy)

    # Generate _headers file for Cloudflare/Netlify COOP/COEP support
    headers_content = _generate_headers_file()
    _write_text_file(output_dir / "_headers", headers_content)


def _generate_headers_file() -> str:
    """Generate _headers file for Cloudflare Pages and Netlify deployment.

    This file configures Cross-Origin-Opener-Policy and Cross-Origin-Embedder-Policy
    headers required for OPFS (Origin Private File System) and SharedArrayBuffer support.
    """
    return """# Cross-Origin Isolation headers for OPFS and SharedArrayBuffer support
# Compatible with Cloudflare Pages and Netlify
# See: https://web.dev/coop-coep/

/*
  Cross-Origin-Opener-Policy: same-origin
  Cross-Origin-Embedder-Policy: require-corp

# Allow viewer assets to be loaded
/viewer/*
  Cross-Origin-Resource-Policy: same-origin

# SQLite database and chunks
/*.sqlite3
  Cross-Origin-Resource-Policy: same-origin
  Content-Type: application/x-sqlite3

/chunks/*
  Cross-Origin-Resource-Policy: same-origin
  Content-Type: application/octet-stream

# Attachments
/attachments/*
  Cross-Origin-Resource-Policy: same-origin
"""


__all__ = [
    "SCRUB_PRESETS",
    "HostingHint",
    "ShareExportError",
    "apply_project_scope",
    "build_how_to_deploy",
    "build_search_indexes",
    "bundle_attachments",
    "copy_viewer_assets",
    "create_performance_indexes",
    "create_sqlite_snapshot",
    "decrypt_with_age",
    "detect_hosting_hints",
    "encrypt_bundle",
    "export_viewer_data",
    "maybe_chunk_database",
    "package_directory_as_zip",
    "prepare_output_directory",
    "resolve_sqlite_database_path",
    "scrub_snapshot",
    "sign_manifest",
    "summarize_snapshot",
    "verify_bundle",
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
