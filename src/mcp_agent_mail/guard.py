"""Pre-commit guard helpers for MCP Agent Mail."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

from .config import Settings
from .storage import ProjectArchive, ensure_archive

__all__ = [
    "install_guard",
    "install_prepush_guard",
    "render_precommit_script",
    "render_prepush_script",
    "uninstall_guard",
]


def _render_chain_runner_script(hook_name: str) -> str:
    """
    Render a Python chain-runner for the given Git hook name.

    Behavior:
    - Runs executables in hooks.d/<hook_name>/* in lexical order.
    - For pre-push, reads STDIN once and forwards it to each child hook.
    - If a <hook_name>.orig exists and is executable, it is invoked last.
    - Exits non-zero on the first non-zero child exit code.
    """
    lines: list[str] = [
        "#!/usr/bin/env python3",
        f"# mcp-agent-mail chain-runner ({hook_name})",
        "import os",
        "import sys",
        "import stat",
        "import subprocess",
        "from pathlib import Path",
        "",
        "HOOK_DIR = Path(__file__).parent",
        f"RUN_DIR = HOOK_DIR / 'hooks.d' / '{hook_name}'",
        f"ORIG = HOOK_DIR / '{hook_name}.orig'",
        "",
        "def _is_exec(p: Path) -> bool:",
        "    try:",
        "        st = p.stat()",
        "        return bool(st.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))",
        "    except Exception:",
        "        return False",
        "",
        "def _list_execs() -> list[Path]:",
        "    if not RUN_DIR.exists() or not RUN_DIR.is_dir():",
        "        return []",
        "    items = sorted([p for p in RUN_DIR.iterdir() if p.is_file()], key=lambda p: p.name)",
        "    # On POSIX, honor exec bit; on Windows, include all files (we'll dispatch .py via python).",
        "    if os.name == 'posix':",
        "        try:",
        "            items = [p for p in items if _is_exec(p)]",
        "        except Exception:",
        "            pass",
        "    return items",
        "",
        "def _run_child(path: Path, * , stdin_bytes=None):",
        "    # On Windows, prefer 'python' for .py plugins to avoid PATHEXT reliance.",
        "    if os.name != 'posix' and path.suffix.lower() == '.py':",
        "        return subprocess.run(['python', str(path)], input=stdin_bytes, check=False).returncode",
        "    return subprocess.run([str(path)], input=stdin_bytes, check=False).returncode",
        "",
    ]
    if hook_name == "pre-push":
        lines += [
            "# Read STDIN once (Git passes ref tuples); forward to children",
            "stdin_bytes = sys.stdin.buffer.read()",
            "for exe in _list_execs():",
            "    rc = _run_child(exe, stdin_bytes=stdin_bytes)",
            "    if rc != 0:",
            "        sys.exit(rc)",
            "",
            "if ORIG.exists():",
            "    rc = _run_child(ORIG, stdin_bytes=stdin_bytes)",
            "    if rc != 0:",
            "        sys.exit(rc)",
            "sys.exit(0)",
        ]
    else:
        lines += [
            "for exe in _list_execs():",
            "    rc = _run_child(exe)",
            "    if rc != 0:",
            "        sys.exit(rc)",
            "",
            "if ORIG.exists():",
            "    rc = _run_child(ORIG)",
            "    if rc != 0:",
            "        sys.exit(rc)",
            "sys.exit(0)",
        ]
    return "\n".join(lines) + "\n"


def _git(cwd: Path, *args: str) -> str | None:
    try:
        cp = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
        return cp.stdout.strip()
    except Exception:
        return None


def _resolve_hooks_dir(repo: Path) -> Path:
    # Prefer core.hooksPath if configured
    hooks_path = _git(repo, "config", "--get", "core.hooksPath")
    if hooks_path:
        # Expand user (e.g. ~/.githooks)
        p = Path(hooks_path).expanduser()
        if p.is_absolute():
            return p
        # Resolve relative to repo root
        root = _git(repo, "rev-parse", "--show-toplevel") or str(repo)
        return Path(root) / hooks_path

    # Fall back to git-dir/hooks
    git_dir = _git(repo, "rev-parse", "--git-dir")
    if git_dir:
        g = Path(git_dir)
        if not g.is_absolute():
            g = repo / g
        return g / "hooks"
    # Last resort: traditional path
    return repo / ".git" / "hooks"



def render_precommit_script(archive: ProjectArchive) -> str:
    """Return the pre-commit script content for the given archive.

    Construct with explicit lines at column 0 to avoid indentation errors.
    """

    file_reservations_dir = str((archive.root / "file_reservations").resolve()).replace("\\", "/")
    storage_root = str(archive.root.resolve()).replace("\\", "/")
    lines = [
        "#!/usr/bin/env python3",
        "# mcp-agent-mail guard hook (pre-commit)",
        "import json",
        "import os",
        "import sys",
        "import subprocess",
        "from pathlib import Path",
        "import fnmatch as _fn",
        "from datetime import datetime, timezone",
        "",
        "# Optional Git pathspec support (preferred when available)",
        "try:",
        "    from pathspec import PathSpec as _PS  # type: ignore[import-not-found]",
        "except Exception:",
        "    _PS = None  # type: ignore[assignment]",
        "",
        f"FILE_RESERVATIONS_DIR = Path({json.dumps(file_reservations_dir)})",
        f"STORAGE_ROOT = Path({json.dumps(storage_root)})",
        "",
        "# Gate variables (presence) and mode",
        "GATE = (os.environ.get(\"WORKTREES_ENABLED\",\"0\") or os.environ.get(\"GIT_IDENTITY_ENABLED\",\"0\") or \"0\")",
        "",
        "# Exit early if gate is not enabled (WORKTREES_ENABLED=0 and GIT_IDENTITY_ENABLED=0)",
        "if GATE.strip().lower() not in {\"1\",\"true\",\"t\",\"yes\",\"y\"}:",
        "    sys.exit(0)",
        "",
        "# Advisory/blocking mode: default to 'block' unless explicitly set to 'warn'.",
        "MODE = (os.environ.get(\"AGENT_MAIL_GUARD_MODE\",\"block\") or \"block\").strip().lower()",
        "ADVISORY = MODE in {\"warn\",\"advisory\",\"adv\"}",
        "",
        "# Emergency bypass",
        "if (os.environ.get(\"AGENT_MAIL_BYPASS\",\"0\") or \"0\").strip().lower() in {\"1\",\"true\",\"t\",\"yes\",\"y\"}:",
        "    sys.stderr.write(\"[pre-commit] bypass enabled via AGENT_MAIL_BYPASS=1\\n\")",
        "    sys.exit(0)",
        "AGENT_NAME = os.environ.get(\"AGENT_NAME\")",
        "if not AGENT_NAME:",
        "    sys.stderr.write(\"[pre-commit] AGENT_NAME environment variable is required.\\n\")",
        "    sys.exit(1)",
        "",
        "# Collect staged paths (name-only) and expand renames/moves (old+new)",
        "paths = []",
        "try:",
        "    co = subprocess.run([\"git\",\"diff\",\"--cached\",\"--name-only\",\"-z\",\"--diff-filter=ACMRDTU\"],",
        "                        check=True,capture_output=True)",
        "    data = co.stdout.decode(\"utf-8\",\"ignore\")",
        "    for p in data.split(\"\\x00\"):",
        "        if p:",
        "            paths.append(p)",
        "    # Rename detection: capture both old and new names",
        "    cs = subprocess.run([\"git\",\"diff\",\"--cached\",\"--name-status\",\"-M\",\"-z\"],",
        "                        check=True,capture_output=True)",
        "    sdata = cs.stdout.decode(\"utf-8\",\"ignore\")",
        "    parts = [x for x in sdata.split(\"\\x00\") if x]",
        "    i = 0",
        "    while i < len(parts):",
        "        status = parts[i]",
        "        i += 1",
        "        if status.startswith(\"R\") and i + 1 < len(parts):",
        "            oldp = parts[i]; newp = parts[i+1]; i += 2",
        "            if oldp: paths.append(oldp)",
        "            if newp: paths.append(newp)",
        "        else:",
        "            # Status followed by one path",
        "            if i < len(parts):",
        "                pth = parts[i]; i += 1",
        "                if pth: paths.append(pth)",
        "except Exception:",
        "    pass",
        "",
        "if not paths:",
        "    sys.exit(0)",
        "",
        "# Local conflict detection against FILE_RESERVATIONS_DIR",
        "def _now_utc():",
        "    return datetime.now(timezone.utc)",
        "def _parse_iso(value):",
        "    if not value:",
        "        return None",
        "    try:",
        "        text = value",
        "        if text.endswith(\"Z\"):",
        "            text = text[:-1] + \"+00:00\"",
        "        dt = datetime.fromisoformat(text)",
        "        if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:",
        "            dt = dt.replace(tzinfo=timezone.utc)",
        "        return dt.astimezone(timezone.utc)",
        "    except Exception:",
        "        return None",
        "def _not_expired(expires_ts):",
        "    parsed = _parse_iso(expires_ts)",
        "    if parsed is None:",
        "        return True",
        "    return parsed > _now_utc()",
        "def _compile_one(patt):",
        "    q = patt.replace(\"\\\\\",\"/\")",
        "    if _PS:",
        "        try:",
        "            return _PS.from_lines(\"gitignore\", [q])",
        "        except Exception:",
        "            return None",
        "    return None",
        "",
        "# Phase 1: Pre-load and compile all reservation patterns ONCE",
        "compiled_patterns = []",
        "all_pattern_strings = []",
        "seen_ids = set()",
        "try:",
        "    for f in FILE_RESERVATIONS_DIR.iterdir():",
        "        if not f.name.endswith('.json'):",
        "            continue",
        "        try:",
        "            data = json.loads(f.read_text(encoding='utf-8'))",
        "        except Exception:",
        "            continue",
        "        recs = data if isinstance(data, list) else [data]",
        "        for r in recs:",
        "            if not isinstance(r, dict):",
        "                continue",
        "            rid = r.get('id')",
        "            if rid is not None:",
        "                rid_key = str(rid)",
        "                if rid_key in seen_ids:",
        "                    continue",
        "                seen_ids.add(rid_key)",
        "            patt = (r.get('path_pattern') or '').strip()",
        "            if not patt:",
        "                continue",
        "            holder = (r.get('agent') or '').strip()",
        "            exclusive = r.get('exclusive', True)",
        "            expires = (r.get('expires_ts') or '').strip()",
        "            if not exclusive:",
        "                continue",
        "            if holder and holder == AGENT_NAME:",
        "                continue",
        "            if not _not_expired(expires):",
        "                continue",
        "            # Pre-compile pattern ONCE (not per-path)",
        "            spec = _compile_one(patt)",
        "            patt_norm = patt.replace('\\\\','/').lstrip('/')",
        "            compiled_patterns.append((spec, patt, patt_norm, holder))",
        "            all_pattern_strings.append(patt_norm)",
        "except Exception:",
        "    compiled_patterns = []",
        "    all_pattern_strings = []",
        "",
        "# Phase 2: Build union PathSpec for fast-path rejection",
        "union_spec = None",
        "if _PS and all_pattern_strings:",
        "    try:",
        "        union_spec = _PS.from_lines(\"gitignore\", all_pattern_strings)",
        "    except Exception:",
        "        union_spec = None",
        "",
        "# Phase 3: Check paths against compiled patterns",
        "conflicts = []",
        "if compiled_patterns:",
        "    for p in paths:",
        "        norm = p.replace('\\\\','/').lstrip('/')",
        "        # Fast-path: if union_spec exists and path doesn't match ANY pattern, skip",
        "        if union_spec is not None and not union_spec.match_file(norm):",
        "            continue",
        "        # Detailed matching for conflict attribution",
        "        for spec, patt, patt_norm, holder in compiled_patterns:",
        "            matched = spec.match_file(norm) if spec is not None else _fn.fnmatch(norm, patt_norm)",
        "            if matched:",
        "                conflicts.append((patt, p, holder))",
        "if conflicts:",
        "    sys.stderr.write(\"Exclusive file_reservation conflicts detected\\n\")",
        "    for patt, path, holder in conflicts[:10]:",
        "        sys.stderr.write(f\"- {path} matches {patt} (holder: {holder})\\n\")",
        "    if ADVISORY:",
        "        sys.exit(0)",
        "    sys.exit(1)",
        "sys.exit(0)",
    ]
    return "\n".join(lines) + "\n"


def render_prepush_script(archive: ProjectArchive) -> str:
    """Return the pre-push script content that checks conflicts across pushed commits.

    Python script to avoid external shell assumptions; NUL-safe and respects gate/advisory mode.
    """
    file_reservations_dir = str((archive.root / "file_reservations").resolve()).replace("\\", "/")
    lines = [
        "#!/usr/bin/env python3",
        "# mcp-agent-mail guard hook (pre-push)",
        "import json",
        "import os",
        "import sys",
        "import subprocess",
        "from pathlib import Path",
        "import fnmatch as _fn",
        "from datetime import datetime, timezone",
        "",
        "# Optional Git pathspec support (preferred when available)",
        "try:",
        "    from pathspec import PathSpec as _PS  # type: ignore[import-not-found]",
        "except Exception:",
        "    _PS = None  # type: ignore[assignment]",
        "",
        f"FILE_RESERVATIONS_DIR = Path({json.dumps(file_reservations_dir)})",
        "",
        "# Gate variables (presence) and mode",
        "GATE = (os.environ.get(\"WORKTREES_ENABLED\",\"0\") or os.environ.get(\"GIT_IDENTITY_ENABLED\",\"0\") or \"0\")",
        "",
        "# Exit early if gate is not enabled (WORKTREES_ENABLED=0 and GIT_IDENTITY_ENABLED=0)",
        "if GATE.strip().lower() not in {\"1\",\"true\",\"t\",\"yes\",\"y\"}:",
        "    sys.exit(0)",
        "",
        "MODE = (os.environ.get(\"AGENT_MAIL_GUARD_MODE\",\"block\") or \"block\").strip().lower()",
        "ADVISORY = MODE in {\"warn\",\"advisory\",\"adv\"}",
        "if (os.environ.get(\"AGENT_MAIL_BYPASS\",\"0\") or \"0\").strip().lower() in {\"1\",\"true\",\"t\",\"yes\",\"y\"}:",
        "    sys.stderr.write(\"[pre-push] bypass enabled via AGENT_MAIL_BYPASS=1\\n\")",
        "    sys.exit(0)",
        "AGENT_NAME = os.environ.get(\"AGENT_NAME\")",
        "if not AGENT_NAME:",
        "    sys.stderr.write(\"[pre-push] AGENT_NAME environment variable is required.\\n\")",
        "    sys.exit(1)",
        "if not FILE_RESERVATIONS_DIR.exists():",
        "    sys.exit(0)",
        "",
        "# Read tuples from STDIN: <local ref> <local sha> <remote ref> <remote sha>",
        "tuples = []",
        "for line in sys.stdin.read().splitlines():",
        "    parts = line.strip().split()",
        "    if len(parts) >= 4:",
        "        tuples.append((parts[0], parts[1], parts[2], parts[3]))",
        "",
        "changed = []",
        "commits = []",
        "for local_ref, local_sha, remote_ref, remote_sha in tuples:",
        "    if not local_sha:",
        "        continue",
        "    # Enumerate commits to be pushed using remote name from args (argv[1]) when available",
        "    remote = (sys.argv[1] if len(sys.argv) > 1 else \"origin\")",
        "    try:",
        "        cp = subprocess.run([\"git\",\"rev-list\",\"--topo-order\",local_sha,\"--not\",f\"--remotes={remote}\"],",
        "                            check=True,capture_output=True,text=True)",
        "        for sha in cp.stdout.splitlines():",
        "            if sha:",
        "                commits.append(sha.strip())",
        "    except Exception:",
        "        # Fallback: gather changed paths directly when range enumeration fails",
        "        rng = local_sha if (not remote_sha or set(remote_sha) == {\"0\"}) else f\"{remote_sha}..{local_sha}\"",
        "        try:",
        "            cp = subprocess.run([\"git\",\"diff\",\"--name-status\",\"-M\",\"-z\",rng],check=True,capture_output=True)",
        "            data = cp.stdout.decode(\"utf-8\",\"ignore\")",
        "            parts = [p for p in data.split(\"\\x00\") if p]",
        "            i = 0",
        "            while i < len(parts):",
        "                status = parts[i]",
        "                i += 1",
        "                if status.startswith(\"R\") and i + 1 < len(parts):",
        "                    oldp = parts[i]; newp = parts[i + 1]; i += 2",
        "                    if oldp: changed.append(oldp)",
        "                    if newp: changed.append(newp)",
        "                else:",
        "                    if i < len(parts):",
        "                        pth = parts[i]; i += 1",
        "                        if pth: changed.append(pth)",
        "        except Exception:",
        "            pass",
        "",
        "# changed already initialized above; add per-commit changed paths (capture renames)",
        "for c in commits:",
        "    try:",
        "        cp = subprocess.run([\"git\",\"diff-tree\",\"-r\",\"--no-commit-id\",\"--name-status\",\"-M\",\"--no-ext-diff\",\"--diff-filter=ACMRDTU\",\"-z\",c],",
        "                            check=True,capture_output=True)",
        "        data = cp.stdout.decode(\"utf-8\",\"ignore\")",
        "        parts = [p for p in data.split(\"\\x00\") if p]",
        "        i = 0",
        "        while i < len(parts):",
        "            status = parts[i]",
        "            i += 1",
        "            if status.startswith(\"R\") and i + 1 < len(parts):",
        "                oldp = parts[i]; newp = parts[i + 1]; i += 2",
        "                if oldp: changed.append(oldp)",
        "                if newp: changed.append(newp)",
        "            else:",
        "                if i < len(parts):",
        "                    pth = parts[i]; i += 1",
        "                    if pth: changed.append(pth)",
        "    except Exception:",
        "        continue",
        "",
        "# Local conflict detection against FILE_RESERVATIONS_DIR using changed paths",
        "if not changed:",
        "    sys.exit(0)",
        "def _now_utc():",
        "    return datetime.now(timezone.utc)",
        "def _parse_iso(value):",
        "    if not value:",
        "        return None",
        "    try:",
        "        text = value",
        "        if text.endswith(\"Z\"):",
        "            text = text[:-1] + \"+00:00\"",
        "        dt = datetime.fromisoformat(text)",
        "        if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:",
        "            dt = dt.replace(tzinfo=timezone.utc)",
        "        return dt.astimezone(timezone.utc)",
        "    except Exception:",
        "        return None",
        "def _not_expired(expires_ts):",
        "    parsed = _parse_iso(expires_ts)",
        "    if parsed is None:",
        "        return True",
        "    return parsed > _now_utc()",
        "def _compile_one(patt):",
        "    q = patt.replace(\"\\\\\",\"/\")",
        "    if _PS:",
        "        try:",
        "            return _PS.from_lines(\"gitignore\", [q])",
        "        except Exception:",
        "            return None",
        "    return None",
        "",
        "# Phase 1: Pre-load and compile all reservation patterns ONCE",
        "compiled_patterns = []",
        "all_pattern_strings = []",
        "seen_ids = set()",
        "try:",
        "    for f in FILE_RESERVATIONS_DIR.iterdir():",
        "        if not f.name.endswith('.json'):",
        "            continue",
        "        try:",
        "            data = json.loads(f.read_text(encoding='utf-8'))",
        "        except Exception:",
        "            continue",
        "        recs = data if isinstance(data, list) else [data]",
        "        for r in recs:",
        "            if not isinstance(r, dict):",
        "                continue",
        "            rid = r.get('id')",
        "            if rid is not None:",
        "                rid_key = str(rid)",
        "                if rid_key in seen_ids:",
        "                    continue",
        "                seen_ids.add(rid_key)",
        "            patt = (r.get('path_pattern') or '').strip()",
        "            if not patt:",
        "                continue",
        "            holder = (r.get('agent') or '').strip()",
        "            exclusive = r.get('exclusive', True)",
        "            expires = (r.get('expires_ts') or '').strip()",
        "            if not exclusive:",
        "                continue",
        "            if holder and holder == AGENT_NAME:",
        "                continue",
        "            if not _not_expired(expires):",
        "                continue",
        "            # Pre-compile pattern ONCE (not per-path)",
        "            spec = _compile_one(patt)",
        "            patt_norm = patt.replace('\\\\','/').lstrip('/')",
        "            compiled_patterns.append((spec, patt, patt_norm, holder))",
        "            all_pattern_strings.append(patt_norm)",
        "except Exception:",
        "    compiled_patterns = []",
        "    all_pattern_strings = []",
        "",
        "# Phase 2: Build union PathSpec for fast-path rejection",
        "union_spec = None",
        "if _PS and all_pattern_strings:",
        "    try:",
        "        union_spec = _PS.from_lines(\"gitignore\", all_pattern_strings)",
        "    except Exception:",
        "        union_spec = None",
        "",
        "# Phase 3: Check changed paths against compiled patterns",
        "conflicts = []",
        "if compiled_patterns:",
        "    for p in changed:",
        "        norm = p.replace('\\\\','/').lstrip('/')",
        "        # Fast-path: if union_spec exists and path doesn't match ANY pattern, skip",
        "        if union_spec is not None and not union_spec.match_file(norm):",
        "            continue",
        "        # Detailed matching for conflict attribution",
        "        for spec, patt, patt_norm, holder in compiled_patterns:",
        "            matched = spec.match_file(norm) if spec is not None else _fn.fnmatch(norm, patt_norm)",
        "            if matched:",
        "                conflicts.append((patt, p, holder))",
        "if conflicts:",
        "    sys.stderr.write(\"Exclusive file_reservation conflicts detected\\n\")",
        "    for patt, path, holder in conflicts[:10]:",
        "        sys.stderr.write(f\"- {path} matches {patt} (holder: {holder})\\n\")",
        "    if ADVISORY:",
        "        sys.exit(0)",
        "    sys.exit(1)",
        "sys.exit(0)",
    ]
    return "\n".join(lines) + "\n"


async def install_guard(settings: Settings, project_slug: str, repo_path: Path) -> Path:
    """Install the pre-commit chain-runner and Agent Mail guard plugin."""

    archive = await ensure_archive(settings, project_slug)

    hooks_dir = _resolve_hooks_dir(repo_path)
    if not hooks_dir.exists():
        await asyncio.to_thread(hooks_dir.mkdir, parents=True, exist_ok=True)

    # Ensure hooks.d/pre-commit exists
    run_dir = hooks_dir / "hooks.d" / "pre-commit"
    await asyncio.to_thread(run_dir.mkdir, parents=True, exist_ok=True)

    chain_path = hooks_dir / "pre-commit"
    # Preserve existing non-chain hook as .orig
    if chain_path.exists():
        try:
            content = (await asyncio.to_thread(chain_path.read_text, "utf-8")).strip()
        except Exception:
            content = ""
        if "mcp-agent-mail chain-runner (pre-commit)" not in content:
            orig = hooks_dir / "pre-commit.orig"
            if not orig.exists():
                await asyncio.to_thread(chain_path.replace, orig)
    # Write/overwrite chain-runner
    chain_script = _render_chain_runner_script("pre-commit")
    await asyncio.to_thread(chain_path.write_text, chain_script, "utf-8")
    await asyncio.to_thread(os.chmod, chain_path, 0o755)

    # Windows shims (.cmd / .ps1) to invoke the Python chain-runner
    cmd_path = hooks_dir / "pre-commit.cmd"
    if not cmd_path.exists():
        cmd_body = (
            "@echo off\r\n"
            "setlocal\r\n"
            "set \"DIR=%~dp0\"\r\n"
            "python \"%DIR%pre-commit\" %*\r\n"
            "exit /b %ERRORLEVEL%\r\n"
        )
        await asyncio.to_thread(cmd_path.write_text, cmd_body, "utf-8")
    ps1_path = hooks_dir / "pre-commit.ps1"
    if not ps1_path.exists():
        ps1_body = (
            "$ErrorActionPreference = 'Stop'\n"
            "$hook = Join-Path $PSScriptRoot 'pre-commit'\n"
            "python $hook @args\n"
            "exit $LASTEXITCODE\n"
        )
        await asyncio.to_thread(ps1_path.write_text, ps1_body, "utf-8")

    # Write our guard plugin
    plugin_path = run_dir / "50-agent-mail.py"
    plugin_script = render_precommit_script(archive)
    await asyncio.to_thread(plugin_path.write_text, plugin_script, "utf-8")
    await asyncio.to_thread(os.chmod, plugin_path, 0o755)
    return chain_path


async def install_prepush_guard(settings: Settings, project_slug: str, repo_path: Path) -> Path:
    """Install the pre-push chain-runner and Agent Mail guard plugin."""
    archive = await ensure_archive(settings, project_slug)

    hooks_dir = _resolve_hooks_dir(repo_path)
    await asyncio.to_thread(hooks_dir.mkdir, parents=True, exist_ok=True)
    # Ensure hooks.d/pre-push exists
    run_dir = hooks_dir / "hooks.d" / "pre-push"
    await asyncio.to_thread(run_dir.mkdir, parents=True, exist_ok=True)

    chain_path = hooks_dir / "pre-push"
    if chain_path.exists():
        try:
            content = (await asyncio.to_thread(chain_path.read_text, "utf-8")).strip()
        except Exception:
            content = ""
        if "mcp-agent-mail chain-runner (pre-push)" not in content:
            orig = hooks_dir / "pre-push.orig"
            if not orig.exists():
                await asyncio.to_thread(chain_path.replace, orig)
    chain_script = _render_chain_runner_script("pre-push")
    await asyncio.to_thread(chain_path.write_text, chain_script, "utf-8")
    await asyncio.to_thread(os.chmod, chain_path, 0o755)

    # Windows shims (.cmd / .ps1) to invoke the Python chain-runner
    cmd_path = hooks_dir / "pre-push.cmd"
    if not cmd_path.exists():
        cmd_body = (
            "@echo off\r\n"
            "setlocal\r\n"
            "set \"DIR=%~dp0\"\r\n"
            "python \"%DIR%pre-push\" %*\r\n"
            "exit /b %ERRORLEVEL%\r\n"
        )
        await asyncio.to_thread(cmd_path.write_text, cmd_body, "utf-8")
    ps1_path = hooks_dir / "pre-push.ps1"
    if not ps1_path.exists():
        ps1_body = (
            "$ErrorActionPreference = 'Stop'\n"
            "$hook = Join-Path $PSScriptRoot 'pre-push'\n"
            "python $hook @args\n"
            "exit $LASTEXITCODE\n"
        )
        await asyncio.to_thread(ps1_path.write_text, ps1_body, "utf-8")

    plugin_path = run_dir / "50-agent-mail.py"
    plugin_script = render_prepush_script(archive)
    await asyncio.to_thread(plugin_path.write_text, plugin_script, "utf-8")
    await asyncio.to_thread(os.chmod, plugin_path, 0o755)
    return chain_path


async def uninstall_guard(repo_path: Path) -> bool:
    """Remove Agent Mail guard plugin(s) from repo, returning True if any were removed.

    - Removes hooks.d/<hook>/50-agent-mail.py if present.
    - Legacy fallback: removes top-level pre-commit/pre-push only if they are old-style
      Agent Mail hooks (sentinel present) and not chain-runners.
    """

    hooks_dir = _resolve_hooks_dir(repo_path)
    removed = False

    def _has_other_plugins(run_dir: Path) -> bool:
        """Check if there are any plugins remaining after removing ours."""
        if not run_dir.exists() or not run_dir.is_dir():
            return False
        # List all files, excluding our plugin
        return any(item.is_file() and item.name != "50-agent-mail.py" for item in run_dir.iterdir())

    # Remove our hooks.d plugins if present
    for sub in ("pre-commit", "pre-push"):
        plugin = hooks_dir / "hooks.d" / sub / "50-agent-mail.py"
        if plugin.exists():
            await asyncio.to_thread(plugin.unlink)
            removed = True

    # Legacy top-level single-file uninstall (pre-chain-runner installs)
    # Only remove chain-runner if no other plugins depend on it
    pre_commit = hooks_dir / "pre-commit"
    pre_push = hooks_dir / "pre-push"
    SENTINELS = ("mcp-agent-mail guard hook", "AGENT_NAME environment variable is required.")
    for hook_name, hook_path in [("pre-commit", pre_commit), ("pre-push", pre_push)]:
        if hook_path.exists():
            try:
                content = (await asyncio.to_thread(hook_path.read_text, "utf-8")).strip()
            except Exception:
                content = ""

            is_our_chain_runner = "mcp-agent-mail chain-runner" in content
            is_legacy_hook = any(s in content for s in SENTINELS)

            if is_our_chain_runner:
                # Check if other plugins exist that need the chain-runner
                run_dir = hooks_dir / "hooks.d" / hook_name
                orig_path = hooks_dir / f"{hook_name}.orig"

                if _has_other_plugins(run_dir):
                    # Other plugins exist - keep the chain-runner so they continue to work
                    pass
                elif orig_path.exists():
                    # No other plugins, but .orig exists - restore original hook
                    await asyncio.to_thread(hook_path.unlink)
                    await asyncio.to_thread(orig_path.replace, hook_path)
                    removed = True
                else:
                    # No other plugins and no .orig - safe to remove chain-runner
                    await asyncio.to_thread(hook_path.unlink)
                    removed = True
            elif is_legacy_hook:
                # Legacy single-file hook (not chain-runner) - safe to remove
                await asyncio.to_thread(hook_path.unlink)
                removed = True

    return removed
