from __future__ import annotations

import json
import sqlite3
import warnings
from pathlib import Path
from zipfile import ZipFile

import pytest
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from typer.testing import CliRunner

from mcp_agent_mail import cli as cli_module
from mcp_agent_mail.config import get_settings

warnings.filterwarnings("ignore", category=ResourceWarning)

console = Console()


def _seed_mailbox(db_path: Path, storage_root: Path) -> None:
    storage_root.mkdir(parents=True, exist_ok=True)
    attachments_dir = storage_root / "attachments" / "raw"
    attachments_dir.mkdir(parents=True, exist_ok=True)
    (attachments_dir / "inline.txt").write_text("inline bytes", encoding="utf-8")
    (attachments_dir / "bundle.bin").write_bytes(b"B" * 256)
    (attachments_dir / "huge.dat").write_bytes(b"H" * 1024 * 32)

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE projects (id INTEGER PRIMARY KEY, slug TEXT, human_key TEXT);
            CREATE TABLE agents (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                name TEXT,
                contact_policy TEXT DEFAULT 'auto'
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                subject TEXT,
                body_md TEXT,
                importance TEXT,
                ack_required INTEGER,
                created_ts TEXT,
                attachments TEXT
            );
            CREATE TABLE message_recipients (
                message_id INTEGER,
                agent_id INTEGER,
                kind TEXT,
                read_ts TEXT,
                ack_ts TEXT
            );
            CREATE TABLE file_reservations (id INTEGER PRIMARY KEY, project_id INTEGER);
            CREATE TABLE agent_links (
                id INTEGER PRIMARY KEY,
                a_project_id INTEGER,
                b_project_id INTEGER
            );
            CREATE TABLE project_sibling_suggestions (
                id INTEGER PRIMARY KEY,
                project_a_id INTEGER,
                project_b_id INTEGER
            );
        """
        )
        conn.execute("INSERT INTO projects (id, slug, human_key) VALUES (1, 'primary', 'Primary Mail')")
        conn.execute("INSERT INTO agents (id, project_id, name) VALUES (1, 1, 'Integration Bot')")

        attachments = [
            {
                "type": "file",
                "media_type": "text/plain",
                "path": "attachments/raw/inline.txt",
            },
            {
                "type": "file",
                "media_type": "application/octet-stream",
                "path": "attachments/raw/bundle.bin",
            },
            {
                "type": "file",
                "media_type": "application/octet-stream",
                "path": "attachments/raw/huge.dat",
            },
        ]

        conn.execute(
            """
            INSERT INTO messages (id, project_id, subject, body_md, importance, ack_required, created_ts, attachments)
            VALUES (1, 1, 'Integration Test', 'Body with bearer TOKEN', 'normal', 1, '2025-01-01T00:00:00Z', ?)
            """,
            (json.dumps(attachments),),
        )
        conn.execute(
            """
            INSERT INTO message_recipients (message_id, agent_id, kind, read_ts, ack_ts)
            VALUES (1, 1, 'to', '2025-01-02T00:00:00Z', '2025-01-03T00:00:00Z')
            """
        )
        conn.execute("INSERT INTO file_reservations (id, project_id) VALUES (1, 1)")
        conn.execute(
            "INSERT INTO agent_links (id, a_project_id, b_project_id) VALUES (1, 1, 1)"
        )


@pytest.mark.usefixtures("isolated_env")
def test_share_export_end_to_end(monkeypatch, tmp_path: Path) -> None:
    settings = get_settings()
    db_path = Path(settings.database.url.replace("sqlite+aiosqlite:///", ""))
    storage_root = Path(settings.storage.root)
    _seed_mailbox(db_path, storage_root)

    output_dir = tmp_path / "bundle"
    runner = CliRunner()

    console.print(Panel.fit("🚀 Starting mailbox share export integration test"))

    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/incubator")

    table = Table(title="Export Configuration")
    table.add_column("Setting")
    table.add_column("Value")
    table.add_row("Database", str(db_path))
    table.add_row("Storage root", str(storage_root))
    table.add_row("Output Dir", str(output_dir))
    table.add_row("Inline Threshold", "64 bytes")
    table.add_row("Detach Threshold", "10240 bytes")
    console.print(table)

    result = runner.invoke(
        cli_module.app,
        [
            "share",
            "export",
            "--output",
            str(output_dir),
            "--project",
            "primary",
            "--inline-threshold",
            "64",
            "--detach-threshold",
            "10240",
        ],
    )
    console.print(Syntax(result.output, "text", theme="ansi_light"))
    assert result.exit_code == 0

    manifest_path = output_dir / "manifest.json"
    assert manifest_path.is_file()
    with manifest_path.open(encoding="utf-8") as handle:
    manifest = json.load(handle)

    console.print(
        Panel(
            Syntax(json.dumps(manifest, indent=2), "json", theme="ansi_light"),
            title="Manifest Snapshot",
            border_style="cyan",
        )
    )

    stats = manifest["attachments"]["stats"]
    assert stats["inline"] == 1
    assert stats["copied"] == 1
    assert stats["externalized"] == 1
    assert stats["missing"] == 0
    assert manifest["scrub"]["preset"] == "standard"

    hosting_detected = {entry["id"] for entry in manifest.get("hosting", {}).get("detected", [])}
    assert "github_pages" in hosting_detected

    viewer_dir = output_dir / "viewer"
    assert (viewer_dir / "index.html").is_file()
    assert (viewer_dir / "styles.css").is_file()
    assert (viewer_dir / "viewer.js").is_file()
    index_content = (viewer_dir / "index.html").read_text(encoding="utf-8")
    assert "Static Viewer" in index_content

    zip_path = output_dir.with_suffix(".zip")
    assert zip_path.is_file()
    with ZipFile(zip_path) as archive:
        names = archive.namelist()
    console.print(
        Panel.fit(
            "\n".join(names),
            title="ZIP Contents",
            border_style="magenta",
        )
    )
    assert "manifest.json" in names
    assert "mailbox.sqlite3" in names
    assert "viewer/index.html" in names

    readme_text = (output_dir / "README.txt").read_text(encoding="utf-8")
    assert "Detected hosting targets" in readme_text

    deployment_text = (output_dir / "HOW_TO_DEPLOY.md").read_text(encoding="utf-8")
    assert "## GitHub Pages (detected)" in deployment_text
