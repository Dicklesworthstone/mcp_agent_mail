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

import mcp_agent_mail.share as share
from mcp_agent_mail import cli as cli_module
from mcp_agent_mail.config import clear_settings_cache
from mcp_agent_mail.share import (
    SCRUB_PRESETS,
    ShareExportError,
    build_materialized_views,
    bundle_attachments,
    finalize_snapshot_for_export,
    maybe_chunk_database,
    scrub_snapshot,
    summarize_snapshot,
)

warnings.filterwarnings("ignore", category=ResourceWarning)

pytestmark = pytest.mark.filterwarnings("ignore:.*ResourceWarning")


def _build_snapshot(tmp_path: Path) -> Path:
    snapshot = tmp_path / "snapshot.sqlite3"
    conn = sqlite3.connect(snapshot)
    try:
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
                sender_id INTEGER,
                thread_id TEXT,
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
            INSERT INTO messages (id, project_id, sender_id, thread_id, subject, body_md, importance, ack_required, created_ts, attachments)
            VALUES (1, 1, 1, 'thread-1', ?, ?, 'normal', 1, '2025-01-01T00:00:00Z', ?)
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
        conn.commit()
    finally:
        conn.close()
    return snapshot


def _read_message(snapshot: Path) -> tuple[str, str, list[dict[str, object]]]:
    conn = sqlite3.connect(snapshot)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT subject, body_md, attachments FROM messages WHERE id = 1").fetchone()
        attachments_raw = row["attachments"]
        attachments = json.loads(attachments_raw) if attachments_raw else []
        return row["subject"], row["body_md"], attachments
    finally:
        conn.close()


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

    conn = sqlite3.connect(snapshot)
    try:
        agent_name = conn.execute("SELECT name FROM agents WHERE id = 1").fetchone()[0]
        assert agent_name.startswith("agent-")

        ack_required = conn.execute("SELECT ack_required FROM messages WHERE id = 1").fetchone()[0]
        assert ack_required == 0

        read_ack = conn.execute(
            "SELECT read_ts, ack_ts FROM message_recipients WHERE message_id = 1"
        ).fetchone()
        assert read_ack == (None, None)
    finally:
        conn.close()

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

    conn = sqlite3.connect(snapshot)
    try:
        body = conn.execute("SELECT body_md FROM messages WHERE id = 1").fetchone()[0]
        attachments_raw = conn.execute("SELECT attachments FROM messages WHERE id = 1").fetchone()[0]
        assert body == "[Message body redacted]"
        assert attachments_raw == "[]"
    finally:
        conn.close()


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

    conn = sqlite3.connect(snapshot)
    try:
        conn.execute(
            "UPDATE messages SET attachments = ? WHERE id = 1",
            (json.dumps(payload),),
        )
        conn.commit()
    finally:
        conn.close()

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


def test_summarize_snapshot(tmp_path: Path) -> None:
    snapshot = _build_snapshot(tmp_path)
    storage_root = tmp_path / "storage"
    attachments_dir = storage_root / "attachments" / "raw"
    attachments_dir.mkdir(parents=True, exist_ok=True)
    (attachments_dir / "inline.txt").write_bytes(b"inline")
    (attachments_dir / "large.bin").write_bytes(b"L" * 1024)

    attachments = [
        {"type": "file", "path": "attachments/raw/inline.txt", "media_type": "text/plain"},
        {"type": "file", "path": "attachments/raw/large.bin", "media_type": "application/octet-stream"},
        {"type": "file", "path": "attachments/raw/missing.bin", "media_type": "application/octet-stream"},
    ]

    conn = sqlite3.connect(snapshot)
    try:
        conn.execute(
            "UPDATE messages SET attachments = ? WHERE id = 1",
            (json.dumps(attachments),),
        )
        conn.commit()
    finally:
        conn.close()

    summary = summarize_snapshot(
        snapshot,
        storage_root=storage_root,
        inline_threshold=64,
        detach_threshold=512,
    )

    assert summary["messages"] == 1
    assert summary["threads"] == 1
    assert summary["projects"]
    stats = summary["attachments"]
    assert stats["total"] == 3
    assert stats["inline_candidates"] == 1
    assert stats["external_candidates"] == 1
    assert stats["missing"] == 1


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
        assert isinstance(manifest["database"].get("fts_enabled"), bool)
        detected_hosts = manifest["hosting"].get("detected", [])
        assert isinstance(detected_hosts, list)
        for host_entry in detected_hosts:
            assert {"id", "title", "summary", "signals"}.issubset(host_entry.keys())

        assert set(SCRUB_PRESETS) >= {"standard", "strict"}
    finally:
        clear_settings_cache()


def test_run_share_export_wizard(monkeypatch, tmp_path: Path) -> None:
    db = tmp_path / "wizard.sqlite3"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE projects (id INTEGER PRIMARY KEY, slug TEXT, human_key TEXT)")
        conn.execute("INSERT INTO projects (id, slug, human_key) VALUES (1, 'demo', 'Demo Human')")
        conn.execute("INSERT INTO projects (id, slug, human_key) VALUES (2, 'ops', 'Operations Vault')")
        conn.commit()
    finally:
        conn.close()

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


def test_share_export_dry_run(monkeypatch, tmp_path: Path) -> None:
    snapshot = _build_snapshot(tmp_path)
    storage_root = tmp_path / "env" / "storage"
    attachments_dir = storage_root / "attachments" / "raw"
    attachments_dir.mkdir(parents=True, exist_ok=True)
    (attachments_dir / "inline.txt").write_bytes(b"data")

    monkeypatch.setenv("STORAGE_ROOT", str(storage_root))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{snapshot}")
    monkeypatch.setenv("HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("HTTP_PORT", "8765")
    monkeypatch.setenv("HTTP_PATH", "/mcp/")
    monkeypatch.setenv("APP_ENVIRONMENT", "test")

    runner = CliRunner()
    clear_settings_cache()
    output_placeholder = tmp_path / "dry-run-out"
    result = runner.invoke(
        cli_module.app,
        [
            "share",
            "export",
            "--output",
            str(output_placeholder),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Dry-Run Summary" in result.output
    assert "Security Checklist" in result.output
    clear_settings_cache()


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
    assert isinstance(manifest["database"].get("fts_enabled"), bool)
    clear_settings_cache()


def test_verify_viewer_vendor_assets():
    # Should not raise when bundled vendor assets match recorded checksums.
    share._verify_viewer_vendor_assets()


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


def test_sign_and_verify_manifest(tmp_path: Path) -> None:
    """Test Ed25519 manifest signing and verification flow."""
    pytest.importorskip("nacl", reason="PyNaCl required for signing tests")

    # Create a test manifest
    manifest_path = tmp_path / "manifest.json"
    manifest_data = {"version": "1.0", "test": "data"}
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    # Generate signing key (32-byte seed)
    signing_key_path = tmp_path / "signing.key"
    signing_key_path.write_bytes(b"A" * 32)

    # Sign the manifest
    signature_info = share.sign_manifest(
        manifest_path,
        signing_key_path,
        tmp_path,
    )

    assert signature_info["algorithm"] == "ed25519"
    assert "signature" in signature_info
    assert "public_key" in signature_info
    assert "manifest_sha256" in signature_info

    # Verify signature file was created
    sig_path = tmp_path / "manifest.sig.json"
    assert sig_path.exists()

    # Verify the bundle (should pass)
    result = share.verify_bundle(tmp_path)
    assert result["signature_checked"] is True
    assert result["signature_verified"] is True

    # Verify with explicit public key (should pass)
    result = share.verify_bundle(tmp_path, public_key=signature_info["public_key"])
    assert result["signature_verified"] is True

    # Tamper with manifest and verify (should fail)
    manifest_path.write_text(json.dumps({"tampered": True}), encoding="utf-8")
    with pytest.raises(ShareExportError, match="signature verification failed"):
        share.verify_bundle(tmp_path)


def test_verify_bundle_without_signature(tmp_path: Path) -> None:
    """Test bundle verification when no signature is present."""
    # Create minimal manifest
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"version": "1.0"}), encoding="utf-8")

    # Verify should succeed but report no signature
    result = share.verify_bundle(tmp_path)
    assert result["signature_checked"] is False
    assert result["signature_verified"] is False


def test_verify_bundle_with_sri(tmp_path: Path) -> None:
    """Test SRI hash verification in bundle."""
    # Create manifest with SRI entries
    viewer_dir = tmp_path / "viewer"
    viewer_dir.mkdir()

    js_file = viewer_dir / "test.js"
    js_file.write_text("console.log('test');", encoding="utf-8")

    # Compute SRI hash
    sri_hash = share._compute_sri(js_file)

    manifest_data = {
        "version": "1.0",
        "viewer": {
            "sri": {
                "viewer/test.js": sri_hash
            }
        }
    }

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    # Verification should pass
    result = share.verify_bundle(tmp_path)
    assert result["sri_checked"] is True

    # Tamper with JS file
    js_file.write_text("console.log('hacked');", encoding="utf-8")

    # Verification should fail
    with pytest.raises(ShareExportError, match="SRI mismatch"):
        share.verify_bundle(tmp_path)


def test_verify_bundle_missing_sri_asset(tmp_path: Path) -> None:
    """Test verification fails when SRI asset is missing."""
    manifest_data = {
        "version": "1.0",
        "viewer": {
            "sri": {
                "viewer/missing.js": "sha256-abc123"
            }
        }
    }

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    with pytest.raises(ShareExportError, match="Missing asset for SRI entry"):
        share.verify_bundle(tmp_path)


def test_decrypt_with_age_requires_age_binary(tmp_path: Path, monkeypatch) -> None:
    """Test that decrypt_with_age fails gracefully when age is not installed."""
    monkeypatch.setattr(share.shutil, "which", lambda x: None)

    encrypted = tmp_path / "bundle.age"
    encrypted.write_bytes(b"encrypted data")
    output = tmp_path / "decrypted"

    with pytest.raises(ShareExportError, match="`age` CLI not found"):
        share.decrypt_with_age(encrypted, output, identity=tmp_path / "key.txt")


def test_decrypt_with_age_validation(tmp_path: Path, monkeypatch) -> None:
    """Test decrypt_with_age input validation."""
    # Mock age binary as available for validation tests
    monkeypatch.setattr(share.shutil, "which", lambda x: "/usr/bin/age" if x == "age" else None)

    encrypted = tmp_path / "bundle.age"
    encrypted.write_bytes(b"data")
    output = tmp_path / "out"
    identity = tmp_path / "identity.txt"
    identity.write_text("AGE-SECRET-KEY-1...", encoding="utf-8")

    # Can't provide both identity and passphrase
    with pytest.raises(ShareExportError, match="either an identity file or a passphrase"):
        share.decrypt_with_age(encrypted, output, identity=identity, passphrase="secret")

    # Must provide at least one
    with pytest.raises(ShareExportError, match="requires --identity or --passphrase"):
        share.decrypt_with_age(encrypted, output)

    # Identity file must exist
    missing_identity = tmp_path / "nonexistent.txt"
    with pytest.raises(ShareExportError, match="Identity file not found"):
        share.decrypt_with_age(encrypted, output, identity=missing_identity)


def test_sri_computation(tmp_path: Path) -> None:
    """Test SRI hash computation."""
    test_file = tmp_path / "test.js"
    test_file.write_text("test content", encoding="utf-8")

    sri = share._compute_sri(test_file)

    # Should start with sha256-
    assert sri.startswith("sha256-")

    # Should be base64 encoded (typically 44+ chars including prefix)
    assert len(sri) > 40

    # Should be deterministic
    sri2 = share._compute_sri(test_file)
    assert sri == sri2


def test_build_viewer_sri(tmp_path: Path) -> None:
    """Test building SRI map for viewer assets."""
    viewer_dir = tmp_path / "viewer"
    vendor_dir = viewer_dir / "vendor"
    vendor_dir.mkdir(parents=True)

    # Create test assets
    (viewer_dir / "viewer.js").write_text("js code", encoding="utf-8")
    (viewer_dir / "styles.css").write_text("css code", encoding="utf-8")
    (vendor_dir / "lib.wasm").write_bytes(b"wasm binary")
    (viewer_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    (viewer_dir / "README.txt").write_text("readme", encoding="utf-8")

    sri_map = share._build_viewer_sri(tmp_path)

    # Should include .js, .css, .wasm files
    assert "viewer/viewer.js" in sri_map
    assert "viewer/styles.css" in sri_map
    assert "viewer/vendor/lib.wasm" in sri_map

    # Should NOT include .html or .txt
    assert "viewer/index.html" not in sri_map
    assert "viewer/README.txt" not in sri_map

    # All values should be SRI hashes
    for _path, sri in sri_map.items():
        assert sri.startswith("sha256-")


def test_cli_verify_command(tmp_path: Path) -> None:
    """Test the CLI verify command."""
    # Create a bundle with manifest
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest = bundle / "manifest.json"
    manifest.write_text(json.dumps({"version": "1.0"}), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        ["share", "verify", str(bundle)],
    )

    assert result.exit_code == 0
    assert "Bundle verification passed" in result.output
    assert "SRI checked: False" in result.output
    assert "Signature checked: False" in result.output


def test_cli_verify_command_missing_bundle(tmp_path: Path) -> None:
    """Test verify command with non-existent bundle."""
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        ["share", "verify", str(tmp_path / "nonexistent")],
    )

    assert result.exit_code == 1
    assert "Bundle directory not found" in result.output


def test_cli_verify_command_not_directory(tmp_path: Path) -> None:
    """Test verify command with file instead of directory."""
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("test", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        ["share", "verify", str(not_a_dir)],
    )

    assert result.exit_code == 1
    assert "must be a directory" in result.output


def test_cli_decrypt_command_auto_output(tmp_path: Path) -> None:
    """Test decrypt command with auto-generated output path."""
    encrypted = tmp_path / "bundle.zip.age"
    encrypted.write_bytes(b"fake encrypted data")

    runner = CliRunner()
    # Should fail because age is not installed, but validates CLI parameter handling
    result = runner.invoke(
        cli_module.app,
        ["share", "decrypt", str(encrypted), "--identity", str(tmp_path / "key.txt")],
    )

    # Will fail due to missing age binary, but should not fail due to missing --output
    assert result.exit_code == 1
    # Error should be about age, not about missing output parameter
    assert "age" in result.output.lower() or "CLI not found" in result.output


def test_cli_decrypt_command_not_file(tmp_path: Path) -> None:
    """Test decrypt command with directory instead of file."""
    not_a_file = tmp_path / "directory"
    not_a_file.mkdir()

    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        ["share", "decrypt", str(not_a_file), "--identity", str(tmp_path / "key.txt")],
    )

    assert result.exit_code == 1
    assert "must be a file" in result.output


def test_encrypt_bundle_requires_age_binary(tmp_path: Path, monkeypatch) -> None:
    """Test that encrypt_bundle fails gracefully when age is not installed."""
    monkeypatch.setattr(share.shutil, "which", lambda x: None)

    bundle = tmp_path / "bundle.zip"
    bundle.write_bytes(b"test data")

    with pytest.raises(ShareExportError, match="`age` CLI not found"):
        share.encrypt_bundle(bundle, ["age1recipient..."])


def test_encrypt_bundle_with_invalid_recipient(tmp_path: Path, monkeypatch) -> None:
    """Test encryption failure with invalid recipient format."""
    # Mock age binary to test actual age failures

    bundle = tmp_path / "bundle.zip"
    bundle.write_bytes(b"test data")

    def mock_run(*args, **kwargs):
        # Simulate age returning error for invalid recipient
        class Result:
            returncode = 1
            stderr = "Error: invalid recipient format: notavalidrecipient"

        return Result()

    monkeypatch.setattr(share.subprocess, "run", mock_run)
    monkeypatch.setattr(share.shutil, "which", lambda x: "/usr/bin/age" if x == "age" else None)

    with pytest.raises(ShareExportError, match=r"age encryption failed.*invalid recipient"):
        share.encrypt_bundle(bundle, ["notavalidrecipient"])


def test_encrypt_bundle_returns_none_for_empty_recipients(tmp_path: Path) -> None:
    """Test that encrypt_bundle returns None when no recipients provided."""
    bundle = tmp_path / "bundle.zip"
    bundle.write_bytes(b"test data")

    result = share.encrypt_bundle(bundle, [])
    assert result is None


def test_verify_bundle_with_tampered_signature(tmp_path: Path) -> None:
    """Test signature verification fails when signature doesn't match."""
    pytest.importorskip("nacl", reason="PyNaCl required for signing tests")

    # Create a valid signed bundle
    manifest_path = tmp_path / "manifest.json"
    manifest_data = {"version": "1.0", "test": "data"}
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    signing_key_path = tmp_path / "signing.key"
    signing_key_path.write_bytes(b"A" * 32)

    share.sign_manifest(manifest_path, signing_key_path, tmp_path)

    # Tamper with the manifest after signing (this invalidates the signature)
    manifest_data["tampered"] = True
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    # Verification should fail because manifest changed
    with pytest.raises(ShareExportError, match="signature verification failed"):
        share.verify_bundle(tmp_path)


def test_verify_bundle_with_missing_signature_file(tmp_path: Path) -> None:
    """Test verification when signature file is present in manifest but missing from disk."""
    pytest.importorskip("nacl", reason="PyNaCl required for signing tests")

    # Create manifest with signature claim
    manifest_path = tmp_path / "manifest.json"
    manifest_data = {"version": "1.0", "signed": True}
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    # Create signature file
    sig_path = tmp_path / "manifest.sig.json"
    sig_data = {
        "algorithm": "ed25519",
        "signature": "dGVzdA==",  # base64 "test"
        "public_key": "dGVzdA==",
        "manifest_sha256": "abc123",
    }
    sig_path.write_text(json.dumps(sig_data), encoding="utf-8")

    # Delete signature file after manifest references it
    sig_path.unlink()

    # Verification should handle missing signature gracefully
    result = share.verify_bundle(tmp_path)
    assert result["signature_checked"] is False


def test_verify_bundle_with_corrupted_signature_json(tmp_path: Path) -> None:
    """Test verification handles corrupted signature JSON."""
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"version": "1.0"}), encoding="utf-8")

    sig_path = tmp_path / "manifest.sig.json"
    sig_path.write_text("{ invalid json", encoding="utf-8")

    # Should handle gracefully
    with pytest.raises(ShareExportError, match="not valid JSON"):
        share.verify_bundle(tmp_path)


def test_decrypt_encrypted_file_not_exist(tmp_path: Path, monkeypatch) -> None:
    """Test decrypt handles non-existent encrypted file."""
    monkeypatch.setattr(share.shutil, "which", lambda x: "/usr/bin/age" if x == "age" else None)

    encrypted = tmp_path / "nonexistent.age"
    output = tmp_path / "out"
    identity = tmp_path / "key.txt"
    identity.write_text("AGE-SECRET-KEY-1...", encoding="utf-8")

    with pytest.raises(ShareExportError, match="Encrypted file not found"):
        share.decrypt_with_age(encrypted, output, identity=identity)


def test_sign_manifest_with_invalid_key_length(tmp_path: Path) -> None:
    """Test signing fails with key that's not 32 or 64 bytes."""
    pytest.importorskip("nacl", reason="PyNaCl required for signing tests")

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"version": "1.0"}), encoding="utf-8")

    signing_key_path = tmp_path / "bad_key.key"
    signing_key_path.write_bytes(b"A" * 16)  # Invalid: only 16 bytes

    with pytest.raises(ShareExportError, match="32-byte seed or 64-byte expanded"):
        share.sign_manifest(manifest_path, signing_key_path, tmp_path)


def test_sign_manifest_with_directory_instead_of_file(tmp_path: Path) -> None:
    """Test signing fails when manifest path is a directory."""
    pytest.importorskip("nacl", reason="PyNaCl required for signing tests")

    manifest_dir = tmp_path / "manifest_dir"
    manifest_dir.mkdir()

    signing_key_path = tmp_path / "key.key"
    signing_key_path.write_bytes(b"A" * 32)

    with pytest.raises(ShareExportError, match="Manifest path must be a file"):
        share.sign_manifest(manifest_dir, signing_key_path, tmp_path)


def test_sign_manifest_with_missing_manifest(tmp_path: Path) -> None:
    """Test signing fails when manifest doesn't exist."""
    pytest.importorskip("nacl", reason="PyNaCl required for signing tests")

    manifest_path = tmp_path / "missing.json"
    signing_key_path = tmp_path / "key.key"
    signing_key_path.write_bytes(b"A" * 32)

    with pytest.raises(ShareExportError, match="Manifest file not found"):
        share.sign_manifest(manifest_path, signing_key_path, tmp_path)


def test_verify_bundle_with_sri_and_signature_both_valid(tmp_path: Path) -> None:
    """Test verification succeeds when both SRI and signature are valid."""
    pytest.importorskip("nacl", reason="PyNaCl required for signing tests")

    # Create viewer file
    viewer_dir = tmp_path / "viewer"
    viewer_dir.mkdir()
    js_file = viewer_dir / "test.js"
    js_file.write_text("console.log('test');", encoding="utf-8")

    # Compute SRI
    sri_hash = share._compute_sri(js_file)

    # Create manifest with SRI
    manifest_data = {
        "version": "1.0",
        "viewer": {"sri": {"viewer/test.js": sri_hash}},
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    # Sign manifest
    signing_key_path = tmp_path / "key.key"
    signing_key_path.write_bytes(b"A" * 32)
    share.sign_manifest(manifest_path, signing_key_path, tmp_path)

    # Verification should pass
    result = share.verify_bundle(tmp_path)
    assert result["sri_checked"] is True
    assert result["signature_checked"] is True
    assert result["signature_verified"] is True


def test_finalize_snapshot_sql_hygiene(tmp_path: Path) -> None:
    """Test SQL hygiene optimizations from finalize_snapshot_for_export."""
    # Create a test database with some data
    snapshot = tmp_path / "snapshot.sqlite3"
    conn = sqlite3.connect(snapshot)
    try:
        # Create tables with data
        conn.executescript(
            """
            CREATE TABLE test_data (id INTEGER PRIMARY KEY, data TEXT);
            INSERT INTO test_data (data) VALUES ('test1'), ('test2'), ('test3');
            """
        )
        conn.commit()
    finally:
        conn.close()

    # Get initial file size
    initial_size = snapshot.stat().st_size

    # Verify WAL mode might exist (default for some SQLite versions)
    conn = sqlite3.connect(snapshot)
    try:
        # Create some operations that might leave WAL files
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT INTO test_data (data) VALUES ('wal-test')")
        conn.commit()
    finally:
        conn.close()

    # Apply SQL hygiene optimizations
    finalize_snapshot_for_export(snapshot)

    # Verify journal mode is DELETE
    conn = sqlite3.connect(snapshot)
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert journal_mode.lower() == "delete", f"Expected DELETE mode, got {journal_mode}"

        # Verify page size is 1024
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        assert page_size == 1024, f"Expected page size 1024, got {page_size}"

        # Verify data integrity (VACUUM shouldn't lose data)
        row_count = conn.execute("SELECT COUNT(*) FROM test_data").fetchone()[0]
        assert row_count == 4, f"Expected 4 rows after finalization, got {row_count}"
    finally:
        conn.close()

    # Verify no WAL or SHM files exist
    wal_file = Path(f"{snapshot}-wal")
    shm_file = Path(f"{snapshot}-shm")
    assert not wal_file.exists(), "WAL file should not exist after finalization"
    assert not shm_file.exists(), "SHM file should not exist after finalization"

    # Note: File size may increase or decrease depending on initial page size
    # and fragmentation, so we just verify it's reasonable (not empty, not corrupted)
    final_size = snapshot.stat().st_size
    assert final_size > 0, "Snapshot should not be empty after finalization"
    assert final_size < initial_size * 2, "Snapshot size should be reasonable"


def test_build_materialized_views(tmp_path: Path) -> None:
    """Test materialized view creation for httpvfs performance optimization."""
    # Create a test database with messages and attachments
    snapshot = tmp_path / "snapshot.sqlite3"
    conn = sqlite3.connect(snapshot)
    try:
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
                sender_id INTEGER,
                thread_id TEXT,
                subject TEXT,
                body_md TEXT,
                importance TEXT,
                ack_required INTEGER,
                created_ts TEXT,
                attachments TEXT
            );

            INSERT INTO projects (id, slug, human_key) VALUES (1, 'demo', 'demo-key');
            INSERT INTO agents (id, project_id, name) VALUES (1, 1, 'AgentAlice');
            INSERT INTO agents (id, project_id, name) VALUES (2, 1, 'AgentBob');

            INSERT INTO messages (id, project_id, sender_id, thread_id, subject, body_md, importance, ack_required, created_ts, attachments)
            VALUES (
                1, 1, 1, 'thread-1', 'Test Message 1', 'Body 1', 'high', 1, '2025-01-01T00:00:00Z',
                '[{"type":"file","path":"test.txt","size_bytes":100,"media_type":"text/plain"}]'
            );

            INSERT INTO messages (id, project_id, sender_id, thread_id, subject, body_md, importance, ack_required, created_ts, attachments)
            VALUES (
                2, 1, 2, 'thread-1', 'Test Message 2', 'Body 2', 'normal', 0, '2025-01-02T00:00:00Z',
                '[{"type":"inline","data_uri":"data:text/plain;base64,dGVzdA=="},{"type":"file","path":"doc.pdf","size_bytes":500,"media_type":"application/pdf"}]'
            );

            INSERT INTO messages (id, project_id, sender_id, thread_id, subject, body_md, importance, ack_required, created_ts, attachments)
            VALUES (
                3, 1, 1, 'thread-2', 'Test Message 3', 'Body 3', 'normal', 0, '2025-01-03T00:00:00Z', '[]'
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    # Build materialized views
    build_materialized_views(snapshot)

    # Verify message_overview_mv was created
    conn = sqlite3.connect(snapshot)
    try:
        # Check table exists
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='message_overview_mv'"
        ).fetchall()
        assert len(tables) == 1, "message_overview_mv should exist"

        # Check data is populated
        rows = conn.execute("SELECT * FROM message_overview_mv ORDER BY id").fetchall()
        assert len(rows) == 3, f"Expected 3 rows in message_overview_mv, got {len(rows)}"

        # Verify columns
        row = conn.execute("SELECT * FROM message_overview_mv WHERE id = 1").fetchone()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM message_overview_mv WHERE id = 1").fetchone()
        assert row["sender_name"] == "AgentAlice"
        assert row["subject"] == "Test Message 1"
        assert row["importance"] == "high"
        assert row["attachment_count"] == 1

        # Verify indexes exist
        indexes = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='index' AND tbl_name='message_overview_mv'
            AND name LIKE 'idx_msg_overview_%'
            """
        ).fetchall()
        assert len(indexes) >= 4, f"Expected at least 4 covering indexes, got {len(indexes)}"

        # Check attachments_by_message_mv was created
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='attachments_by_message_mv'"
        ).fetchall()
        assert len(tables) == 1, "attachments_by_message_mv should exist"

        # Check attachment data is flattened
        attach_rows = conn.execute("SELECT * FROM attachments_by_message_mv ORDER BY message_id").fetchall()
        # Message 1: 1 attachment, Message 2: 2 attachments, Message 3: 0 attachments
        assert len(attach_rows) == 3, f"Expected 3 flattened attachment rows, got {len(attach_rows)}"

        # Verify attachment details
        attach_row = conn.execute(
            "SELECT * FROM attachments_by_message_mv WHERE message_id = 1"
        ).fetchone()
        conn.row_factory = sqlite3.Row
        attach_row = conn.execute(
            "SELECT * FROM attachments_by_message_mv WHERE message_id = 1"
        ).fetchone()
        assert attach_row["attachment_type"] == "file"
        assert attach_row["media_type"] == "text/plain"
        assert attach_row["path"] == "test.txt"
        assert attach_row["size_bytes"] == 100

        # Verify attachment indexes exist
        attach_indexes = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='index' AND tbl_name='attachments_by_message_mv'
            AND name LIKE 'idx_attach_%'
            """
        ).fetchall()
        assert len(attach_indexes) >= 3, f"Expected at least 3 attachment indexes, got {len(attach_indexes)}"
    finally:
        conn.close()
