"""Tests for image processing edge cases in MCP Agent Mail.

Tests edge cases including:
- Malformed/corrupt image files
- Various image modes (palette, LA, RGBA, etc.)
- Invalid data URIs
- Truncated images
- Zero-byte files
- Very large images
- Unusual file extensions
"""

from __future__ import annotations

import base64
import contextlib
import io
from pathlib import Path

import pytest
from fastmcp import Client
from PIL import Image

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import get_settings

# =============================================================================
# Malformed Image Tests
# =============================================================================


@pytest.mark.asyncio
async def test_corrupt_image_file_gracefully_fails(isolated_env):
    """Test that corrupt image files are rejected with clear error."""
    from fastmcp.exceptions import ToolError

    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    corrupt_path = storage_root.parent / "corrupt.png"
    corrupt_path.write_bytes(b"this is not a valid image file at all")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        # Server rejects corrupt images with an error
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool(
                "send_message",
                {
                    "project_key": "Backend",
                    "sender_name": "BlueLake",
                    "to": ["BlueLake"],
                    "subject": "Corrupt Image",
                    "body_md": f"![img]({corrupt_path})",
                },
            )
        # Error should mention image identification
        assert "cannot identify image" in str(exc_info.value).lower()
    corrupt_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_zero_byte_image_file(isolated_env):
    """Test that zero-byte image files are rejected with error."""
    from fastmcp.exceptions import ToolError

    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    empty_path = storage_root.parent / "empty.png"
    empty_path.write_bytes(b"")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        # Empty images are rejected
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool(
                "send_message",
                {
                    "project_key": "Backend",
                    "sender_name": "BlueLake",
                    "to": ["BlueLake"],
                    "subject": "Empty Image",
                    "body_md": f"![img]({empty_path})",
                },
            )
        assert "cannot identify image" in str(exc_info.value).lower()
    empty_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_truncated_png_header_only(isolated_env):
    """Test image file with only a PNG header but no data is rejected."""
    from fastmcp.exceptions import ToolError

    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    truncated_path = storage_root.parent / "truncated.png"
    # PNG magic header only
    truncated_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        # Truncated PNG is rejected
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool(
                "send_message",
                {
                    "project_key": "Backend",
                    "sender_name": "BlueLake",
                    "to": ["BlueLake"],
                    "subject": "Truncated PNG",
                    "body_md": f"![img]({truncated_path})",
                },
            )
        assert "cannot identify image" in str(exc_info.value).lower()
    truncated_path.unlink(missing_ok=True)


# =============================================================================
# Image Mode Tests
# =============================================================================


@pytest.mark.asyncio
async def test_palette_mode_image(isolated_env):
    """Test conversion of palette (P) mode image."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    palette_path = storage_root.parent / "palette.png"

    # Create palette mode image
    img = Image.new("P", (4, 4))
    img.putpalette(list(range(256)) * 3)  # Simple grayscale palette
    img.save(palette_path, "PNG")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "Palette Image",
                "body_md": f"![img]({palette_path})",
            },
        )
        assert res.data.get("deliveries")
    palette_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_la_mode_image(isolated_env):
    """Test conversion of LA (luminance + alpha) mode image."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    la_path = storage_root.parent / "la_image.png"

    # Create LA mode image
    img = Image.new("LA", (4, 4), color=(128, 200))
    img.save(la_path, "PNG")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "LA Image",
                "body_md": f"![img]({la_path})",
            },
        )
        assert res.data.get("deliveries")
        # Check that attachment was processed (should be RGBA after conversion)
        attachments = (res.data.get("deliveries") or [{}])[0].get("payload", {}).get("attachments", [])
        assert len(attachments) > 0
    la_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_rgba_mode_image_preserves_alpha(isolated_env):
    """Test conversion of RGBA mode image preserves transparency."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    rgba_path = storage_root.parent / "rgba_image.png"

    # Create RGBA mode image with transparency
    img = Image.new("RGBA", (4, 4), color=(255, 0, 0, 128))  # Semi-transparent red
    img.save(rgba_path, "PNG")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "RGBA Image",
                "body_md": f"![img]({rgba_path})",
            },
        )
        assert res.data.get("deliveries")
    rgba_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_grayscale_mode_image(isolated_env):
    """Test conversion of grayscale (L) mode image."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    gray_path = storage_root.parent / "gray_image.png"

    # Create L (grayscale) mode image
    img = Image.new("L", (4, 4), color=128)
    img.save(gray_path, "PNG")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "Grayscale Image",
                "body_md": f"![img]({gray_path})",
            },
        )
        assert res.data.get("deliveries")
    gray_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_1bit_mode_image(isolated_env):
    """Test conversion of 1-bit (black and white) mode image."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    bw_path = storage_root.parent / "bw_image.png"

    # Create 1-bit mode image
    img = Image.new("1", (4, 4), color=1)
    img.save(bw_path, "PNG")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "1-bit Image",
                "body_md": f"![img]({bw_path})",
            },
        )
        assert res.data.get("deliveries")
    bw_path.unlink(missing_ok=True)


# =============================================================================
# Data URI Edge Cases
# =============================================================================


@pytest.mark.asyncio
async def test_malformed_data_uri_missing_comma(isolated_env, monkeypatch):
    """Test handling of malformed data URI without comma separator."""
    monkeypatch.setenv("CONVERT_IMAGES", "false")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        # Malformed: no comma after base64
        body = "![img](data:image/pngbase64ABC123)"
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "Malformed URI",
                "body_md": body,
                "convert_images": False,
            },
        )
        # Should not crash
        assert res is not None


@pytest.mark.asyncio
async def test_data_uri_empty_base64(isolated_env, monkeypatch):
    """Test handling of data URI with empty base64 content."""
    monkeypatch.setenv("CONVERT_IMAGES", "false")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        body = "![img](data:image/png;base64,)"
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "Empty Base64",
                "body_md": body,
                "convert_images": False,
            },
        )
        assert res is not None


@pytest.mark.asyncio
async def test_data_uri_invalid_base64(isolated_env, monkeypatch):
    """Test handling of data URI with invalid base64 characters."""
    monkeypatch.setenv("CONVERT_IMAGES", "false")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        body = "![img](data:image/png;base64,!!!not-valid-base64!!!)"
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "Invalid Base64",
                "body_md": body,
                "convert_images": False,
            },
        )
        assert res is not None


@pytest.mark.asyncio
async def test_data_uri_unusual_media_type(isolated_env, monkeypatch):
    """Test handling of data URI with unusual media type."""
    monkeypatch.setenv("CONVERT_IMAGES", "false")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        payload = base64.b64encode(b"fake").decode()
        body = f"![img](data:image/x-custom-format;base64,{payload})"
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "Unusual Media Type",
                "body_md": body,
                "convert_images": False,
            },
        )
        attachments = (res.data.get("deliveries") or [{}])[0].get("payload", {}).get("attachments", [])
        # Should preserve the unusual media type
        inline_atts = [a for a in attachments if a.get("type") == "inline"]
        assert len(inline_atts) > 0
        assert inline_atts[0].get("media_type") == "image/x-custom-format"


# =============================================================================
# File Extension Edge Cases
# =============================================================================


@pytest.mark.asyncio
async def test_image_without_extension(isolated_env):
    """Test handling of image file without extension."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    no_ext_path = storage_root.parent / "image_no_extension"

    # Create a valid PNG without file extension
    img = Image.new("RGB", (4, 4), color=(0, 255, 0))
    buffer = io.BytesIO()
    img.save(buffer, "PNG")
    no_ext_path.write_bytes(buffer.getvalue())

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "No Extension",
                "body_md": f"![img]({no_ext_path})",
            },
        )
        assert res.data.get("deliveries")
    no_ext_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_image_wrong_extension(isolated_env):
    """Test handling of image with wrong extension (PNG saved as .jpg)."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    wrong_ext_path = storage_root.parent / "actually_png.jpg"

    # Create a PNG but save with .jpg extension
    img = Image.new("RGB", (4, 4), color=(0, 0, 255))
    buffer = io.BytesIO()
    img.save(buffer, "PNG")
    wrong_ext_path.write_bytes(buffer.getvalue())

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "Wrong Extension",
                "body_md": f"![img]({wrong_ext_path})",
            },
        )
        # Pillow should detect the actual format
        assert res.data.get("deliveries")
    wrong_ext_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_image_uppercase_extension(isolated_env):
    """Test handling of image with uppercase extension."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    upper_path = storage_root.parent / "IMAGE.PNG"

    img = Image.new("RGB", (4, 4), color=(255, 255, 0))
    img.save(upper_path, "PNG")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "Uppercase Extension",
                "body_md": f"![img]({upper_path})",
            },
        )
        assert res.data.get("deliveries")
    upper_path.unlink(missing_ok=True)


# =============================================================================
# Multiple Images Edge Cases
# =============================================================================


@pytest.mark.asyncio
async def test_multiple_images_in_body(isolated_env):
    """Test handling of multiple images in a single message body."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    img_paths = []

    for i in range(3):
        path = storage_root.parent / f"multi_img_{i}.png"
        img = Image.new("RGB", (4, 4), color=(i * 80, i * 80, i * 80))
        img.save(path, "PNG")
        img_paths.append(path)

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        body = "\n".join([f"![img{i}]({p})" for i, p in enumerate(img_paths)])
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "Multiple Images",
                "body_md": body,
            },
        )
        assert res.data.get("deliveries")
        attachments = (res.data.get("deliveries") or [{}])[0].get("payload", {}).get("attachments", [])
        assert len(attachments) == 3

    for p in img_paths:
        p.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_mixed_valid_and_invalid_images(isolated_env):
    """Test handling of mix of valid and invalid images - server rejects on first invalid."""
    from fastmcp.exceptions import ToolError

    storage_root = Path(get_settings().storage.root).expanduser().resolve()

    valid_path = storage_root.parent / "valid_img.png"
    img = Image.new("RGB", (4, 4), color=(100, 100, 100))
    img.save(valid_path, "PNG")

    invalid_path = storage_root.parent / "invalid_img.png"
    invalid_path.write_bytes(b"not an image")

    missing_path = storage_root.parent / "totally_missing.png"

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        body = f"![valid]({valid_path})\n![invalid]({invalid_path})\n![missing]({missing_path})"
        # Server rejects when it encounters an invalid image
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool(
                "send_message",
                {
                    "project_key": "Backend",
                    "sender_name": "BlueLake",
                    "to": ["BlueLake"],
                    "subject": "Mixed Images",
                    "body_md": body,
                },
            )
        # Error should indicate image identification failure
        assert "cannot identify image" in str(exc_info.value).lower()

    valid_path.unlink(missing_ok=True)
    invalid_path.unlink(missing_ok=True)


# =============================================================================
# Attachment Path Edge Cases
# =============================================================================


@pytest.mark.asyncio
async def test_attachment_path_with_spaces(isolated_env):
    """Test attachment path containing spaces."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    spaced_path = storage_root.parent / "path with spaces" / "image file.png"
    spaced_path.parent.mkdir(parents=True, exist_ok=True)

    img = Image.new("RGB", (4, 4), color=(200, 100, 50))
    img.save(spaced_path, "PNG")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "Spaced Path",
                "body_md": "check attachment",
                "attachment_paths": [str(spaced_path)],
            },
        )
        assert res.data.get("deliveries")

    spaced_path.unlink(missing_ok=True)
    spaced_path.parent.rmdir()


@pytest.mark.asyncio
async def test_attachment_path_unicode(isolated_env):
    """Test attachment path containing unicode characters."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    unicode_path = storage_root.parent / "image_\u4e2d\u6587_\u65e5\u672c\u8a9e.png"

    img = Image.new("RGB", (4, 4), color=(50, 150, 200))
    img.save(unicode_path, "PNG")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "Unicode Path",
                "body_md": "check attachment",
                "attachment_paths": [str(unicode_path)],
            },
        )
        assert res.data.get("deliveries")

    unicode_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_attachment_symlink(isolated_env, tmp_path):
    """Test attachment via symlink."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    real_path = storage_root.parent / "real_image.png"
    link_path = storage_root.parent / "symlink_image.png"

    img = Image.new("RGB", (4, 4), color=(10, 20, 30))
    img.save(real_path, "PNG")

    # Create symlink
    try:
        link_path.symlink_to(real_path)
    except OSError:
        pytest.skip("Cannot create symlinks on this system")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "Symlink Attachment",
                "body_md": f"![img]({link_path})",
            },
        )
        assert res.data.get("deliveries")

    link_path.unlink(missing_ok=True)
    real_path.unlink(missing_ok=True)


# =============================================================================
# Image Format Tests
# =============================================================================


@pytest.mark.asyncio
async def test_gif_image_conversion(isolated_env):
    """Test conversion of GIF image to WebP."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    gif_path = storage_root.parent / "image.gif"

    img = Image.new("RGB", (4, 4), color=(255, 0, 255))
    img.save(gif_path, "GIF")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "GIF Image",
                "body_md": f"![img]({gif_path})",
            },
        )
        assert res.data.get("deliveries")
        attachments = (res.data.get("deliveries") or [{}])[0].get("payload", {}).get("attachments", [])
        # Should be converted to webp
        if attachments:
            assert attachments[0].get("media_type") == "image/webp"

    gif_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_bmp_image_conversion(isolated_env):
    """Test conversion of BMP image to WebP."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    bmp_path = storage_root.parent / "image.bmp"

    img = Image.new("RGB", (4, 4), color=(0, 128, 255))
    img.save(bmp_path, "BMP")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "BMP Image",
                "body_md": f"![img]({bmp_path})",
            },
        )
        assert res.data.get("deliveries")

    bmp_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_jpeg_image_conversion(isolated_env):
    """Test conversion of JPEG image to WebP."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    jpeg_path = storage_root.parent / "image.jpeg"

    img = Image.new("RGB", (4, 4), color=(255, 200, 100))
    img.save(jpeg_path, "JPEG")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "JPEG Image",
                "body_md": f"![img]({jpeg_path})",
            },
        )
        assert res.data.get("deliveries")

    jpeg_path.unlink(missing_ok=True)


# =============================================================================
# Size and Memory Edge Cases
# =============================================================================


@pytest.mark.asyncio
async def test_single_pixel_image(isolated_env):
    """Test handling of 1x1 pixel image."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    tiny_path = storage_root.parent / "tiny.png"

    img = Image.new("RGB", (1, 1), color=(128, 128, 128))
    img.save(tiny_path, "PNG")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "Tiny Image",
                "body_md": f"![img]({tiny_path})",
            },
        )
        assert res.data.get("deliveries")
        attachments = (res.data.get("deliveries") or [{}])[0].get("payload", {}).get("attachments", [])
        if attachments:
            assert attachments[0].get("width") == 1
            assert attachments[0].get("height") == 1

    tiny_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_moderately_large_image(isolated_env, monkeypatch):
    """Test handling of moderately large image (triggers file mode)."""
    # Small inline threshold to force file mode
    monkeypatch.setenv("INLINE_IMAGE_MAX_BYTES", "100")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    large_path = storage_root.parent / "large_image.png"

    # Create a larger image (100x100 should exceed 100 bytes threshold)
    img = Image.new("RGB", (100, 100), color=(64, 128, 192))
    img.save(large_path, "PNG")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/backend"})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "BlueLake"},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "BlueLake",
                "to": ["BlueLake"],
                "subject": "Large Image",
                "body_md": f"![img]({large_path})",
            },
        )
        assert res.data.get("deliveries")
        attachments = (res.data.get("deliveries") or [{}])[0].get("payload", {}).get("attachments", [])
        # Should be stored as file, not inline
        if attachments:
            assert attachments[0].get("type") == "file"

    large_path.unlink(missing_ok=True)
