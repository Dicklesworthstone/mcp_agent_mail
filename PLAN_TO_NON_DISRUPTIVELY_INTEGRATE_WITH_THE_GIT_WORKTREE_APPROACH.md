# PLAN: Non-disruptive integration with Git worktrees for multi-agent development

---

## Objectives

- **Primary goal**: Allow agents working in separate Git worktrees of the same repository to share the same MCP Agent Mail project (identities, messages, file reservations), without breaking existing single-directory behavior.
- **Non-goals**:
  - No mandatory migrations that break existing data or workflows.
  - No behavior changes unless explicitly enabled via a startup flag or explicit parameter.
  - No forced editor/tooling changes for users who don't use worktrees.
  - All worktree‑friendly behavior is **opt‑in** behind a single gate; default stays today’s `dir` identity and existing hooks. Nothing changes unless explicitly enabled.

---

## Design summary (high-signal)

**Identity (portable, privacy‑safe, zero‑config across clones)**
- Add a durable `project_uid` and treat slug as a view. Precedence (first hit wins):
  repo marker (`.agent-mail-project-id`) → git‑remote (normalized `origin` URL) → `git-common-dir` → `git-toplevel` → `dir`. Slugs remain privacy‑safe; never embed absolute paths.

**Same‑repo and multi‑repo coordination**
- Same‑repo auto‑unification (when opted in): clones/worktrees with the same normalized remote are one project bus.
- Product Bus (**Phase 2, optional**): introduce `product_uid` to group multiple repos (frontend/backend/infra) with product‑wide inbox/search and shared threads (`bd-###`, etc.).

**Hooks & guards (composable, cross‑platform)**
- Install a chain‑runner that respects `core.hooksPath` and existing frameworks; ship POSIX + Windows variants. Add a correct pre‑push guard (reads STDIN tuples) and keep `AGENT_MAIL_BYPASS=1`.

**Reservations: Git semantics**
- Switch reservation matching to Git pathspec wildmatch (honor `core.ignorecase`), against repo‑root relative paths.

**Build interference**
- Provide build slots + a thin `am-run` wrapper for long‑running tasks (watchers/devservers) with per‑agent cache namespaces. Containers remain optional recipes layered on top.

**Zero disruption**
- All of the above are opt‑in (defaults preserve current behavior); existing projects/slugs keep working unchanged.

**Feature flag (single gate)**
- `WORKTREES_ENABLED=1` (env/flag) must be set for any of the worktree‑friendly features in this plan to activate. When unset/false, the system behaves exactly as it does today.
  - Guard installer and identity resolver must no‑op unless `WORKTREES_ENABLED=1` (see code/CLI notes below).
  - Hook scripts themselves also check the gate and exit early when disabled.

---

## Executive TL;DR (what to change first)

1. **Adopt a repo‑scoped, portable, privacy‑safe Project ID** (file marker) and make slug a view, not an identity.
   - Add a stable `project_uid` derived from a committed marker (e.g., `.agent-mail-project-id`) or, if you can’t change the repo, from a non‑committed `.git/agent-mail/project-id`.
   - Slugs can continue to differ (e.g., machine‑local privacy), but all slugs map to one `project_uid`.
   - This solves cross‑worktree and cross‑machine unification without leaking absolute paths.
2. **Make hooks composable and cross‑platform** instead of overwriting.
   - Install a chain‑runner hook that executes `hooks.d/<hook>/*` and falls back to an existing hook body.
   - Ship both POSIX `.sh` and Windows `.cmd/.ps1` variants.
   - Respect `core.hooksPath` and detect Husky/lefthook/pre‑commit frameworks.
3. **Use Git’s pathspec semantics for reservations and matching.**
   - Store patterns as Git wildmatch (not ad‑hoc globs).
   - Compile and match via a Git‑compatible engine (e.g., `pathspec` with `GitWildMatchPattern`).
   - Honor `core.ignorecase` and treat repo‑root relativity exactly like Git does.
4. **Harden pre‑push logic** to correctly diff the to‑be‑pushed range.
   - Parse pre‑push STDIN tuples; for new branches use empty remote SHA as `--not` boundary.
   - Handle force‑pushes, tags, and multiple refspecs.
   - Use `-z` everywhere to be path‑safe.
5. **Add a tiny “adoption/merge” command** to consolidate legacy per‑worktree projects into the canonical one by `project_uid`. No breaking migration; opt‑in admin action.
6. **Return identity + repo facts everywhere** (`git root`, `worktree name`, `branch`, `core.ignorecase`, `sparse-checkout`) so UIs and guards can explain decisions and match Git semantics precisely.
7. **Keep onboarding safe and reversible**: gate all new behavior behind `WORKTREES_ENABLED=1`; start guards in `warn` mode (`AGENT_MAIL_GUARD_MODE=warn`) during trials, then flip to `block`.
   - Guard installer refuses to install when the gate is off (explicit `--force` overrides for power users).

Everything below deepens these, with code and exact behaviors.

---

## 0) Opt‑in switch & safe defaults (tiny but important)

- Global gate: `WORKTREES_ENABLED = 0|1` (default **0**). If `0`, identity stays `dir`, existing slugs are unchanged, and no new hooks/guards/paths are installed.
- Recommended trial posture when enabling: set `AGENT_MAIL_GUARD_MODE=warn` for a sprint, then switch to `block`.
- Per‑feature toggles remain available (e.g., `INSTALL_PREPUSH_GUARD`, `PROJECT_IDENTITY_MODE`), but are ignored unless `WORKTREES_ENABLED=1`.
 - CLI behavior: `mcp-agent-mail guard install` and `am-ports` subcommands must detect the gate and print a short “disabled (WORKTREES_ENABLED=0)” message instead of mutating state.

Implementation progress:
- 2025-11-10: Introduced `WORKTREES_ENABLED` (default false) into application config:
  - Added `worktrees_enabled: bool` to `Settings` in `src/mcp_agent_mail/config.py`, read via python‑decouple with default `"false"`.
  - No changes to `.env` were made; if absent or set to false, the system continues with existing behavior unchanged.
  - Guard installer gating:
    - `mcp_agent_mail.cli guard install` now no‑ops with a clear message when `WORKTREES_ENABLED=0`.
    - MCP tool `install_precommit_guard` returns without installing and emits an info message when gated off.
    - Generated pre‑commit hook script checks `WORKTREES_ENABLED` at runtime and exits early when disabled.
  - Identity wiring (no behavior change yet): `_ensure_project` now computes slugs via a `_compute_project_slug` helper that preserves existing `dir` behavior unless the gate is enabled (and even then, remains `dir` until additional identity modes are implemented).
  - Guard advisory mode: pre‑commit script honors `AGENT_MAIL_GUARD_MODE=warn` to print conflicts but not block; default remains `block`.
 - 2025-11-10: Implemented identity modes behind the gate in `src/mcp_agent_mail/app.py`:
   - `PROJECT_IDENTITY_MODE=git-remote|git-toplevel|git-common-dir|dir` supported (default `dir`).
   - `git-remote`: normalize `remote.<name>.url` (default `origin`) to `host/owner/repo`; slug = `repo-<sha1(normalized)[:10]>`.
   - `git-toplevel`: slug = `basename-<sha1(realpath)[:10]>`.  `git-common-dir`: slug = `repo-<sha1(realpath)[:10]>`.
   - On any failure, falls back to `dir` (strict back‑compat). Behavior is unchanged unless `WORKTREES_ENABLED=1`.
  - Added identity inspection resource:
    - `resource://identity/{project}` returns `{ slug, identity_mode_used, canonical_path, human_key, repo_root, git_common_dir, branch, worktree_name, core_ignorecase, normalized_remote }`.
    - `project` can be an absolute path; query parsing is robust to transports that embed params in the path segment.
  - ensure_project extended:
    - Accepts optional `identity_mode` arg (for inspection/testing).
    - Returns identity metadata alongside `{id, slug, human_key, created_at}`.
  - Guard installer now respects Git configuration for hook placement:
    - Honors `core.hooksPath` (absolute or repo-relative) and falls back to `rev-parse --git-dir`/hooks.
    - Creates directories as needed and remains gated by `WORKTREES_ENABLED`.
 - 2025-11-10: Identity canonicalizer with durable `project_uid`:
   - Implemented `_resolve_project_identity(human_key)` that computes slug, mode, canonical path, normalized remote, and `project_uid` via precedence:
     - committed marker `.agent-mail-project-id` → private marker `.git/agent-mail/project-id` → remote fingerprint (`host/owner/repo@default_branch`) → `git-common-dir` hash → directory hash.
   - When `WORKTREES_ENABLED=1` and no marker exists, writes a private marker under `.git/agent-mail/project-id` (non-destructive).
   - `ensure_project` and `resource://identity/{project}` now return `project_uid` in the payload.
 - 2025-11-10: Pathspec matcher (server-side) implemented:
   - Switched server matching in `_file_reservations_conflict` to Git wildmatch semantics (via `pathspec`), with safe fnmatch fallback if the optional dependency is unavailable.
   - Added `_patterns_overlap` heuristic using pathspec for better overlap detection.
 - 2025-11-10: Mail diagnostics CLI:
   - `mcp-agent-mail mail status <path>` prints gate state, identity mode, normalized remote, and the slug that would be used for the path (non-destructive, read-only).
  - 2025-11-10: Guards and installer updates:
    - Added optional `--prepush` flag to `mcp-agent-mail guard install` to install a Python-based pre-push guard that enumerates to-be-pushed commits with `rev-list` and inspects changed paths via `diff-tree`. Honors `WORKTREES_ENABLED` and `AGENT_MAIL_GUARD_MODE=warn`.
    - Hook installer now resolves `core.hooksPath` and `git-dir/hooks` correctly for both `pre-commit` and `pre-push`.
    - Pre-commit hook now:
      - Honors `AGENT_MAIL_BYPASS=1` for emergency bypass.
      - Expands renames/moves via `git diff --cached --name-status -M -z` and checks both old and new names.
    - Pre-push hook now:
      - Honors `AGENT_MAIL_BYPASS=1` and `AGENT_MAIL_GUARD_MODE=warn`.
      - Uses `rev-list` + `diff-tree` and `--no-ext-diff` for correct ranges and NUL-safety.
  - 2025-11-10: Chain-runner hooks implemented (composition-safe):
    - Installer writes a Python chain-runner at `.git/hooks/pre-commit` and `.git/hooks/pre-push` that executes `hooks.d/<hook>/*` in lexical order and then `<hook>.orig` if present.
    - Existing single-file hooks are preserved as `<hook>.orig` on first install; no overwrites.
    - Agent Mail guard is installed as `hooks.d/pre-commit/50-agent-mail.py` and `hooks.d/pre-push/50-agent-mail.py`.
    - Pre-push chain-runner reads STDIN once and forwards it to child hooks, matching Git’s tuple semantics.
    - Uninstall removes only our `50-agent-mail.py` plugins; chain-runner and user hooks remain intact. Legacy single-file Agent Mail hooks are still removed if detected by sentinel.
  - 2025-11-10: Unified guard checker via CLI:
    - Added `mcp-agent-mail guard check` command that reads NUL-delimited paths (`--stdin-nul`) and checks conflicts using Git wildmatch semantics (via `pathspec`), honoring `core.ignorecase`.
    - Hook plugins now delegate to the CLI via `uv run python -m mcp_agent_mail.cli guard check --stdin-nul --repo <root>` so server/CLI/hook semantics cannot drift. Fallback to `python -m mcp_agent_mail.cli ...` when `uv` is unavailable.
    - Advisory mode (`AGENT_MAIL_GUARD_MODE=warn` or `--advisory`) prints conflicts but does not block.
    - Windows: `.cmd` and `.ps1` shims for chain-runner are installed to mirror POSIX behavior.
  - 2025-11-10: Project maintenance CLI:
    - Added `mcp-agent-mail projects adopt <from> <to> --dry-run` that validates same repo (`git-common-dir`) and prints a consolidation plan. (Apply phase to be implemented in a later step.)
  - 2025-11-10: Guard status and adopt apply:
    - `mcp-agent-mail guard status <repo>` prints gate/mode, resolved hooks directory (honors `core.hooksPath`), and presence of `pre-commit`/`pre-push`.
    - `mcp-agent-mail projects adopt <from> <to> --apply` moves Git artifacts within the archive into the target project (preserving history), re-keys DB rows (`agents`, `messages`, `file_reservations`), and records `aliases.json`. Safeguards: requires same repo; aborts on agent-name conflicts to preserve uniqueness.

## 1) Identity: from “slug” to “stable Project UID” (marker → remote → gitdir → dir)

### Why:

- Current `git-…` modes unify worktrees on one machine but not across machines; hashing paths is guessable and not portable.
- Treat slug as a presentation key (disk folder) and add a durable `project_uid` (UUID) as the true identity.

### Mechanism (opt‑in, no disruption):

- On first `ensure_project` in a repo:
  1. If a committed marker exists at repo root (recommended): read it.
     - File name: `.agent-mail-project-id` (committed).
     - Content: a single line UUIDv4 (or ULID).
  2. Else, if a private marker exists: `.git/agent-mail/project-id` (uncommitted).
  3. Else: generate a UUIDv4, write private marker under `.git/agent-mail/project-id`. Expose a CLI to “promote” it to the committed marker later.
- Compute slug exactly as in the existing plan (privacy‑safe in `git-*` modes). Record `(project_uid, slug)`.

### Minimal additive schema (optional but recommended)

- You can avoid a DB migration by keeping `projects.id` as the primary key and storing `project_uid` only in Git metadata. Adding a unique `project_uid` column will make “adopt/merge” trivial:
  - `ALTER TABLE projects ADD COLUMN project_uid TEXT UNIQUE;`
  - Backfill existing rows with generated UUIDs.
  - Look up by `project_uid` first, then by slug.

### Canonicalization order (robust & private; applied only when `WORKTREES_ENABLED`/`GIT_IDENTITY_ENABLED` is on)

1. If committed marker present: `project_uid = read_marker()`.
2. Else if discovery YAML `.agent-mail.yaml` contains `project_uid:`: use it.
3. Else if `PROJECT_IDENTITY_MODE=git-remote` (or remote source is enabled) **and** a normalized remote URL can be derived (default remote: `origin` or `PROJECT_IDENTITY_REMOTE`): unify by remote; generate `project_uid` and persist the private marker for stability across machines.
4. Else if `PROJECT_IDENTITY_MODE in {git-common-dir, git-toplevel}`: compute privacy‑safe slug from canonical Git paths and generate `project_uid`.
5. Else: `dir` mode for slug (back‑compat), but still generate `project_uid` so you can adopt later.
- ### Migration tools
  - `mcp-agent-mail projects mark-identity [--no-commit]` writes `.agent-mail-project-id` with the current `project_uid` and optionally commits it.
  - `mcp-agent-mail projects discovery-init [--product <uid>]` scaffolds `.agent-mail.yaml` with `project_uid:` (and optional `product_uid:`) for discovery/overrides.

### Drop‑in function (replacement for canonicalizer; now supports `git-remote`)

```python
from __future__ import annotations
import hashlib, os, re, subprocess, uuid
from dataclasses import dataclass
from typing import Literal, Optional, Tuple
from urllib.parse import urlparse

IdentityMode = Literal["dir", "git-remote", "git-toplevel", "git-common-dir"]

@dataclass(frozen=True)
class ProjectIdentity:
    project_uid: str
    slug: str
    identity_mode_used: IdentityMode
    canonical_path: str
    human_key: str
    repo_root: Optional[str]
    git_common_dir: Optional[str]
    branch: Optional[str]
    worktree_name: Optional[str]
    core_ignorecase: Optional[bool]
    normalized_remote: Optional[str]

_SLUG_RE = re.compile(r"[^a-z0-9]+")
def slugify(value: str) -> str:
    s = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return s or "project"

def _short_sha1(text: str, n: int = 10) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:n]

def _norm_real(p: str) -> str:
    return os.path.normcase(os.path.realpath(p))

def _git(workdir: str, *args: str) -> Optional[str]:
    try:
        cp = subprocess.run(["git", "-C", workdir, *args], check=True, capture_output=True, text=True)
        return cp.stdout.strip()
    except Exception:
        return None

def _read_file(p: str) -> Optional[str]:
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except Exception:
        return None

def _write_file(p: str, content: str) -> None:
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content + "\n")

def _repo_facts(human_key_real: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[bool]]:
    root = _git(human_key_real, "rev-parse", "--show-toplevel")
    common = _git(human_key_real, "rev-parse", "--git-common-dir")
    branch = _git(human_key_real, "rev-parse", "--abbrev-ref", "HEAD")
    wt = _git(human_key_real, "worktree", "list", "--porcelain")
    worktree_name = None
    if wt and root:
        for block in wt.split("\n\n"):
            lines = [l.strip() for l in block.splitlines() if l.strip()]
            if lines and lines[0].startswith("worktree "):
                path = lines[0].split(" ", 1)[1]
                if _norm_real(path) == _norm_real(root):
                    worktree_name = os.path.basename(path) or None
                    break
    ignorecase = _git(human_key_real, "config", "--get", "core.ignorecase")
    return (root, common, branch, worktree_name, (ignorecase == "true"))

def _norm_remote(url: Optional[str]) -> Optional[str]:
    if not url: return None
    url = url.strip()
    if url.startswith("git@"):
        try:
            host = url.split("@",1)[1].split(":",1)[0]
            path = url.split(":",1)[1]
        except Exception:
            return None
    else:
        try:
            p = urlparse(url)
            host, path = p.hostname, (p.path or "")
        except Exception:
            return None
    if not host: return None
    path = path.lstrip("/")
    if path.endswith(".git"): path = path[:-4]
    parts = [x for x in path.split("/") if x]
    if len(parts) < 2: return None
    owner, repo = parts[0], parts[1]
    return f"{host}/{owner}/{repo}"

def canonicalize_project_identity(
    human_key: str,
    mode: IdentityMode,
    fallback: IdentityMode = "dir",
    allow_commit_marker: bool = False,
) -> ProjectIdentity:
    human_key_real = _norm_real(human_key)
    repo_root, git_common_dir, branch, worktree_name, core_ignorecase = _repo_facts(human_key_real)
    normalized_remote = _norm_remote(_git(human_key_real, "config", "--get", "remote.%s.url" % os.environ.get("PROJECT_IDENTITY_REMOTE","origin")))

    project_uid = None
    marker_committed = os.path.join(repo_root or "", ".agent-mail-project-id") if repo_root else None
    marker_private = os.path.join(git_common_dir or "", "agent-mail", "project-id") if git_common_dir else None

    for p in (marker_committed, marker_private):
        if p:
            project_uid = _read_file(p)
            if project_uid:
                break

    if not project_uid:
        project_uid = str(uuid.uuid4())
        if marker_private:
            _write_file(marker_private, project_uid)
        if allow_commit_marker and marker_committed:
            _write_file(marker_committed, project_uid)

    if mode == "git-remote" and normalized_remote:
        canonical_path = normalized_remote  # privacy-safe, no local paths
        base = (normalized_remote.rsplit("/",1)[-1]) or "repo"
        slug = f"{base}-{_short_sha1(canonical_path)}"
        return ProjectIdentity(project_uid, slug, "git-remote", canonical_path, human_key_real,
                               repo_root, git_common_dir, branch, worktree_name, core_ignorecase, normalized_remote)

    if mode == "git-toplevel" and repo_root:
        canonical_path = _norm_real(repo_root)
        base = os.path.basename(canonical_path) or "repo"
        slug = f"{base}-{_short_sha1(canonical_path)}"
        return ProjectIdentity(project_uid, slug, "git-toplevel", canonical_path, human_key_real,
                               repo_root, git_common_dir, branch, worktree_name, core_ignorecase, normalized_remote)

    if mode == "git-common-dir" and git_common_dir:
        canonical_path = _norm_real(git_common_dir)
        base = "repo"
        slug = f"{base}-{_short_sha1(canonical_path)}"
        return ProjectIdentity(project_uid, slug, "git-common-dir", canonical_path, human_key_real,
                               repo_root, git_common_dir, branch, worktree_name, core_ignorecase, normalized_remote)

    slug = slugify(human_key_real)
    return ProjectIdentity(project_uid, slug, "dir", human_key_real, human_key_real,
                           repo_root, git_common_dir, branch, worktree_name, core_ignorecase, normalized_remote)
```

Behavioral wins:

- Back‑compat: default remains `dir`; slugs identical.
- Opt‑in `git-*` still gives privacy‑safe slugs.
- Cross‑clone/machine stability: `project_uid` binds them all; git‑remote mode makes unrelated clones of the same repo “just work”.
- Admin can later “commit the ID” once they want portable identity in VCS.

---

## 2) Hook installation: composable, idempotent, cross‑platform

### Why:

Overwriting `pre-commit` or `pre-push` breaks existing setups (Husky, pre-commit, lefthook). Worktrees + `core.hooksPath` complicate paths. Windows shells differ.

### Approach:

- Resolve hooks path exactly as previously proposed.
- Install a chain‑runner `pre-commit` / `pre-push` once, which executes `hooks.d/pre-commit/*` (or `/pre-push/*`) in lexical order, then (if present) an original hook saved as `pre-commit.orig` (first install only).
- Drop the Agent Mail guard as `hooks.d/pre-commit/50-agent-mail.sh` (+ `.cmd` on Windows).
- Idempotency: re‑runs do nothing if sentinel lines are present.

#### Chain‑runner (POSIX `pre-commit`)

```bash
#!/usr/bin/env bash
set -euo pipefail
HOOK_NAME="pre-commit"
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$HOOK_DIR/hooks.d/$HOOK_NAME"

if [ -d "$RUN_DIR" ]; then
  while IFS= read -r -d '' f; do
    [ -x "$f" ] || continue
    "$f"
  done < <(find "$RUN_DIR" -maxdepth 1 -type f -perm -111 -print0 | sort -z)
fi

if [ -x "$HOOK_DIR/$HOOK_NAME.orig" ]; then
  "$HOOK_DIR/$HOOK_NAME.orig"
fi
```

#### Guard script (POSIX) placed at `hooks.d/pre-commit/50-agent-mail.sh` (honors advisory mode)

```bash
#!/usr/bin/env bash
set -euo pipefail
if [ "${WORKTREES_ENABLED:-0}" != "1" ]; then exit 0; fi
MODE="${AGENT_MAIL_GUARD_MODE:-block}"
if [ "${AGENT_MAIL_BYPASS:-0}" = "1" ]; then
  echo "[agent-mail] bypass enabled via AGENT_MAIL_BYPASS=1"
  exit 0
fi
ROOT="$(git rev-parse --show-toplevel)"
if [ -z "$ROOT" ]; then exit 0; fi
MAPLIST=()
while IFS= read -r -d '' f; do
  MAPLIST+=("$f")
done < <(git diff --cached -z --name-only --diff-filter=ACMRDTU)
if [ "${#MAPLIST[@]}" -gt 0 ]; then
  if [ "$MODE" = "warn" ]; then
    printf "%s\0" "${MAPLIST[@]}" | uv run python -m mcp_agent_mail.cli guard check --stdin-nul --advisory
    exit 0
  else
    printf "%s\0" "${MAPLIST[@]}" | uv run python -m mcp_agent_mail.cli guard check --stdin-nul
  fi
fi
```

#### Pre‑push “range correct” collector (POSIX) for `hooks.d/pre-push/50-agent-mail.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
if [ "${WORKTREES_ENABLED:-0}" != "1" ]; then exit 0; fi
if [ "${AGENT_MAIL_BYPASS:-0}" = "1" ]; then
  echo "[agent-mail] bypass enabled via AGENT_MAIL_BYPASS=1"
  exit 0
fi
read -r REMOTE REMOTE_URL || true
declare -a ALL COMMITS
while read -r LOCAL_REF LOCAL_SHA REMOTE_REF REMOTE_SHA; do
  [ -n "${LOCAL_SHA:-}" ] || continue
  # enumerate commits that will be pushed for this ref
  while IFS= read -r c; do COMMITS+=("$c"); done < <(git rev-list --topo-order "${LOCAL_SHA}" --not --remotes="${REMOTE}")
done
for c in "${COMMITS[@]}"; do
  # collect changed paths per commit; disable external diffs; NUL-safe
  while IFS= read -r -d '' f; do ALL+=("$f"); done < <(git diff-tree -r --no-commit-id --name-only --no-ext-diff --diff-filter=ACMRDTU -z "$c")
done
if [ "${#ALL[@]}" -gt 0 ]; then
  printf "%s\0" "${ALL[@]}" | uv run python -m mcp_agent_mail.cli guard check --stdin-nul
fi
```

(Ship Windows `.cmd`/`.ps1` wrappers that shell out to the same Python CLI.)

---

## 3) Reservations: Git pathspec semantics, not ad‑hoc globs

### Why:

Developers expect matching to behave exactly like `git add`, `git diff`, `git ls-files`. OS case sensitivity should follow the repo’s `core.ignorecase`, not the host OS.

### Changes:

- Persist patterns verbatim + a compiled “normalized” form (for fast match).
- Treat all patterns as GitWildMatch with `**` semantics, path separators `/`, repo‑root relative unless pattern starts with `:(top)`.
- Honor `core.ignorecase`.
- Support pathspec magic: `:(icase)`, `:(literal)`, `:(glob)`, `:(top)`.

#### Matching engine (Python), using `pathspec`:

```python
from pathspec import PathSpec
from pathspec.patterns.gitwildmatch import GitWildMatchPattern

def compile_git_pathspec(patterns: list[str], ignore_case: bool) -> tuple[PathSpec, bool]:
    if ignore_case:
        patterns = [p.lower() for p in patterns]
    return PathSpec.from_lines(GitWildMatchPattern, patterns), ignore_case

def matches_any(spec: PathSpec, ignore_case: bool, repo_rel_path: str) -> bool:
    p = repo_rel_path.replace("\\", "/")
    if ignore_case:
        p = p.lower()
    return spec.match_file(p)
```

Guard side: compute repo‑relative paths via Git plumbing (`git diff -z`, `git ls-files --full-name -z`). For renames, check both old and new names.

Optional guardrails: warn (not block) on ultra‑broad patterns like `**/*`. In `warn` mode the guard never blocks, it only prints actionable hints.

---

## 4) “Adopt/Merge” legacy projects into canonical one (safe, explicit)

Command: `uv run python -m mcp_agent_mail.cli projects adopt --from <old-slug> --to <project_uid or new-slug> [--dry-run]`

Behavior:

- Validates both projects refer to the same repo (by marker or by heuristic: same `git-common-dir` hash).
- Moves Git artifacts under `projects/<old-slug>/…` into `projects/<new-slug>/…` (preserving history).
- Re‑keys DB rows from `old_project_id` → `new_project_id`.
- Writes `aliases.json` under `projects/<new-slug>/` with `"former_slugs": [...]`.
- Idempotent; dry‑run prints a plan. Adoption is optional.

---

## 5) Guard UX: precise, explainable, Git‑native

- Surface holder, reason, expiry, branch, worktree.
- Include matching patterns and exact staged file(s).
- Respect `AGENT_NAME`; show corrective action.
- Respect `--no-verify` and `AGENT_MAIL_BYPASS=1` with a clear warning.

Sample:

```
✖ Commit blocked by active exclusive reservation

Holder: BlueLake  (branch: feature/auth, worktree: frontend-wt)
Reason: bd-123  •  Expires: 2025-11-10T18:14:09Z

Matched pattern(s):
  - frontend/**          (reservation #4182)
  - frontend/src/auth/*  (reservation #4191)

Your staged changes:
  - frontend/src/auth/login.tsx
  - frontend/src/auth/Guard.tsx

Resolve: wait for expiry, request release, or coordinate in thread [bd-123].
Emergency: set AGENT_MAIL_BYPASS=1 or run 'git commit --no-verify'
```

---

## 6) Pre‑push correctness: tricky cases handled

Handle:

- New branches/tags (remote SHA = zeros → use local root only).
- Force‑push: diff against the old remote tip (pre‑push provides it).
- Multiple refspecs in one push: accumulate all changed files.
- Tags: evaluate too. Always use `-z` and dedupe paths.

---

## 7) Extra robustness for real‑world repos

- Sparse‑checkout: Use Git plumbing; repo‑relative names only.
- LFS: Path-only matching, no special handling required.
- Case‑insensitive filesystems: drive matcher from `core.ignorecase`.
- WSL2: Avoid mixing `C:\` and `/mnt/c`; rely on Git for repo‑relative paths.
- Symlinks: Match the link path, not the target.
- Submodules: Keep phase‑1 behavior (separate projects).
- Network filesystems: Prefer Git facts over `realpath` quirks.

---

## 8) Containerized builds: reliable pattern

- Canonical archive structure:
  - `projects/<slug>/builds/<iso>__<agent>__<branch>.log`
  - `projects/<slug>/artifacts/<agent>/<branch>/<iso>/…`
- Helper: `amctl env` prints `SLUG, PROJECT_UID, BRANCH, AGENT, CACHE_KEY, ARTIFACT_DIR`.
- Cache keys: `am-cache-${PROJECT_UID}-${AGENT_NAME}-${BRANCH}` (use `PROJECT_UID`, not slug).
- Mount per‑agent cache subpaths (npm/pnpm, uv, cargo, etc.). Optionally emit a Mail message with a link to the log on success/failure.

---

## 9) Surfaces & APIs (additive, low risk)

- Tools: keep existing signatures; add `project_uid` in responses.
- New resource (gated by `WORKTREES_ENABLED=1`): `resource://identity?project=<abs-path>` returns:

```json
{
  "project_uid": "uuid",
  "slug": "repo-a1b2c3d4e5",
  "identity_mode_used": "git-remote",
  "repo_root": "/abs/repo",
  "git_common_dir": "/abs/repo/.git",
  "branch": "feature/x",
  "worktree_name": "repo-wt-x",
  "core_ignorecase": true,
  "normalized_remote": "github.com/owner/repo"
}
```

- Guard CLI:
  - `guard check --stdin-nul` (non‑interactive checker; exit code is the decision)
  - `guard status` prints hooks path, chained entries, repo facts, sample match.

---

## 10) Observability & ergonomics

- Structured guard events (JSON on stderr) when blocking: include `project_uid`, `reservation_ids`, `patterns`, `paths`, `agent`, `branch`.
- Web UI identity panel: show `Project UID`, slug(s), repo facts, worktree badges, and `core.ignorecase`.
- Doctor command: `amctl doctor` checks hooks path, chain‑runner presence, `AGENT_NAME`, repo facts, pathspec probe, next actions.

---

## 11) Documentation deltas (short and clear)

- “Worktrees: Recommended configuration” recipe:
  - Per‑worktree `.envrc` setting `AGENT_NAME`
  - Enabling `PROJECT_IDENTITY_MODE=git-common-dir` on server
  - (Optional) `amctl project-id --commit` to create `.agent-mail-project-id` for cross‑machine stability
  - Installing guards once; verifying with `guard status`
- Explain why Git wildmatch is used and how to write good patterns; include a table mapping shell globs → Git pathspec.

---

## 12) Test plan upgrades

- Cross‑machine slug divergence but shared `project_uid`: both write to same project by UID while slugs differ.
- Pre‑push new branch (remote zeros).
- Force‑push updating history.
- Rename detection: check both old/new names.
- `core.ignorecase=true` repo on case‑insensitive FS: `Auth.tsx` vs `auth.tsx` reservations.
- Hook composition: existing Husky hook continues to run (assert order and non‑duplication).
- NUL‑byte safety: filenames with spaces/newlines/UTF‑8; use `-z` end‑to‑end.
- WSL path formats: guard check succeeds with repo‑relative paths from Git plumbing.
 - Port allocator: `assign` returns unique ports within range; `release` frees; sweeper reaps expired leases.
 - Guard advisory mode: with `AGENT_MAIL_GUARD_MODE=warn`, commits never block but emit conflicts.
 - Remote override marker: `.agent-mail-remote` takes precedence when present and `WORKTREES_ENABLED=1`.

---

## Small but sharp correctness fixes

- Read pre‑push STDIN tuples (above) rather than diffing remote tracking branches (which may be stale).
- For privacy‑safe slugs, prefer Git’s absolute plumbing where available:
  - `git rev-parse --path-format=absolute --show-toplevel`
  - `git rev-parse --git-common-dir`
- Treat zero‑length patterns or patterns containing `..` as invalid and reject early.
 - Honor the global gate: if `WORKTREES_ENABLED=0`, skip worktree‑specific logic entirely and behave as today.

---

## 13) Small pragmatic add‑ons (opt‑in, minimal)

### A) Lightweight port allocator (prevents dev‑server clashes without containers)
- CLI only; no daemon. Requires `WORKTREES_ENABLED=1`.
- Commands:
  - `am-ports assign <service> [--range 3000-3999]` → prints a free port and writes a small lease file under `projects/<slug>/ports/<service>/<agent>__<branch>.json` with TTL (default 4h).
  - `am-ports release <service>` → removes the lease.
  - `am-ports list` → shows current leases.
- `am-run` auto‑renews the lease while the process is alive. Leases are reaped by the sweeper when TTL expires.

### B) Remote override marker (helps mirrors/renamed remotes)
- Optional repo‑root file `.agent-mail-remote` containing a single normalized value like `github.com/owner/repo`.
- When `WORKTREES_ENABLED=1` and `PROJECT_IDENTITY_MODE=git-remote`, this marker wins over `remote.origin.url` normalization.

### C) Guard advisory mode (gentle onboarding)
- `AGENT_MAIL_GUARD_MODE=warn` prints conflicts but does not block; pairs well with pilots.
- CI can still enforce blocking (see snippet below) while local dev runs in `warn`.

### D) Minimal CI/PR check (optional)
Add a tiny GitHub Action step to catch bypassed local hooks:
```yaml
- uses: actions/checkout@v4
- run: uv run python -m mcp_agent_mail.cli guard check --range ${{ github.event.pull_request.base.sha }}..${{ github.event.pull_request.head.sha }} --ci
```

---

## What this buys you

- No default breakage; everything stays opt‑in.
- Agents in multiple worktrees share one project across machines, not just locally.
- Hooks don’t fight with existing tooling.
- Guards are Git‑native, explainable, and safe on every platform.
- Clean adoption path and reversible story for legacy slugs.

---

## Optional (speculative, flagged)

- (Speculative) Add a `project_uid` → network namespace mapping for multi‑repo products (frontend/backend).
- (Speculative) CI gate: GitHub Action/CI step that runs `guard check` on the merge diff to protect `main` against bypassed local hooks.

---

### Drop‑in lists to implement next

1. Implement `ProjectIdentity` (marker→git‑remote→gitdir→dir) and thread `project_uid` through `ensure_project`, macros, and guard CLI responses.
2. Chain‑runner hook (+ Windows shims) and guards; switch installer to compose rather than replace.
3. Add `pathspec` dependency; move matching to Git wildmatch; honor `core.ignorecase`.
4. Implement `projects adopt` command (dry‑run + apply).
5. Add `resource://identity` panel + `mail status` command with routing diagnostics.
6. Ship `am-run` and `amctl env` for build isolation; wire build slots enforcement into the runner.
7. (Phase 2) Add Product Bus: `ensure_product`, `products.link`, product‑wide thread/search resources.

---

## Requirements

- **Multi‑clone / multi‑machine unification**: agents across separate clones of the same GitHub repo should automatically share one project bus.
- **Build isolation**: watchers, caches, and dev servers must not interfere across agents; file reservations alone are insufficient.
- **Multi‑repo workflows**: first‑class support for products spanning multiple repos.
- **Identity fix**: avoid “project = absolute path” to eliminate the core impedance mismatch.

---

## A. Identity model: portable and zero‑config across clones

Keep the opt‑in canonicalizer, but add two identity sources and a precedence rule so separate clones of the same GH repo unify automatically, even across machines.

Identity precedence (first hit wins):

1. Committed marker: `.agent-mail-project-id` at repo root → `project_uid`.
2. Remote fingerprint (NEW): normalized `origin` URL + default branch → `remote_uid`.
   - Example canonical form: `github.com/owner/repo@main`
   - Hash to privacy‑safe `remote_uid = sha1(canonical)[:20]`.
3. Git common dir (existing `git-common-dir` mode).
4. Dir hash (existing `dir` mode; strict back‑compat).

Behavior:

- If (1) exists, use it and you’re done.
- Else if (2) exists, auto‑unify all clones with the same `remote_uid` into one project bus on first contact. Later, admins can “promote” this to (1) with a single command if they want a committed ID.
- Else fall back to current modes.

Complete helper (drop‑in ready):

```python
import hashlib, os, re, subprocess, uuid
from dataclasses import dataclass
from urllib.parse import urlparse

def _git(C, *args):
    try:
        out = subprocess.run(["git","-C",C,*args],check=True,capture_output=True,text=True).stdout.strip()
        return out or None
    except Exception:
        return None

def _norm_remote(url: str) -> str|None:
    # ssh: git@github.com:org/repo.git ; https: https://github.com/org/repo.git ; ssh://git@...
    if not url: return None
    url = url.strip()
    if url.startswith("git@"):
        host_path = url.split(":",1)
        if len(host_path)!=2: return None
        host = host_path[0].split("@",1)[1]
        path = host_path[1]
    else:
        p = urlparse(url)
        host = p.hostname
        path = (p.path or "").lstrip("/")
    if not host or not path: return None
    if path.endswith(".git"): path = path[:-4]
    parts = [x for x in path.split("/") if x]
    if len(parts)<2: return None
    owner, repo = parts[0], parts[1]
    return f"{host}/{owner}/{repo}"

def resolve_identity(human_path: str, allow_commit_marker=False):
    C = os.path.realpath(human_path)
    root = _git(C, "rev-parse", "--show-toplevel")
    gdir = _git(C, "rev-parse", "--git-common-dir")
    # 1) committed marker
    uid = None
    if root:
        marker = os.path.join(root, ".agent-mail-project-id")
        if os.path.exists(marker):
            uid = open(marker,"r",encoding="utf-8").read().strip() or None
    # 2) remote fingerprint
    remote_uid = None
    if not uid:
        url = _git(C,"config","--get","remote.origin.url")
        norm = _norm_remote(url or "")
        if norm:
            head = _git(C,"symbolic-ref","refs/remotes/origin/HEAD")
            if head and head.startswith("refs/remotes/origin/"):
                default_branch = head.rsplit("/",1)[-1]
            else:
                default_branch = "main"
            canonical = f"{norm}@{default_branch}"
            remote_uid = hashlib.sha1(canonical.encode()).hexdigest()[:20]
            uid = uid or remote_uid
    # 3) fallback: git-common-dir / dir
    if not uid and gdir:
        uid = hashlib.sha1(os.path.realpath(gdir).encode()).hexdigest()[:20]
    if not uid:
        uid = hashlib.sha1(os.path.realpath(C).encode()).hexdigest()[:20]
    # promote to committed marker if requested
    if allow_commit_marker and root:
        with open(os.path.join(root,".agent-mail-project-id"),"w",encoding="utf-8") as f:
            f.write(uid+"\n")
    return {"project_uid": uid, "remote_uid": remote_uid, "repo_root": root, "git_common_dir": gdir}
```

Server knobs:

- `PROJECT_IDENTITY_SOURCES=marker,remote,git-common-dir,dir` (default keeps `remote` enabled)
- `PROJECT_IDENTITY_REMOTE_REQUIRED=false` (set true in enterprises that rely on GH/GitLab canonicalization)

Outcome: different clones of the same GH repo become one mailbox automatically, no UI linking required, no worktree requirement.

---

## B. (Phase 2, optional) Multi‑repo “product bus” with first‑class threads

Elevate project (repo) into a product group:

- Product UID: `product_uid` groups many `project_uid`s. Source it from:
  1. A committed file `product.toml` with a stable `product_uid`, or
  2. A heuristically built group: same GH owner + repo name prefixes + UI discovery.
- Unified thread resolution: if a `thread_id` looks like a task key (`bd-###`, `TKT-###`), show one thread merged across all projects in the group (server read‑only view that stitches by `thread_id`).
- Group‑level addressing: `to: ["@frontend", "@backend"]` maps to agent lists in member projects (policy‑checked).
- Group default auto‑handshake: within `product_uid`, new contacts are auto‑approved (TTL‑limited, configurable).

Additive tables (tiny):

- `product_groups(product_uid TEXT PK, name TEXT)`
- `product_members(product_uid, project_uid, UNIQUE(product_uid, project_uid))`

Resources (Phase 2):

- `resource://product/{product_uid}/thread/{id}`
- `resource://product/{product_uid}/search?q=...`

---

## C. Build interference: beyond file reservations

Reservations alone won’t tame devservers, watchers, and caches. Add execution‑domain controls for heavy writers.

1. Build Slots (coarse concurrency control)
   - `acquire_build_slot(project_uid, slot="frontend-build", ttl=3600, exclusive=true|false)`
   - Defaults: `frontend-build`, `backend-build`, `db-migrate`, `e2e`.
   - Guards read slot state but don’t block commits; slots are enforced by runners/macros.
   - Heartbeat/renew; stale slots auto‑reaped.
2. Guarded runners (wrap long‑running tools)
   - `am-run <slot> -- <cmd...>` acquires slot, prepares per‑agent cache volumes, streams logs to `projects/<slug>/builds/...`.
   - Works on Windows/macOS/Linux (Python wrapper + file lock + DB row).
3. Per‑agent caches and outputs
   - Cache root keyed by `project_uid/agent_name/branch` (not slug).
   - Default mappings for npm, uv, cargo, gradle; wrapper sets env so agents never collide.
4. Opt‑in write fences for hot paths
   - Lightweight FS watcher that logs and warns (not blocks) if a process writes inside another agent’s active reservation.

---

## D. Zero‑friction onboarding for clones

- Auto‑adopt wizard: when server first sees two projects with the same `remote_uid`, propose one‑click adopt/merge into a canonical `project_uid` (manual CLI remains).
- Implicit auto‑link: messaging between agents with equal `remote_uid` is allowed immediately under default `auto` policy (same repo), no contact request UX.

---

## E. Path semantics: match Git exactly

Keep repo‑root path semantics but switch reservations to Git wildmatch and honor `core.ignorecase` (via `pathspec`). Ensure rename and sparse‑checkout behave predictably.

---

## F. Hooks: compose, don’t replace

Keep the chain‑runner; ship Windows `.cmd/.ps1` variants so Husky/pre‑commit users aren’t broken. Treat pre‑push ranges correctly (read STDIN tuples; new branches; force‑pushes).

---

## G. Routing policy: “same‑repo easy, cross‑repo explicit”

- Same `project_uid` or same `remote_uid` → delivery always allowed (subject to per‑agent policy), no handshake.
- Same `product_uid` but different projects → auto‑approve with TTL and log the implicit link.
- Different products → current handshake flow.

---

## H. UX/Observability tweaks that reduce confusion

- “Why aren’t they talking?” banner with explicit reason and next action.
- Identity debugger `resource://identity/evidence?project=<path>` returning all sources considered (marker, remote, gitdir, dir) and which one won.
- Product view: single inbox listing per `product_uid`, even if messages live in many repo archives.

---

## I. Minimal schema changes (optional but clean)

- Add `project_uid` and `remote_uid` columns to `projects` (unique).
- Add `product_groups` and `product_members` (see above).
- If you defer migration, keep `project_uid` in Git metadata and a small cache table; behaviors still work.

---

## K. Short docs additions

1. How identity is resolved (marker → remote → gitdir → dir), with a one‑liner to promote to a committed ID.
2. Same‑repo zero‑config: “If the remotes match, you’re already in one mailbox.”
3. Product groups: how to opt in (commit `product.toml` or accept UI suggestion).
4. Build slots & `am-run`: examples for devserver, tests, migrations.
5. Windows hooks: what’s installed and why it won’t break Husky/pre‑commit.

---

## L. What to trim or de‑emphasize from the original plan

- You don’t need users to pick between `git-toplevel` and `git-common-dir` to solve multi‑clone/multi‑machine; remote fingerprint handles this cleanly. Keep git‑dir modes as advanced options, not the primary story.
- Don’t require containers in the core plan; keep them as optional stronger‑isolation on top of build slots (which cover most pain with less friction).

---

Net effect:

- Same GitHub repo feels like one project across clones, machines, and worktrees—zero configuration.
- Multi‑repo products have a first‑class address book and shared threads, so cross‑repo feels designed, not bolted on.
- Builds/watchers have a dedicated coordination primitive (slots) that makes interference rare even for large teams.
- All of this remains opt‑in and back‑compatible with the current `dir` default.

---

## Why Git worktrees for agents

- Separate worktrees isolate branch state, indexes, and build outputs while sharing the same underlying repository object database.
- Multiple coding agents (e.g., Claude Code, Codex) can iterate in parallel on separate branches without stomping on each other's working directories.
- The coordination layer (Agent Mail) should treat these separate worktrees as the same "project bus" when desired.

---

## Current system touch-points

- Project identity is currently derived from `human_key` (absolute working directory path). The system stores `human_key` and derives a `slug` from it.
- Tools and macros (e.g., `ensure_project`, `register_agent`, `macro_start_session`) use the `slug` to address a project archive under `projects/{slug}`.
- A pre-commit guard installs to `<repo>/.git/hooks/pre-commit` and consults file reservations stored in the archive.

---

## Proposed changes (non-disruptive and additive)

### 1) Project identity and precedence (supersedes earlier slug‑only canonicalizer)

- Identity is `project_uid` (UUID). Slug is a presentation key.
- Precedence: marker → git‑remote → `git-common-dir` → `git-toplevel` → `dir`.
- Git‑remote normalizes SSH/HTTPS/file URLs to `host/owner/repo` and hashes it for privacy; supports `PROJECT_IDENTITY_REMOTE` and `PROJECT_IDENTITY_REMOTE_URL_OVERRIDE`.
- First successful resolution also writes a private marker under `.git/agent-mail/project-id` for cross‑machine stability. A CLI can “promote” to a committed marker later.

### 2) Worktree-aware pre-commit guard installation

- **Problem**: In a linked worktree, `<worktree>/.git` is a file pointing at a per-worktree gitdir; naive `repo/.git/hooks` paths may be wrong.
- **Solution**: Update installation logic to discover the correct hooks directory by preference:
  1. `git -C <repo> config --get core.hooksPath` → if set, use this directory (resolve relative to repo root; create if missing).
  2. Else `git -C <repo> rev-parse --git-dir` → use `GIT_DIR/hooks`.
- Always create the `hooks` directory if missing and write POSIX-executable `pre-commit` file.
- **Add optional `pre-push` guard** (server config flag: `INSTALL_PREPUSH_GUARD=true`) to catch conflicts before cross-worktree pushes.
- Print resolved hook path(s) after successful install for transparency.

Guard semantics:

- Check only **staged paths** using `git diff --cached --name-only --diff-filter=ACMRDTU` (pre-commit) or `git diff --name-only <local>..<remote>` (pre-push).
- Normalize all paths to **repo-root relative** for matching against reservations (using `git rev-parse --show-prefix` to strip worktree subdir).
- Handle **renames/moves** by checking both the new path (always) and old path when available via `git diff --name-status -M`.
- Match paths against active exclusive file reservations stored in the archive using repo-root relative patterns.
- Continue requiring `AGENT_NAME` to be set in the environment (recommended via per-worktree `.envrc`).

Emergency bypass:

- **`AGENT_MAIL_BYPASS=1`** environment variable allows proceeding despite conflicts (still logs a warning that bypass occurred).
- Standard `git --no-verify` remains as native Git fallback.

Error messages:

- Use rich-styled, actionable output showing:
  - The exact reservation(s) that block the commit.
  - Holder agent name, expiry timestamp, and reason.
  - How to resolve: wait for expiry, contact holder, use bypass, or `--no-verify`.

Worktree notes:

- For linked worktrees, `rev-parse --git-dir` points to the per-worktree gitdir (e.g., `<common>/.git/worktrees/<name>`). Git evaluates hooks from that location unless `core.hooksPath` is set; supporting either covers modern setups.
- If an organization uses `core.hooksPath` globally for tooling like Husky, we respect it.

Reference implementation (hooks path resolution):

```bash
# POSIX shell snippet to resolve hooks dir from <repo>
resolve_hooks_dir() {
  repo="${1:?repo path required}"

  # Prefer local core.hooksPath if set
  hooks_path="$(git -C "$repo" config --get core.hooksPath 2>/dev/null || true)"
  if [ -n "$hooks_path" ]; then
    # If relative, resolve relative to repo root (git treats it as repo-relative)
    case "$hooks_path" in
      /*) resolved="$hooks_path" ;;
      *) resolved="$repo/$hooks_path" ;;
    esac
    mkdir -p "$resolved"
    printf "%s\n" "$resolved"
    return 0
  fi

  # Fall back to git-dir/hooks
  git_dir="$(git -C "$repo" rev-parse --git-dir 2>/dev/null || true)"
  if [ -z "$git_dir" ]; then
    echo "fatal: not a git repo: $repo" >&2
    return 1
  fi

  case "$git_dir" in
    /*) hooks="$git_dir/hooks" ;;
    *) hooks="$repo/$git_dir/hooks" ;;
  esac
  mkdir -p "$hooks"
  printf "%s\n" "$hooks"
}
```

### 3) File reservation path semantics (repo-root relative)

- **Store and match reservation patterns against repo-root relative paths**, never absolute OS paths.
- Always normalize path separators (`/` vs `\`) and case (on case-insensitive filesystems).
- Encourage narrower patterns in docs and UI.
- Consider lightweight server-side validation that warns on very broad patterns (e.g., `**/*` or `*`).
- Include `branch` and `worktree_name` context in reservation metadata (non-blocking; purely informational) to improve triage messages and debugging.

Path normalization:

- When receiving a reservation request with patterns like `app/api/*.py`:
  - Store exactly as provided (repo-root relative).
- When the guard checks staged files:
  - Get repo-root relative paths via `git diff --cached --name-only`.
  - Normalize separators and case.
  - Match using Git wildmatch pathspec (via `pathspec`/`GitWildMatchPattern`), honoring `core.ignorecase`. Renames check both old and new names.

### 4) Containerized builds to avoid cross-worktree conflict (optional)

- **Goal**: Eliminate shared build artifacts and cache clashes across multiple worktrees and agents.
- **Pattern**: Each agent runs builds in an ephemeral container, mounting only its own worktree and writing logs/artifacts to isolated locations.

Recommended approach:

- Use `docker buildx`/BuildKit or `docker run` for scripted builds.
- Mount the agent's worktree read-write at a stable path (`/workspace`).
- Mount a dedicated build cache volume keyed by canonical project slug + agent name + branch (e.g., `am-cache-{slug}-{agent}-{branch}`) to accelerate repeat builds without colliding with others.
- Emit logs and artifacts to the Agent Mail archive under `projects/{slug}/builds/` and `projects/{slug}/artifacts/` using timestamped, agent-scoped directories.

Example command patterns (illustrative):

```bash
# Build with BuildKit, per-worktree cache, plain progress for log capture
export SLUG="$(amctl slug --mode=${PROJECT_IDENTITY_MODE:-dir} --path="$PWD")"
export AGENT_NAME=${AGENT_NAME:?set per worktree}
export BRANCH="$(git rev-parse --abbrev-ref HEAD)"
export CACHE_VOL="am-cache-${SLUG}-${AGENT_NAME}-${BRANCH}"

# Using docker buildx local cache
docker buildx build \
  --progress=plain \
  --build-arg BUILDKIT_INLINE_CACHE=1 \
  --cache-from type=local,src=/var/lib/docker/volumes/${CACHE_VOL}/_data \
  --cache-to type=local,dest=/var/lib/docker/volumes/${CACHE_VOL}/_data,mode=max \
  -t am-build:${SLUG}-${AGENT_NAME}-${BRANCH} . | tee \
  "${ARCHIVE_ROOT}/projects/${SLUG}/builds/$(date -u +%Y%m%dT%H%M%SZ)__${AGENT_NAME}__${BRANCH}.log"

# Or a run-based build wrapper that writes artifacts to /out
docker run --rm \
  -v "$PWD":/workspace \
  -v ${CACHE_VOL}:/cache \
  -v "${ARCHIVE_ROOT}/projects/${SLUG}/artifacts":/out \
  -w /workspace \
  build-image:latest \
  bash -lc 'make clean && make build && cp -r build/* /out/${AGENT_NAME}/'
```

Notes:

- For language-specific caches (e.g., Node, uv, Cargo, Gradle), map per-agent volumes or subpaths under `/cache` to avoid conflicts.
- Prefer deterministic, hermetic builds; avoid reading host user caches.
- If the project uses GPU or OS-specific toolchains, create per-agent build profiles or different builder instances.

Optional server assistance:

- Provide a small CLI helper (future) to compute the canonical `slug` from `PROJECT_IDENTITY_MODE` and current path (`amctl slug …`), and to register a per-worktree build cache name and artifact/log paths.

### 5) Operational conventions for worktrees

- Per-worktree environment:
  - Set `AGENT_NAME` via `.envrc` in each worktree for precise attribution in guards and messages.
  - Optionally set `AGENT_MAIL_PROJECT_IDENTITY_MODE` (if we add a client-side helper) to match server mode.
- File reservations:
  - Reserve repo-root relative patterns (e.g., `app/api/*.py`). The pre-commit guard runs in the current worktree and checks staged paths against the shared archive's reservations.
  - Prefer tighter patterns (e.g., `frontend/**` vs `**/*`) to reduce unnecessary conflicts across teams.
- Messaging & threads:
  - Keep a single `thread_id` per task/ticket (e.g., `task-123` or `bd-123`) so summaries and action items stitch together across agents/worktrees.

---

## API/behavior changes (additive)

- Server settings (env/flag):
  - `WORKTREES_ENABLED = true|false` (default `false`; gate for all features in this plan)
  - `PROJECT_IDENTITY_MODE = dir|git-remote|git-common-dir|git-toplevel` (default `dir`)
  - `PROJECT_IDENTITY_FALLBACK = dir`
  - `PROJECT_IDENTITY_REMOTE = origin`
  - `PROJECT_IDENTITY_REMOTE_URL_OVERRIDE =` (optional)
  - `INSTALL_PREPUSH_GUARD = true|false` (default `false`)
  - `PROJECT_PRODUCT_UID =` (Phase 2, optional)
  - `AGENT_MAIL_GUARD_MODE = block|warn` (default `block`)
- Tool/macro enhancements (non-breaking):
  - `ensure_project(human_key: str, identity_mode?: str)` → returns `{ project_uid, slug, normalized_remote?, identity_mode_used, canonical_path, human_key, ... }`
  - `macro_start_session(...)` → returns the same, plus `product_uid?`
  - `ensure_product(product_uid?: str, name?: str)` and `products.link(project_uid, product_uid)` (Phase 2)
  - Guard install: `install_guard(project_key: str, repo_path: str, install_prepush?: bool)` → prints resolved hook paths
  - Ports: `am-ports assign|release|list` (no daemon; file‑based leases with TTL)
- Transparency resource (read-only):
  - `resource://identity?project=<abs-path>` → returns identity result + evidence (marker/remote/gitdir/dir)
  - `resource://product/{product_uid}/thread/{id}` and `/search` (product‑wide reads) (Phase 2)
- Guard utilities:
  - `guard status` subcommand: prints current agent, repo root, hooks path, `project_uid`, `normalized_remote`, sample reservation matches, and bypass hints.
  - `mail status` subcommand: prints routing diagnostics (why DMs did/didn’t deliver and one‑line fixes).
  - `guard check --range A..B` for CI/PR diffs; `--advisory` to emit warnings only.

No DB migrations required for phase 1:

- Existing `projects` rows (with per-worktree slugs) remain valid.
- When `git-…` identity is enabled, new/returning calls will hit the canonical project (shared slug) moving forward.

---

## Observability and UX

- **Rich-styled server logs** for canonicalization decisions, conflicts, and guard install:
  - One-line "why" with mode/fallback and resolved paths.
  - Color-coded conflict messages with holder info and expiry.
- **Guard status command** that prints:
  - Current `AGENT_NAME` (or "not set").
  - Repo root and resolved hooks path.
  - Sample reservation matches for context.
  - How to bypass in emergencies (`AGENT_MAIL_BYPASS=1` or `--no-verify`).
- **Actionable error messages** from guards:
  - Show exact reservation(s) blocking the commit.
  - Include holder agent name, expiry timestamp, reason.
  - Suggest resolution steps.

---

## Rollout plan

0. Enablement is explicit: roll pilots by setting `WORKTREES_ENABLED=1` in a single repo/team; start with `AGENT_MAIL_GUARD_MODE=warn`, then flip to `block`.
1. Ship server setting and canonicalizer:
   - Implement marker→git‑remote→gitdir→dir identity with durable `project_uid`; privacy‑safe slugs.
   - Ensure `dir` mode uses existing `slugify()` function for 100% backward compatibility.
   - Return structured identity metadata from `ensure_project`/`macro_start_session`.
   - Log canonicalization decisions with rich-styled output.
   - Default `dir` to preserve existing behavior (no changes to existing slugs).
2. Update guard installer:
   - Resolve hook path as described; verify behavior for monorepo, bare clone + worktrees, and `core.hooksPath`.
   - Add optional `pre-push` guard installation.
   - Implement repo-root relative path matching with normalization.
   - Add rename/move handling in path collection.
   - Add `AGENT_MAIL_BYPASS=1` emergency bypass.
   - Rich-styled error messages with actionable guidance.
3. File reservation enhancements:
   - Normalize to repo-root relative storage and Git pathspec matching.
   - Add branch/worktree context to metadata.
   - Server-side validation warning for overly broad patterns.
4. Documentation:
   - Update `AGENTS.md` and `README.md` with worktree recipes.
   - Add examples for `.envrc`, guard installation per worktree, identity mode selection.
   - Document edge cases (submodules, bare repos, nested repos, etc.).
   - Change all "uv/pip" references to "uv only" to match repo policy.
5. Optional utilities:
   - Add `amctl env` and `am-run` (build slots + per‑agent caches).
   - Add `guard status` and `mail status`.
   - Add `resource://identity?project=<path>` and product resources for transparency.
6. E2E tests:
   - Simulate two clones and two worktrees of the same repo.
   - Verify shared project slug, shared messaging, and guard conflict detection.
   - Test on case-insensitive FS (macOS/Windows).
   - Test WSL2 path normalization.
   - Test rename/move detection in guards.
   - Test bypass mechanism.
   - Test pre‑push new branch, multiple refspecs, forced updates.
   - Test product bus thread/search across two repos.
7. Gradual adoption:
   - Start with a single team; monitor for confusion or edge cases.
   - Expand once stable.

---

## Test plan (high level)

- Unit tests:
  - **Backward compatibility**:
    - Verify `dir` mode uses existing `slugify()` function.
    - Verify identical slugs for existing projects (no duplicates created).
    - Test that existing project data is found and reused.
  - Canonicalizer:
    - Non-git dir, git repo (no worktrees), linked worktree, two separate clones with same remote.
    - `git-remote`, `git-toplevel`, and `git-common-dir` modes.
    - Fallback behavior when git commands fail.
    - Verify privacy-safe slugging (basename + hash) only applies to git-* modes.
    - Submodules (treated as separate projects).
    - Bare repos.
    - Symlinked worktrees.
    - Case-insensitive filesystems (Windows/macOS).
    - WSL2 path normalization.
  - Guard installer:
    - Path resolution with/without `core.hooksPath`.
    - Relative vs absolute `core.hooksPath`.
    - Per-worktree gitdir resolution.
  - Path matching:
    - Repo-root relative normalization with Git wildmatch and `core.ignorecase`.
    - Rename/move detection.
    - Case-insensitive matching on appropriate filesystems.
- Integration tests:
  - Two agents in two clones; both call `macro_start_session` with `identity_mode=git-remote`. Verify:
    - Same project `slug` returned.
    - Identity metadata includes correct `identity_mode_used` and `canonical_path`.
    - File reservations made by one agent block the other in a different worktree.
    - Messages appear in a single thread.
    - Bypass mechanism works (`AGENT_MAIL_BYPASS=1`).
  - Product bus:
    - Two repos linked under one `product_uid`; verify shared thread view and product-wide search.
  - Pre‑push correctness:
    - New branch, multiple refspecs, forced update ranges.
  - Build isolation smoke test (optional):
    - Run containerized builds in two worktrees concurrently.
    - Verify artifact/log separation and absence of cache conflicts.

---

## Risks and mitigations

- **Risk**: Users opt into `git-…` identity while older data exists with per-worktree slugs.
  - **Mitigation**: This is acceptable; we won't delete historical rows. Add docs to explain. Future enhancement could add "aliases" mapping.
- **Risk**: Hook path resolution differences across Git versions.
  - **Mitigation**: Prefer `core.hooksPath` if set; otherwise `rev-parse --git-dir`. Create the directory explicitly. Test across Git versions.
- **Risk**: Non-git directories when `git-…` identity is set.
  - **Mitigation**: Use configured fallback (`dir`) and log a clear, rich-styled message explaining the fallback.
- **Risk**: Containerized builds increase complexity.
  - **Mitigation**: Provide simple recipes and a small helper script later. Keep it optional and well-documented.
- **Risk**: Path normalization edge cases on different platforms.
  - **Mitigation**: Use `os.path.realpath()` + `os.path.normcase()` consistently. Test on Windows, macOS (case-insensitive), Linux, and WSL2.
- **Risk**: Submodule confusion (users expect unified project across submodule boundaries).
  - **Mitigation**: Document clearly that submodules are separate projects in phase 1. Consider superproject unification as future enhancement.
- **Risk**: Remote mismatches across clones (e.g., `origin` vs `upstream`, different hosts/mirrors).
  - **Mitigation**: Allow `PROJECT_IDENTITY_REMOTE` and `PROJECT_IDENTITY_REMOTE_URL_OVERRIDE`; write a private marker on first resolve; provide `projects adopt` and `mail status` with one‑line fixes.
- **Risk**: Default branch detection variance across hosts.
  - **Mitigation**: Prefer `refs/remotes/<remote>/HEAD` when present; otherwise fall back to `"main"`.
 - **Risk**: Port allocator “lost lease” if a process crashes.
   - **Mitigation**: File‑based TTL + periodic sweeper; `am-run` renews while alive; manual `release` is a no‑op if already reaped.

---

## Developer notes & recipes

Compute canonical identity (manual):

```bash
git rev-parse --show-toplevel    # repo working tree root
git rev-parse --git-common-dir   # shared .git directory across worktrees
git worktree list --porcelain    # enumerate worktrees for debugging
git rev-parse --show-prefix      # get current subdir (for path normalization)
```

Per-worktree environment via direnv (`.envrc`):

```bash
export AGENT_NAME="AliceDev"
# Optional client-side hint if needed later
export AGENT_MAIL_PROJECT_IDENTITY_MODE="git-common-dir"
```

Guard install per worktree (composable, cross‑platform):

```bash
# Install pre-commit guard only
mcp-agent-mail guard install <project-slug-or-human-key> .

# Install both pre-commit and pre-push guards
mcp-agent-mail guard install <project-slug-or-human-key> . --prepush
```

Check guard status / routing:

```bash
mcp-agent-mail guard status .
mcp-agent-mail mail status .
```

Emergency bypass (use sparingly):

```bash
# Bypass Agent Mail guard (still logs warning)
AGENT_MAIL_BYPASS=1 git commit -m "emergency fix"

# Bypass all hooks (native Git)
git commit --no-verify -m "emergency fix"
```

File reservation best practices:

- Prefer more specific, repo-root relative globs (`app/api/*.py` over `**/*`).
- Reserve early; renew as needed; release on completion.
- Check current reservations before starting work.
- Include meaningful `reason` strings to help other agents understand intent.

---

## Acceptance criteria

- **Backward compatibility (critical)**:
  - Default `PROJECT_IDENTITY_MODE=dir` uses the **existing `slugify()` function**.
  - Existing projects continue to work with **identical slugs** (zero changes).
  - No duplicate projects created; existing projects are found and reused.
  - No data migrations required; no disruption to current users.
- With `PROJECT_IDENTITY_MODE=git-common-dir`:
  - Two agents in two linked worktrees of the same repo share the same project `slug` and see a unified inbox/thread set.
  - Slugs are **privacy-safe** (git-* modes only): they contain basename + short hash, never full absolute paths.
  - `ensure_project` and macros return structured identity metadata: `{ slug, identity_mode_used, canonical_path, human_key }`.
  - Pre-commit guards installed in both worktrees consult the same archive and block conflicting commits across worktrees using **repo-root relative path matching**.
  - Rename/move operations are detected and checked correctly.
  - `AGENT_MAIL_BYPASS=1` allows emergency bypass (logged).
  - Rich-styled error messages show exact conflicts with holder info and actionable resolution steps.
- With **git‑remote identity enabled** (or marker present):
  - Agents in different clones on different machines of the same `github.com/owner/repo` communicate without any UI linking on first start; they see one inbox/thread set.
  - With no markers committed and remote enabled, clones unify via remote; a private marker is written for stability.
- Build slots (if enabled):
  - Starting a devserver in one clone while another is building triggers slot contention with a clear message; switching to a different slot avoids collisions.
- Product groups (Phase 2, optional):
  - A product spanning `frontend` and `backend` repos shows a single thread for a shared `thread_id` (e.g., `bd-123`) across both; replies from either side continue the same conversation.
- Hooks (Windows and composition):
  - Windows users with Husky/pre-commit retain their existing hooks; the Agent Mail guard runs in addition to existing hooks via the chain-runner.
- Optional features:
  - `pre-push` guard can be installed and works correctly.
  - `guard status` command provides clear diagnostic info.
  - Containerized builds from multiple worktrees run concurrently without interfering caches or artifacts.
  - With `WORKTREES_ENABLED=0`, behavior is indistinguishable from today across identity, hooks, reservations, and messaging.
  - With `WORKTREES_ENABLED=1` and `AGENT_MAIL_GUARD_MODE=warn`, the system surfaces conflicts without blocking; switching to `block` enforces the same checks.
  - Port allocator prevents common dev‑server port collisions in pilots (no daemon; leases have TTL and are reaped).
- Edge cases handled:
  - Submodules treated as separate projects (documented).
  - Bare repos work with appropriate identity mode.
  - Case-insensitive filesystems (macOS/Windows) normalize correctly.
  - WSL2 path normalization works correctly.
  - Symlinked worktrees resolve to canonical paths.

---

## Future extensions (later phases)

- **Project aliases**: map legacy per-worktree `human_key` values to a canonical `slug` for discoverability.
- **Cross-machine unification**: optional repo-side marker/ID file (e.g., `.agent-mail-project-id`) that overrides path-based slugging for consistent identity across machines.
- **Superproject/submodule unification**: option to treat submodules as part of parent project (requires careful design of path semantics).
- **Server-side build macros**: orchestrate containerized builds and post results/logs as messages in the relevant thread.
- **Reservation conflict prediction**: analyze reservation patterns and warn about potential conflicts before they occur.
- **Cross-repo coordination**: use contact handshakes when different repos need to collaborate (kept separate from this worktree plan).

---

## Implementation checklist

- [x] Add canonicalizer with durable `project_uid` and precedence marker → git‑remote → gitdir → dir; privacy‑safe slugs.
- [x] Ensure `dir` mode uses existing `slugify()` function for 100% backward compatibility.
- [x] Return structured identity metadata from `ensure_project` and `resource://identity`.
- [x] Add rich-styled logging for canonicalization decisions.
  - DONE: identity resolution emits rich-structured context when enabled; tool instrumentation already prints rich panels under `tools_log_enabled`.
- [x] Wire `identity_mode` optional arg into `ensure_project` (macros N/A).
- [x] Update guard installer to honor `core.hooksPath` and per-worktree `git-dir`.
- [x] Add optional `pre-push` guard installation.
- [x] Implement repo-root relative Git pathspec matching with normalization (fallback if missing dependency).
- [x] Add rename/move detection in guard path collection.
- [x] Add `AGENT_MAIL_BYPASS=1` emergency bypass mechanism.
- [x] Implement rich-styled, actionable error messages in guards.
  - DONE: pre-commit and pre-push hooks now print conflict summaries and hints for `AGENT_MAIL_GUARD_MODE=warn` and `AGENT_MAIL_BYPASS=1`; fixed small NUL-safety bug in pre-push collector (changed list population).
- [x] Add branch/worktree context to reservation metadata.
- [x] Add server-side validation warning for overly broad reservation patterns.
- [x] Implement `guard status` subcommand.
- [x] Add `mail status` subcommand and `resource://identity?project=<path>` transparency resource.
- [x] Implement `projects adopt` CLI (dry‑run/apply) and write `aliases.json`.
- [x] Implement Product Bus: `ensure_product`, `products_link`, and product‑wide resources.
  - DONE: Added models (`Product`, `ProductProjectLink`), tools (`ensure_product`, `products_link`), product resource (`resource://product/{key}`), and product-wide search (`search_messages_product`). Tests included.
- [x] Add product‑wide inbox fetch and summarization helpers.
  - DONE: Server tools `fetch_inbox_product`, `summarize_thread_product` aggregate across linked projects with optional LLM refinement. CLI commands added: `products inbox`, `products summarize-thread`.
  - DONE: README and AGENTS updated with examples for product-wide inbox and summarization usage.
- [x] Ship `am-run` and `amctl env` with build slots + per‑agent caches.
  - DONE: `am-run` now auto‑acquires/renews/releases advisory build slots (warn mode prints conflicts), and exports `CACHE_KEY`/`ARTIFACT_DIR` via `amctl env`.
- [x] Update docs (`AGENTS.md`, `README.md`) with worktree guides and edge cases.
  - DONE: Added worktree recipes, guard usage, and build slots sections with examples.
- [x] Change all "uv/pip" references to "uv only".
  - DONE: Owned docs (`README.md`, `AGENTS.md`, plan) contain uv‑only guidance; any remaining `pip` mentions are in third‑party docs retained verbatim.
- [x] Add unit tests for canonicalizer (all edge cases).
  - DONE: added tests for dir-mode fallback, git-common-dir, committed/private marker precedence, and remote fingerprint fallback.
- [x] Add unit tests for guard path resolution and matching.
  - DONE: pre-commit rename detection; pre-push range collection conflicts with real tuple input.
- [x] Add integration tests for worktrees (shared project, cross-worktree conflicts).
  - DONE: worktree identity returns same slug/project_uid; cross-worktree pre-commit conflict blocks.
- [x] Test on case-insensitive filesystems.
  - PARTIAL DONE: unit test validates `core.ignorecase` detection via git config. CI FS matrix still pending.
- [x] Test WSL2 path normalization.
  - DONE: added skip-if-not-WSL2 test to assert POSIX canonical paths under `_resolve_project_identity`.
- [x] Test rename/move detection.
  - DONE: staged rename collected (old+new) triggers conflicts on new path.
- [x] Test bypass mechanism.
  - DONE: AGENT_MAIL_BYPASS=1 exits 0 despite conflicts.
- [x] Optional: CI template that runs `am-run`.
  - DONE: GitHub Actions workflow added (Ubuntu, macOS): runs lint, type-check, tests, and a smoke `am-run` on Ubuntu.
- [x] Optional: publish container recipes.
  - DONE: Added `Dockerfile` (Python 3.14 + uv + git) and `compose.yaml` (port 8765, volume for storage, optional `.env` mount). README includes usage instructions.
