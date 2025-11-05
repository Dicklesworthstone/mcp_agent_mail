from __future__ import annotations

import base64
import json
import sqlite3
import threading
import urllib.request
import warnings
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mcp_agent_mail import cli as cli_module
from mcp_agent_mail.config import clear_settings_cache
from mcp_agent_mail.share import (
    SCRUB_PRESETS,
    ShareExportError,
    bundle_attachments,
    maybe_chunk_database,
    scrub_snapshot,
)

warnings.filterwarnings("ignore", category=ResourceWarning)

pytestmark = pytest.mark.filterwarnings("ignore:.*ResourceWarning")


def _build_snapshot(tmp_path: Path) -> Path:
    snapshot = tmp_path / "snapshot.sqlite3"
    with sqlite3.connect(snapshot) as conn:
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
        conn.execute(
            "INSERT INTO projects (id, slug, human_key) VALUES (1, 'demo', 'demo-human')"
        )
        conn.execute(
            "INSERT INTO agents (id, project_id, name) VALUES (1, 1, 'Alice Agent')"
        )
        attachments = [
            {
                "type": "file",
                "path": "attachments/raw/secret.txt",
                "media_type": "text/plain",
                "download_url": "https://example.com/private?token=ghp_secret",
                "authorization": "Bearer " + "C" * 24,
            }
        ]
        conn.execute(
            """
            INSERT INTO messages (id, project_id, subject, body_md, importance, ack_required, created_ts, attachments)
            VALUES (1, 1, ?, ?, 'normal', 1, '2025-01-01T00:00:00Z', ?)
            """,
            (
                "Token sk-" + "A" * 24,
                "Body bearer " + "B" * 24,
                json.dumps(attachments),
            ),
        )
        conn.execute(
            "INSERT INTO message_recipients (message_id, agent_id, kind, read_ts, ack_ts) VALUES (1, 1, 'to', '2025-01-01', '2025-01-02')"
        )
        conn.execute(
            "INSERT INTO file_reservations (id, project_id) VALUES (1, 1)"
        )
        conn.execute(
            "INSERT INTO agent_links (id, a_project_id, b_project_id) VALUES (1, 1, 1)"
        )
    return snapshot


def _read_message(snapshot: Path) -> tuple[str, str, list[dict[str, object]]]:
    with sqlite3.connect(snapshot) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT subject, body_md, attachments FROM messages WHERE id = 1").fetchone()
    attachments_raw = row["attachments"]
    attachments = json.loads(attachments_raw) if attachments_raw else []
    return row["subject"], row["body_md"], attachments


def test_scrub_snapshot_pseudonymizes_and_clears(tmp_path: Path) -> None:
    snapshot = _build_snapshot(tmp_path)

    summary = scrub_snapshot(snapshot, export_salt=b"unit-test-salt")

    assert summary.preset == "standard"
    assert summary.agents_total == 1
    assert summary.agents_pseudonymized == 1
    assert summary.ack_flags_cleared == 1
    assert summary.file_reservations_removed == 1
    assert summary.agent_links_removed == 1
    assert summary.secrets_replaced >= 2  # subject + body tokens
    assert summary.bodies_redacted == 0
    assert summary.attachments_cleared == 0

    with sqlite3.connect(snapshot) as conn:
        agent_name = conn.execute("SELECT name FROM agents WHERE id = 1").fetchone()[0]
        assert agent_name.startswith("agent-")

        ack_required = conn.execute("SELECT ack_required FROM messages WHERE id = 1").fetchone()[0]
        assert ack_required == 0

        read_ack = conn.execute(
            "SELECT read_ts, ack_ts FROM message_recipients WHERE message_id = 1"
        ).fetchone()
    assert read_ack == (None, None)

    subject, body, attachments = _read_message(snapshot)
    assert "sk-" not in subject
    assert "bearer" not in body.lower()
    assert attachments[0]["type"] == "file"
    assert "download_url" not in attachments[0]


def test_scrub_snapshot_strict_preset(tmp_path: Path) -> None:
    snapshot = _build_snapshot(tmp_path)

    summary = scrub_snapshot(snapshot, preset="strict", export_salt=b"strict-mode")

    assert summary.preset == "strict"
    assert summary.bodies_redacted == 1
    assert summary.attachments_cleared == 1

    with sqlite3.connect(snapshot) as conn:
        body = conn.execute("SELECT body_md FROM messages WHERE id = 1").fetchone()[0]
        attachments_raw = conn.execute("SELECT attachments FROM messages WHERE id = 1").fetchone()[0]
    assert body == "[Message body redacted]"
    assert attachments_raw == "[]"


def test_bundle_attachments_handles_modes(tmp_path: Path) -> None:
    snapshot = _build_snapshot(tmp_path)
    storage_root = tmp_path / "storage"
    base_assets = storage_root / "attachments" / "raw"
    base_assets.mkdir(parents=True, exist_ok=True)

    small = base_assets / "small.txt"
    small.write_bytes(b"tiny data")

    medium = base_assets / "medium.txt"
    medium.write_bytes(b"m" * 256)

    large = base_assets / "large.txt"
    large.write_bytes(b"L" * 512)

    payload = [
        {"type": "file", "path": str(small.relative_to(storage_root)), "media_type": "text/plain"},
        {"type": "file", "path": str(medium.relative_to(storage_root)), "media_type": "text/plain"},
        {"type": "file", "path": str(large.relative_to(storage_root)), "media_type": "text/plain"},
        {"type": "file", "path": "attachments/raw/missing.txt", "media_type": "text/plain"},
    ]

    with sqlite3.connect(snapshot) as conn:
        conn.execute(
            "UPDATE messages SET attachments = ? WHERE id = 1",
            (json.dumps(payload),),
        )
        conn.commit()

    manifest = bundle_attachments(
        snapshot,
        tmp_path / "out",
        storage_root=storage_root,
        inline_threshold=32,
        detach_threshold=400,
    )

    stats = manifest["stats"]
    assert stats == {
        "inline": 1,
        "copied": 1,
        "externalized": 1,
        "missing": 1,
        "bytes_copied": 256,
    }

    _subject, _body, attachments = _read_message(snapshot)
    assert attachments[0]["type"] == "inline"
    assert attachments[1]["type"] == "file"
    path_value = attachments[1]["path"]
    assert isinstance(path_value, str)
    assert path_value.startswith("attachments/")
    assert (tmp_path / "out" / path_value).is_file()
    assert attachments[2]["type"] == "external"
    assert "note" in attachments[2]
    assert attachments[3]["type"] == "missing"

    inline_data = attachments[0]["data_uri"]
    assert isinstance(inline_data, str)
    assert inline_data.startswith("data:text/plain;base64,")
    decoded = base64.b64decode(inline_data.split(",", 1)[1])
    assert decoded == b"tiny data"

    items = manifest["items"]
    assert len(items) == 4
    modes = {item["mode"] for item in items}
    assert modes == {"inline", "file", "external", "missing"}


def test_manifest_snapshot_structure(monkeypatch, tmp_path: Path) -> None:
    snapshot = _build_snapshot(tmp_path)
    storage_root = tmp_path / "env" / "storage"
    storage_root.mkdir(parents=True, exist_ok=True)
    attachments_dir = storage_root / "attachments" / "raw"
    attachments_dir.mkdir(parents=True, exist_ok=True)
    (attachments_dir / "binary.bin").write_bytes(b"binary data")

    monkeypatch.setenv("STORAGE_ROOT", str(storage_root))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{snapshot}")
    monkeypatch.setenv("HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("HTTP_PORT", "8123")
    monkeypatch.setenv("HTTP_PATH", "/mcp/")
    monkeypatch.setenv("APP_ENVIRONMENT", "test")

    output_dir = tmp_path / "bundle"
    runner = CliRunner()
    clear_settings_cache()
    try:
        result = runner.invoke(
            cli_module.app,
            [
                "share",
                "export",
                "--output",
                str(output_dir),
                "--inline-threshold",
                "64",
                "--detach-threshold",
                "1024",
            ],
        )
        assert result.exit_code == 0, result.output

        manifest_path = output_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text())

        assert manifest["schema_version"] == "0.1.0"
        assert manifest["scrub"]["preset"] == "standard"
        assert manifest["scrub"]["agents_total"] == 1
        assert manifest["scrub"]["agents_pseudonymized"] == 1
        assert manifest["scrub"]["ack_flags_cleared"] == 1
        assert manifest["scrub"]["recipients_cleared"] == 1
        assert manifest["scrub"]["file_reservations_removed"] == 1
        assert manifest["scrub"]["agent_links_removed"] == 1
        assert manifest["scrub"]["bodies_redacted"] == 0
        assert manifest["scrub"]["attachments_cleared"] == 0
        assert manifest["scrub"]["attachments_sanitized"] == 1
        assert manifest["scrub"]["secrets_replaced"] >= 2
        assert manifest["project_scope"]["included"] == [
            {"slug": "demo", "human_key": "demo-human"}
        ]
        assert manifest["project_scope"]["removed_count"] == 0
        assert manifest["database"]["chunked"] is False
        detected_hosts = manifest["hosting"].get("detected", [])
        assert isinstance(detected_hosts, list)
        for host_entry in detected_hosts:
            assert {"id", "title", "summary", "signals"}.issubset(host_entry.keys())

        assert set(SCRUB_PRESETS) >= {"standard", "strict"}
    finally:
        clear_settings_cache()


def test_run_share_export_wizard(monkeypatch, tmp_path: Path) -> None:
    db = tmp_path / "wizard.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE projects (id INTEGER PRIMARY KEY, slug TEXT, human_key TEXT)")
        conn.execute("INSERT INTO projects (id, slug, human_key) VALUES (1, 'demo', 'Demo Human')")
        conn.execute("INSERT INTO projects (id, slug, human_key) VALUES (2, 'ops', 'Operations Vault')")

    responses = iter(["demo,ops", "2048", "65536", "1048576", "131072", "strict"])
    monkeypatch.setattr(cli_module.typer, "prompt", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(cli_module.typer, "confirm", lambda *_args, **_kwargs: False)

    result = cli_module._run_share_export_wizard(db, 1024, 32768, 1_048_576, 131_072, "standard")

    assert result["projects"] == ["demo", "ops"]
    assert result["inline_threshold"] == 2048
    assert result["detach_threshold"] == 65536
    assert result["chunk_threshold"] == 1_048_576
    assert result["chunk_size"] == 131_072
    assert result["zip_bundle"] is False
    assert result["scrub_preset"] == "strict"


def test_start_preview_server_serves_content(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "index.html").write_text("hello preview", encoding="utf-8")

    server = cli_module._start_preview_server(bundle, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=2) as response:
            body = response.read().decode("utf-8")
        assert "hello preview" in body
        with urllib.request.urlopen(f"http://{host}:{port}/__preview__/status", timeout=2) as response:
            status_payload = json.loads(response.read().decode("utf-8"))
        assert "signature" in status_payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_share_export_chunking_and_viewer_data(monkeypatch, tmp_path: Path) -> None:
    snapshot = _build_snapshot(tmp_path)
    storage_root = tmp_path / "env" / "storage"
    monkeypatch.setenv("STORAGE_ROOT", str(storage_root))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{snapshot}")
    monkeypatch.setenv("HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("HTTP_PORT", "8765")
    monkeypatch.setenv("HTTP_PATH", "/mcp/")
    monkeypatch.setenv("APP_ENVIRONMENT", "test")

    output_dir = tmp_path / "bundle"
    runner = CliRunner()
    clear_settings_cache()
    result = runner.invoke(
        cli_module.app,
        [
            "share",
            "export",
            "--output",
            str(output_dir),
            "--inline-threshold",
            "32",
            "--detach-threshold",
            "128",
            "--chunk-threshold",
            "1",
            "--chunk-size",
            "2048",
        ],
    )
    assert result.exit_code == 0, result.output

    chunk_config_path = output_dir / "mailbox.sqlite3.config.json"
    assert chunk_config_path.is_file()
    chunk_config = json.loads(chunk_config_path.read_text())
    assert chunk_config["chunk_count"] > 0

    chunks_dir = output_dir / "chunks"
    assert any(chunks_dir.iterdir())

    viewer_data_dir = output_dir / "viewer" / "data"
    messages_json = viewer_data_dir / "messages.json"
    assert messages_json.is_file()
    messages = json.loads(messages_json.read_text())
    assert messages and messages[0]["subject"]

    manifest = json.loads((output_dir / "manifest.json").read_text())
    assert manifest["database"]["chunked"] is True
    assert "viewer" in manifest
    assert manifest["scrub"]["preset"] == "standard"
    clear_settings_cache()


def test_maybe_chunk_database_rejects_zero_chunk_size(tmp_path: Path) -> None:
    snapshot = _build_snapshot(tmp_path)
    output_dir = tmp_path / "bundle"
    output_dir.mkdir()
    with pytest.raises(ShareExportError):
        maybe_chunk_database(
            snapshot,
            output_dir,
            threshold_bytes=1,
            chunk_bytes=0,
        )
