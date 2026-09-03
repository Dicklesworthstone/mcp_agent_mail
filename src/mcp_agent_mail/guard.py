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


# A husky v9 stub's single effective statement sources the sibling resolver
# ``h``. These patterns admit the spellings husky itself and hand-rolled
# equivalents produce: ``.`` or ``source`` (optional ``--``); the resolver
# directory as ``$(dirname "$0")`` (any quoting of $0, optional ``--``) or as
# the ``${0%/*}`` parameter expansion husky actually ships; the whole operand
# bare, double- or single-quoted; optional trailing ``;``. Anything else is a
# hook body of its own and must NOT be treated as a stub (issue #264).
_STUB_DOLLAR0 = r'(?:"\$\{?0\}?"|\'\$\{?0\}?\'|\$\{?0\}?)'
_STUB_DIR = (
    r'(?:\$\([ \t]*dirname[ \t]+(?:--[ \t]+)?' + _STUB_DOLLAR0 + r'[ \t]*\)'
    r'|\$\{0%/\*\})'
)
_STUB_SOURCE_LINE = (
    r'^(?:\.|source)[ \t]+(?:--[ \t]+)?'
    r'(?:"' + _STUB_DIR + r'/h"'
    r"|'" + _STUB_DIR + r"/h'"
    r'|' + _STUB_DIR + r'/h)'
    r'[ \t]*;?[ \t]*$'
)


def _render_chain_runner_script(hook_name: str) -> str:
    """
    Render a Python chain-runner for the given Git hook name.

    Behavior:
    - Runs executables in hooks.d/<hook_name>/* in lexical order.
    - For pre-push, reads STDIN once and forwards it to each child hook.
    - If a <hook_name>.orig exists and is executable, it is invoked last.
      When the preserved .orig is a husky v9 stub (it sources the sibling
      resolver ``h``, which derives the tracked hook name from basename($0)),
      the runner sources ``h`` through /bin/sh with argv0 set to the real
      hook name. Exec'ing the renamed ``<hook_name>.orig`` directly would
      make husky look up a non-existent ``.husky/<hook_name>.orig`` and
      silently skip the user's tracked hook. Only a file that is *nothing
      but* such a stub is diverted this way; a hand-written hook that also
      sources ``h`` is exec'd directly so its own body still runs.
    - On Windows, where CreateProcess cannot honor shebangs, children are
      dispatched by suffix (``.py`` via ``python``; ``.exe/.com/.bat/.cmd``
      directly) and everything else — notably a preserved shell-script
      ``<hook_name>.orig`` — by shebang, through ``python`` or a resolved
      ``sh`` (PATH first, then git-for-windows' bundled ``sh.exe``).
    - Exits non-zero on the first non-zero child exit code.
    """
    lines: list[str] = [
        "#!/usr/bin/env python3",
        f"# mcp-agent-mail chain-runner ({hook_name})",
        "import os",
        "import re",
        "import sys",
        "import stat",
        "import subprocess",
        "from pathlib import Path",
        "",
        "HOOK_DIR = Path(__file__).parent",
        f"RUN_DIR = HOOK_DIR / 'hooks.d' / '{hook_name}'",
        f"ORIG = HOOK_DIR / '{hook_name}.orig'",
        f"HOOK_NAME = '{hook_name}'",
        "HUSKY_H = HOOK_DIR / 'h'",
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
        "# Forward Git's hook arguments (e.g. pre-push <remote> <url>) to children.",
        "ARGV = sys.argv[1:]",
        "",
        "def _win_sh():",
        "    # Resolve a POSIX shell for shebang scripts on Windows. Git supplies",
        "    # its own sh when *git* runs the hook, but the hook is also invoked",
        "    # directly (tests, wrappers, GUI clients) where bare 'sh' may be",
        "    # absent from PATH, so fall back to git-for-windows' bundled sh.exe.",
        "    import shutil",
        "    found = shutil.which('sh')",
        "    if found:",
        "        return found",
        "    try:",
        "        cp = subprocess.run(['git', '--exec-path'], capture_output=True, text=True, check=False)",
        "        exec_path = (cp.stdout or '').strip()",
        "    except Exception:",
        "        exec_path = ''",
        "    if exec_path:",
        "        base = Path(exec_path)",
        "        for anchor in (base, *base.parents):",
        "            for rel in (('usr', 'bin', 'sh.exe'), ('bin', 'sh.exe')):",
        "                cand = anchor.joinpath(*rel)",
        "                if cand.exists():",
        "                    return str(cand)",
        "    return 'sh'",
        "",
        "def _read_shebang(path: Path) -> str:",
        "    try:",
        "        with open(path, 'rb') as fh:",
        "            first = fh.readline(256).decode('utf-8', 'ignore').strip()",
        "    except Exception:",
        "        return ''",
        "    return first[2:].strip() if first.startswith('#!') else ''",
        "",
        "def _run_child(path: Path, * , stdin_bytes=None):",
        "    argv = [str(path), *ARGV]",
        "    if os.name != 'posix':",
        "        # Windows CreateProcess runs PE binaries and PATHEXT suffixes only;",
        "        # shebang scripts (a preserved <hook>.orig, hooks.d shell plugins)",
        "        # need explicit interpreter dispatch or every commit dies with",
        "        # WinError 193 (%1 is not a valid Win32 application). Issue #262.",
        "        suffix = path.suffix.lower()",
        "        if suffix == '.py':",
        "            argv = ['python', str(path), *ARGV]",
        "        elif suffix not in ('.exe', '.com', '.bat', '.cmd'):",
        "            shebang = _read_shebang(path)",
        "            if 'python' in shebang:",
        "                argv = ['python', str(path), *ARGV]",
        "            else:",
        "                argv = [_win_sh(), str(path), *ARGV]",
        "    return subprocess.run(argv, input=stdin_bytes, check=False).returncode",
        "",
        f"HUSKY_STUB_LINE_RE = re.compile({_STUB_SOURCE_LINE!r})",
        "",
        "def _is_husky_stub(path: Path) -> bool:",
        "    # husky v9 keeps its hooks in <repo>/.husky/_ next to a resolver",
        "    # named 'h'; each stub just sources h, and h derives the tracked",
        "    # hook name from basename($0).",
        "    #",
        "    # Divert to the resolver ONLY when the preserved file is nothing",
        "    # but such a stub: an optional shebang, blank lines, '#' comments,",
        "    # and a single statement sourcing the sibling 'h'. A hand-written",
        "    # hook that does real work and ALSO sources 'h' must be exec'd",
        "    # directly -- diverting it silently discards the user's hook body",
        "    # (issue #264). Bias to custom whenever unsure: a custom hook that",
        "    # sources 'h' still reaches the resolver through its own source",
        "    # line, whereas a dropped body can wave through a commit that the",
        "    # user's hook would have blocked.",
        "    if os.name != 'posix' or not HUSKY_H.is_file():",
        "        return False",
        "    try:",
        "        text = path.read_text(encoding='utf-8', errors='ignore')",
        "    except Exception:",
        "        return False",
        "    effective = []",
        "    for index, raw in enumerate(text.splitlines()):",
        "        line = raw.strip()",
        "        if index == 0 and line.startswith('#!'):",
        "            continue",
        "        if not line or line.startswith('#'):",
        "            continue",
        "        effective.append(line)",
        "        if len(effective) > 1:",
        "            return False  # a second statement means a real hook body",
        "    return bool(effective) and HUSKY_STUB_LINE_RE.match(effective[0]) is not None",
        "",
        "def _run_orig(*, stdin_bytes=None):",
        "    if _is_husky_stub(ORIG):",
        "        # Source husky's resolver with argv0 = <hooksDir>/<hook name> so",
        "        # basename($0) is the hook name (not '<hook name>.orig') and it",
        "        # still resolves and runs the user's tracked hook.",
        "        argv0 = str(HOOK_DIR / HOOK_NAME)",
        "        snippet = 'husky_h=\"$1\"; shift; . \"$husky_h\"'",
        "        return subprocess.run(",
        "            ['/bin/sh', '-c', snippet, argv0, str(HUSKY_H), *ARGV],",
        "            input=stdin_bytes,",
        "            check=False,",
        "        ).returncode",
        "    return _run_child(ORIG, stdin_bytes=stdin_bytes)",
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
            "# Run the preserved original hook last (POSIX: only if it is executable).",
            "if ORIG.exists() and (os.name != 'posix' or _is_exec(ORIG)):",
            "    rc = _run_orig(stdin_bytes=stdin_bytes)",
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
            "# Run the preserved original hook last (POSIX: only if it is executable).",
            "if ORIG.exists() and (os.name != 'posix' or _is_exec(ORIG)):",
            "    rc = _run_orig()",
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
        "TRUTHY = {\"1\",\"true\",\"t\",\"yes\",\"y\"}",
        "GATE_ENABLED = (",
        "    os.environ.get(\"WORKTREES_ENABLED\", \"0\").strip().lower() in TRUTHY",
        "    or os.environ.get(\"GIT_IDENTITY_ENABLED\", \"0\").strip().lower() in TRUTHY",
        ")",
        "",
        "# Exit early if gate is not enabled (WORKTREES_ENABLED=0 and GIT_IDENTITY_ENABLED=0)",
        "if not GATE_ENABLED:",
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
        "# Honor core.ignorecase: case-fold paths and patterns before matching (#194).",
        "def _detect_ignorecase():",
        "    try:",
        "        cp = subprocess.run([\"git\",\"config\",\"--type=bool\",\"--get\",\"core.ignorecase\"],",
        "                            check=False,capture_output=True,text=True)",
        "        return cp.stdout.strip() == \"true\"",
        "    except Exception:",
        "        return False",
        "IGNORECASE = _detect_ignorecase()",
        "def _casefold(value):",
        "    return value.lower() if IGNORECASE else value",
        "def _compile_one(patt):",
        "    q = _casefold(patt.replace(\"\\\\\",\"/\"))",
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
        "            # Skip virtual namespace reservations (tool://, resource://, service://) — bd-14z",
        "            if any(patt.startswith(pfx) for pfx in ('tool://', 'resource://', 'service://')):",
        "                continue",
        "            holder = (r.get('agent') or '').strip()",
        "            exclusive = r.get('exclusive', True)",
        "            released = (r.get('released_ts') or '').strip()",
        "            expires = (r.get('expires_ts') or '').strip()",
        "            if not exclusive:",
        "                continue",
        "            if holder and holder == AGENT_NAME:",
        "                continue",
        "            if released:",
        "                continue",
        "            if not _not_expired(expires):",
        "                continue",
        "            # Pre-compile pattern ONCE (not per-path)",
        "            spec = _compile_one(patt)",
        "            patt_norm = _casefold(patt.replace('\\\\','/').lstrip('/'))",
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
        "        norm = _casefold(p.replace('\\\\','/').lstrip('/'))",
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
        "TRUTHY = {\"1\",\"true\",\"t\",\"yes\",\"y\"}",
        "GATE_ENABLED = (",
        "    os.environ.get(\"WORKTREES_ENABLED\", \"0\").strip().lower() in TRUTHY",
        "    or os.environ.get(\"GIT_IDENTITY_ENABLED\", \"0\").strip().lower() in TRUTHY",
        ")",
        "",
        "# Exit early if gate is not enabled (WORKTREES_ENABLED=0 and GIT_IDENTITY_ENABLED=0)",
        "if not GATE_ENABLED:",
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
        "        cp = subprocess.run([\"git\",\"diff-tree\",\"-r\",\"--root\",\"--no-commit-id\",\"--name-status\",\"-M\",\"--no-ext-diff\",\"--diff-filter=ACMRDTU\",\"-z\",c],",
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
        "# Honor core.ignorecase: case-fold paths and patterns before matching (#194).",
        "def _detect_ignorecase():",
        "    try:",
        "        cp = subprocess.run([\"git\",\"config\",\"--type=bool\",\"--get\",\"core.ignorecase\"],",
        "                            check=False,capture_output=True,text=True)",
        "        return cp.stdout.strip() == \"true\"",
        "    except Exception:",
        "        return False",
        "IGNORECASE = _detect_ignorecase()",
        "def _casefold(value):",
        "    return value.lower() if IGNORECASE else value",
        "def _compile_one(patt):",
        "    q = _casefold(patt.replace(\"\\\\\",\"/\"))",
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
        "            # Skip virtual namespace reservations (tool://, resource://, service://) — bd-14z",
        "            if any(patt.startswith(pfx) for pfx in ('tool://', 'resource://', 'service://')):",
        "                continue",
        "            holder = (r.get('agent') or '').strip()",
        "            exclusive = r.get('exclusive', True)",
        "            released = (r.get('released_ts') or '').strip()",
        "            expires = (r.get('expires_ts') or '').strip()",
        "            if not exclusive:",
        "                continue",
        "            if holder and holder == AGENT_NAME:",
        "                continue",
        "            if released:",
        "                continue",
        "            if not _not_expired(expires):",
        "                continue",
        "            # Pre-compile pattern ONCE (not per-path)",
        "            spec = _compile_one(patt)",
        "            patt_norm = _casefold(patt.replace('\\\\','/').lstrip('/'))",
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
        "        norm = _casefold(p.replace('\\\\','/').lstrip('/'))",
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

    def _agent_mail_shims(hook_name: str) -> list[Path]:
        shim_signatures = {
            hooks_dir / f"{hook_name}.cmd": f'python "%DIR%{hook_name}" %*',
            hooks_dir / f"{hook_name}.ps1": f"Join-Path $PSScriptRoot '{hook_name}'",
        }
        matches: list[Path] = []
        for shim_path, signature in shim_signatures.items():
            try:
                content = shim_path.read_text("utf-8")
            except Exception:
                continue
            if signature in content:
                matches.append(shim_path)
        return matches

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
                    for shim_path in await asyncio.to_thread(_agent_mail_shims, hook_name):
                        await asyncio.to_thread(shim_path.unlink)
                    removed = True
                else:
                    # No other plugins and no .orig - safe to remove chain-runner
                    await asyncio.to_thread(hook_path.unlink)
                    for shim_path in await asyncio.to_thread(_agent_mail_shims, hook_name):
                        await asyncio.to_thread(shim_path.unlink)
                    removed = True
            elif is_legacy_hook:
                # Legacy single-file hook (not chain-runner) - safe to remove
                await asyncio.to_thread(hook_path.unlink)
                for shim_path in await asyncio.to_thread(_agent_mail_shims, hook_name):
                    await asyncio.to_thread(shim_path.unlink)
                removed = True

    return removed
