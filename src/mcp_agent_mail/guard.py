"""Pre-commit guard helpers for MCP Agent Mail."""

from __future__ import annotations

import asyncio
import os
import textwrap
from pathlib import Path
from string import Template

from .config import Settings
from .storage import AsyncFileLock, ProjectArchive, ensure_archive

__all__ = [
    "install_guard",
    "render_precommit_script",
    "uninstall_guard",
]


def render_precommit_script(archive: ProjectArchive) -> str:
    """Return the pre-commit script content for the given archive.

    Construct with explicit lines at column 0 to avoid indentation errors.
    """

    claims_dir = str((archive.root / "claims").resolve())
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
        f"CLAIMS_DIR = Path(\"{claims_dir}\")",
        f"STORAGE_ROOT = Path(\"{storage_root}\")",
        "AGENT_NAME = os.environ.get(\"AGENT_NAME\")",
        "if not AGENT_NAME:",
        "    sys.stderr.write(\"[pre-commit] AGENT_NAME environment variable is required.\\n\")",
        "    sys.exit(1)",
        "",
        "if not CLAIMS_DIR.exists():",
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
        "def load_claims():",
        "    for candidate in CLAIMS_DIR.glob(\"*.json\"):",
        "        try:",
        "            data = json.loads(candidate.read_text())",
        "        except Exception:",
        "            continue",
        "        yield data",
        "",
        "conflicts = []",
        "for claim in load_claims():",
        "    if claim.get(\"agent\") == AGENT_NAME:",
        "        continue",
        "    expires = claim.get(\"expires_ts\")",
        "    if expires:",
        "        try:",
        "            expires_dt = datetime.fromisoformat(expires)",
        "            if expires_dt < now:",
        "                continue",
        "        except Exception:",
        "            pass",
        "    pattern = claim.get(\"path_pattern\")",
        "    if not pattern:",
        "        continue",
        "    for path_value in paths:",
        "        if fnmatch(path_value, pattern) or fnmatch(pattern, path_value):",
        "            conflicts.append((path_value, claim.get(\"agent\"), pattern))",
        "",
        "if conflicts:",
        "    sys.stderr.write(\"[pre-commit] Exclusive claim conflicts detected:\\n\")",
        "    for path_value, agent_name, pattern in conflicts:",
        "        sys.stderr.write(f\"  - {path_value} matches claim '{pattern}' held by {agent_name}\\n\")",
        "    sys.stderr.write(\"Resolve conflicts or release claims before committing.\\n\")",
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

    async with AsyncFileLock(archive.lock_path):
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
