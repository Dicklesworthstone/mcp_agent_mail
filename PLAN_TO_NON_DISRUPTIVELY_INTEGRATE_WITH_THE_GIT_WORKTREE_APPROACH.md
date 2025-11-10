# PLAN: Non-disruptive integration with Git worktrees for multi-agent development

---

## Objectives

- **Primary goal**: Allow agents working in separate Git worktrees of the same repository to share the same MCP Agent Mail project (identities, messages, file reservations), without breaking existing single-directory behavior.
- **Non-goals**:
  - No mandatory migrations that break existing data or workflows.
  - No behavior changes unless explicitly enabled via a startup flag or explicit parameter.
  - No forced editor/tooling changes for users who don't use worktrees.

---

## Design summary (high-signal)

- **Add a project identity canonicalizer (opt-in)** that maps each agent's `human_key` (its working directory) to a shared, stable, privacy-safe identity when the working copy is a Git worktree of the same repository.
  - New server setting/flag: `PROJECT_IDENTITY_MODE = dir|git-toplevel|git-common-dir` (default: `dir`).
  - **`dir` mode (default)**: Uses the existing `slugify()` function for **100% backward compatibility**. Zero changes to existing behavior; existing projects continue to work with identical slugs.
  - **`git-toplevel` and `git-common-dir` modes**: The server computes a privacy-safe project `slug` (human-readable basename + short hash) from a shared canonical path while keeping the original `human_key` unchanged for audit.
  - Privacy-safe slugs never contain absolute paths; they use `basename + sha1(canonical_path)[:10]` for stability and privacy (git-* modes only).
- **Make guard installation work with worktrees** by resolving the real hooks directory via `git config core.hooksPath` or `git rev-parse --git-dir` (supports both monorepos and linked worktrees where `.git` is a file).
  - Add optional `pre-push` guard to catch conflicts before cross-worktree pushes.
  - Include emergency bypass: `AGENT_MAIL_BYPASS=1` (logged).
- **Repo-root relative path matching** for file reservations to ensure cross-worktree consistency.
- **Containerized builds (optional)**: Provide a standard pattern to run isolated builds per worktree using Docker/BuildKit, emitting logs and artifacts to the shared Agent Mail archive, avoiding cross-worktree conflicts.
- **Zero disruption**: All new behavior is gated behind a setting/flag and optional parameters on macros; current users see no change by default. The default `dir` mode uses the existing `slugify()` function, ensuring 100% backward compatibility with identical slugs for existing projects.

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

### 1) Project identity canonicalizer

- **Problem**: Each worktree has a different absolute path, so today each worktree becomes a distinct project (different `slug`).
- **Solution**: Introduce an optional canonicalization step that derives the `slug` from a shared Git identity while keeping `human_key` as the actual working directory.

Configuration (server-side, startup flag/env):

- `PROJECT_IDENTITY_MODE`:
  - `dir` (default): current behavior; slug is based on `human_key`.
  - `git-toplevel`: slug is based on `git rev-parse --show-toplevel` from `human_key`.
  - `git-common-dir`: slug is based on `git rev-parse --git-common-dir` from `human_key`.
- Optional: `PROJECT_IDENTITY_FALLBACK=dir` to define a fallback if Git queries fail.

Behavioral contract:

- `human_key` remains the agent's actual working directory (for audit and ergonomics).
- `slug` computation depends on identity mode:
  - **`dir` mode**: Uses the existing `slugify()` function for **100% backward compatibility**. No changes to existing project slugs.
  - **`git-toplevel` and `git-common-dir` modes**: Uses **privacy-safe** format: `basename + "-" + sha1(canonical_path)[:10]`. Slugs never embed absolute paths; they combine a human-readable name with a short hash for stability.
- All linked worktrees of the same repo share the same `slug` (when using git-* modes).
- Existing APIs remain unchanged; `ensure_project` and macros continue to accept `human_key` or `slug` as today.
- Add optional override parameter to `macro_start_session` and `ensure_project` (e.g., `identity_mode?: str`) to support per-call testing and gradual rollout, but prefer the server-side setting for consistency.
- **Return structured identity metadata**: `{ slug, identity_mode_used, canonical_path, human_key }` from `ensure_project` and macros so clients can show users exactly what happened.

Canonicalization algorithm:

- Resolve `human_key` to an absolute, normalized, realpath using `os.path.realpath()` + `os.path.normcase()` to handle symlinks and case-insensitive filesystems.
- Normalize trailing slashes.
- If `PROJECT_IDENTITY_MODE == dir`:
  - Use **existing `slugify(human_key)` function** for 100% backward compatibility.
  - No changes to slug format; existing projects continue to work.
  - Example: `/data/projects/mcp_agent_mail` → `data-projects-mcp-agent-mail`
- If `git-toplevel`:
  - Run `git rev-parse --show-toplevel` in `human_key`; if success:
    - `slug_source = realpath(normcase(show_toplevel))`
    - `base = os.path.basename(slug_source) or "repo"`
    - `slug = f"{base}-{sha1(slug_source)[:10]}"` (privacy-safe format)
  - If Git command fails, fall back to configured fallback mode.
  - Example: `/data/projects/mcp_agent_mail` → `mcp_agent_mail-a1b2c3d4e5`
- If `git-common-dir`:
  - Run `git rev-parse --git-common-dir` in `human_key`; if success:
    - `slug_source = realpath(normcase(git_common_dir))`
    - `base = "repo"`
    - `slug = f"{base}-{sha1(slug_source)[:10]}"` (privacy-safe format)
  - If Git command fails, fall back to `dir` mode (hard fallback).
  - Example: `/data/projects/mcp_agent_mail/.git` → `repo-f9e8d7c6b5`
- Log canonicalization decision with rich-styled output explaining which mode was used and why, including fallback path when git commands fail.

Edge cases and special Git configurations:

- **Non-git directories**: Falls back to `dir` mode (logged).
- **Bare repositories**: `git-toplevel` will fail; `git-common-dir` will succeed and point to the bare repo location.
- **Submodules**: Each submodule is treated as a separate project by default in phase 1. Superproject unification is not supported initially.
- **Nested repos**: Each repo gets its own identity based on where the git command is run.
- **Detached worktrees**: Behavior follows standard worktree semantics; `git-common-dir` will unify them.
- **Cross-machine**: Slugs are per-machine by default (different absolute paths → different hashes). Cross-machine unification via a repo-side marker/ID is out of scope for phase 1 (optional enhancement later).

Back-compat:

- Default remains `dir`; **zero changes to existing behavior**.
- **`dir` mode uses the existing `slugify()` function**, ensuring existing projects continue to work with identical slugs.
- Existing projects will be found and reused without any migration or duplicate creation.
- No data changes, no migrations, no disruption to current users.
- If users opt in to `git-toplevel` or `git-common-dir` modes later, previously created per-worktree projects (using old `dir` mode slugs) may remain in the DB (not deleted). That is acceptable and auditable; we can optionally add a discoverable "aliases" mapping later (out of scope for this first phase).

Reference implementation:

```python
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Literal, Optional

IdentityMode = Literal["dir", "git-toplevel", "git-common-dir"]

@dataclass(frozen=True)
class ProjectIdentity:
    slug: str
    identity_mode_used: IdentityMode
    canonical_path: str
    human_key: str

def _short_sha1(text: str, length: int = 10) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]

def _norm_real(path: str) -> str:
    """Normalize symlinks and case; keep stable separators."""
    real = os.path.realpath(path)
    return os.path.normcase(real)

def _git(workdir: str, *args: str) -> Optional[str]:
    """Run git command, return stdout or None on failure."""
    try:
        cp = subprocess.run(
            ["git", "-C", workdir, *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return cp.stdout.strip()
    except Exception:
        return None

# Existing slugify function for backward compatibility
_SLUG_RE = re.compile(r"[^a-z0-9]+")

def slugify(value: str) -> str:
    """Normalize a human-readable value into a slug (EXISTING function)."""
    normalized = value.strip().lower()
    slug = _SLUG_RE.sub("-", normalized).strip("-")
    return slug or "project"

def canonicalize_project_identity(
    human_key: str,
    mode: IdentityMode,
    fallback: IdentityMode = "dir",
) -> ProjectIdentity:
    """
    Compute project identity.

    - dir mode: Uses existing slugify() for 100% backward compatibility
    - git-* modes: Use privacy-safe slugging (basename + hash)
    """
    human_key_real = _norm_real(human_key)

    def mk_privacy_safe_slug(base_name: str, source_path: str) -> str:
        """Privacy-safe slug: basename + short hash (git-* modes only)."""
        return f"{base_name}-{_short_sha1(source_path)}"

    used_mode: IdentityMode = mode
    if mode == "git-toplevel":
        top = _git(human_key_real, "rev-parse", "--show-toplevel")
        if top:
            top_real = _norm_real(top)
            base = os.path.basename(top_real) or "repo"
            return ProjectIdentity(
                slug=mk_privacy_safe_slug(base, top_real),
                identity_mode_used="git-toplevel",
                canonical_path=top_real,
                human_key=human_key_real,
            )
        used_mode = fallback

    if used_mode == "git-common-dir":
        common = _git(human_key_real, "rev-parse", "--git-common-dir")
        if common:
            common_real = _norm_real(common)
            base = "repo"
            return ProjectIdentity(
                slug=mk_privacy_safe_slug(base, common_real),
                identity_mode_used="git-common-dir",
                canonical_path=common_real,
                human_key=human_key_real,
            )
        used_mode = "dir"  # hard fallback

    # dir mode: use EXISTING slugify() for backward compatibility
    # This ensures existing projects continue to work without any changes
    return ProjectIdentity(
        slug=slugify(human_key_real),
        identity_mode_used="dir",
        canonical_path=human_key_real,
        human_key=human_key_real,
    )
```

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
  - Match using `fnmatch` against stored patterns (also normalized).

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
  - `PROJECT_IDENTITY_MODE = dir|git-toplevel|git-common-dir` (default `dir`).
  - `PROJECT_IDENTITY_FALLBACK = dir` (default `dir`).
  - `INSTALL_PREPUSH_GUARD = true|false` (default `false`).
- Tool/macro enhancements (non-breaking):
  - `ensure_project(human_key: str, identity_mode?: str)` → returns `{ slug, identity_mode_used, canonical_path, human_key, ... }`
  - `macro_start_session(human_key: str, program: str, model: str, ..., identity_mode?: str)` → returns identity metadata
  - Guard install: `install_guard(project_key: str, repo_path: str, install_prepush?: bool)` → prints resolved hook paths
- Transparency resource (read-only):
  - `resource://identity?project=<abs-path>` → returns canonicalization result and mode for inspection.
- Guard utilities:
  - `guard status` subcommand: prints current agent, repo root, resolved hooks path, sample reservation matches, and how to bypass (for emergencies).

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

1. Ship server setting and canonicalizer:
   - Implement `_canonicalize_project_identity(human_key, mode, fallback)` with privacy-safe slugging for git-* modes only.
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
   - Normalize to repo-root relative storage and matching.
   - Add branch/worktree context to metadata.
   - Server-side validation warning for overly broad patterns.
4. Documentation:
   - Update `AGENTS.md` and `README.md` with worktree recipes.
   - Add examples for `.envrc`, guard installation per worktree, identity mode selection.
   - Document edge cases (submodules, bare repos, nested repos, etc.).
   - Change all "uv/pip" references to "uv only" to match repo policy.
5. Optional utilities:
   - Add CLI helper `amctl slug` for manual inspection.
   - Add `guard status` subcommand.
   - Add `resource://identity?project=<path>` for transparency.
6. E2E tests:
   - Simulate two linked worktrees against the same repo.
   - Verify shared project slug, shared messaging, and guard conflict detection.
   - Test on case-insensitive FS (macOS/Windows).
   - Test WSL2 path normalization.
   - Test rename/move detection in guards.
   - Test bypass mechanism.
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
    - Non-git dir, git repo (no worktrees), linked worktree.
    - Both `git-toplevel` and `git-common-dir` modes.
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
    - Repo-root relative normalization.
    - Rename/move detection.
    - Case-insensitive matching on appropriate filesystems.
- Integration tests:
  - Two agents in two linked worktrees; both call `macro_start_session` with `identity_mode=git-common-dir`. Verify:
    - Same project `slug` returned.
    - Identity metadata includes correct `identity_mode_used` and `canonical_path`.
    - File reservations made by one agent block the other in a different worktree.
    - Messages appear in a single thread.
    - Bypass mechanism works (`AGENT_MAIL_BYPASS=1`).
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

Guard install per worktree (unchanged usage, improved resolution under the hood):

```bash
# Install pre-commit guard only
mcp-agent-mail guard install <project-slug-or-human-key> .

# Install both pre-commit and pre-push guards
mcp-agent-mail guard install <project-slug-or-human-key> . --prepush
```

Check guard status:

```bash
mcp-agent-mail guard status .
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
- Optional features:
  - `pre-push` guard can be installed and works correctly.
  - `guard status` command provides clear diagnostic info.
  - Containerized builds from multiple worktrees run concurrently without interfering caches or artifacts.
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

- [ ] Add canonicalizer with privacy-safe slugging (basename + short hash) for git-* modes only.
- [ ] Ensure `dir` mode uses existing `slugify()` function for 100% backward compatibility.
- [ ] Return structured identity metadata from `ensure_project` and `macro_start_session`.
- [ ] Add rich-styled logging for canonicalization decisions.
- [ ] Wire `identity_mode` optional arg into `ensure_project` and macros.
- [ ] Update guard installer to honor `core.hooksPath` and per-worktree `git-dir`.
- [ ] Add optional `pre-push` guard installation.
- [ ] Implement repo-root relative path matching with normalization.
- [ ] Add rename/move detection in guard path collection.
- [ ] Add `AGENT_MAIL_BYPASS=1` emergency bypass mechanism.
- [ ] Implement rich-styled, actionable error messages in guards.
- [ ] Add branch/worktree context to reservation metadata.
- [ ] Add server-side validation warning for overly broad reservation patterns.
- [ ] Implement `guard status` subcommand.
- [ ] Add `resource://identity?project=<path>` transparency resource.
- [ ] Update docs (`AGENTS.md`, `README.md`) with worktree guides and edge cases.
- [ ] Change all "uv/pip" references to "uv only".
- [ ] Add unit tests for canonicalizer (all edge cases).
- [ ] Add unit tests for guard path resolution and matching.
- [ ] Add integration tests for worktrees (shared project, cross-worktree conflicts).
- [ ] Test on case-insensitive filesystems.
- [ ] Test WSL2 path normalization.
- [ ] Test rename/move detection.
- [ ] Test bypass mechanism.
- [ ] Optional: publish build isolation recipes and CLI helper for slugs.
