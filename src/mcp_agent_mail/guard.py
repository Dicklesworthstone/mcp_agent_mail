"""Pre-commit guard helpers for MCP Agent Mail."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from .config import Settings
from .storage import ProjectArchive, archive_write_lock, ensure_archive

__all__ = [
    "install_guard",
    "render_precommit_script",
    "uninstall_guard",
]


def render_precommit_script(archive: ProjectArchive) -> str:
    """Return the pre-commit script content for the given archive.

    Construct with explicit lines at column 0 to avoid indentation errors.
    """

    file_reservations_dir = str((archive.root / "file_reservations").resolve())
    storage_root = str(archive.root.resolve())
    lines = [
        "#!/usr/bin/env python3",
        "import json",
        "import os",
        "import sys",
        "import subprocess",
        "from pathlib import Path",
        "from fnmatch import fnmatch",
        "from datetime import datetime, timezone",
        "",
        f"FILE_RESERVATIONS_DIR = Path(\"{file_reservations_dir}\")",
        f"STORAGE_ROOT = Path(\"{storage_root}\")",
        "AGENT_NAME = os.environ.get(\"AGENT_NAME\")",
        "if not AGENT_NAME:",
        "    sys.stderr.write(\"[pre-commit] AGENT_NAME environment variable is required.\\n\")",
        "    sys.exit(1)",
        "",
        "if not FILE_RESERVATIONS_DIR.exists():",
        "    sys.exit(0)",
        "",
        "now = datetime.now(timezone.utc)",
        "",
        "staged = subprocess.run([\"git\", \"diff\", \"--cached\", \"--name-only\"], capture_output=True, text=True, check=False)",
        "if staged.returncode != 0:",
        "    sys.stderr.write(\"[pre-commit] Failed to enumerate staged files.\\n\")",
        "    sys.exit(1)",
        "",
        "paths = [line.strip() for line in staged.stdout.splitlines() if line.strip()]",
        "",
        "if not paths:",
        "    sys.exit(0)",
        "",
        "def load_file_reservations():",
        "    for candidate in FILE_RESERVATIONS_DIR.glob(\"*.json\"):",
        "        try:",
        "            data = json.loads(candidate.read_text())",
        "        except Exception:",
        "            continue",
        "        yield data",
        "",
        "conflicts = []",
        "for file_reservation in load_file_reservations():",
        "    if file_reservation.get(\"agent\") == AGENT_NAME:",
        "        continue",
        "    expires = file_reservation.get(\"expires_ts\")",
        "    if expires:",
        "        try:",
        "            expires_dt = datetime.fromisoformat(expires)",
        "            if expires_dt < now:",
        "                continue",
        "        except Exception:",
        "            pass",
        "    pattern = file_reservation.get(\"path_pattern\")",
        "    if not pattern:",
        "        continue",
        "    for path_value in paths:",
        "        if fnmatch(path_value, pattern) or fnmatch(pattern, path_value):",
        "            conflicts.append((path_value, file_reservation.get(\"agent\"), pattern))",
        "",
        "if conflicts:",
        "    sys.stderr.write(\"[pre-commit] Exclusive file_reservation conflicts detected:\\n\")",
        "    for path_value, agent_name, pattern in conflicts:",
        "        sys.stderr.write(f\"  - {path_value} matches file_reservation '{pattern}' held by {agent_name}\\n\")",
        "    sys.stderr.write(\"Resolve conflicts or release file_reservations before committing.\\n\")",
        "    sys.exit(1)",
        "",
        "sys.exit(0)",
    ]
    return "\n".join(lines) + "\n"


async def install_guard(settings: Settings, project_slug: str, repo_path: Path) -> Path:
    """Install the pre-commit guard for the given project into the repo."""

    archive = await ensure_archive(settings, project_slug)
    hooks_dir = repo_path / ".git" / "hooks"
    if not hooks_dir.is_dir():
        raise ValueError(f"No git hooks directory at {hooks_dir}")

    hook_path = hooks_dir / "pre-commit"
    script = render_precommit_script(archive)

    async with archive_write_lock(archive):
        await asyncio.to_thread(hooks_dir.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(hook_path.write_text, script, "utf-8")
        await asyncio.to_thread(os.chmod, hook_path, 0o755)
    return hook_path


async def uninstall_guard(repo_path: Path) -> bool:
    """Remove the pre-commit guard from repo, returning True if removed."""

    hook_path = repo_path / ".git" / "hooks" / "pre-commit"
    if hook_path.exists():
        await asyncio.to_thread(hook_path.unlink)
        return True
    return False
