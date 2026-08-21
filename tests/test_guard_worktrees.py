"""Tests for guard hook functionality in git worktree scenarios.

Tests guard installation, hook generation, and conflict detection in various
git configurations including worktrees, custom hooksPath, and hook preservation.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from mcp_agent_mail.config import get_settings
from mcp_agent_mail.guard import (
    install_guard,
    install_prepush_guard,
    render_precommit_script,
    render_prepush_script,
    uninstall_guard,
)
from mcp_agent_mail.storage import ensure_archive, write_file_reservation_record


def _init_git_repo(repo_path: Path) -> None:
    """Initialize a git repository."""
    subprocess.run(["git", "init"], cwd=str(repo_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_path), check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo_path), check=True)


def _create_initial_commit(repo_path: Path) -> None:
    """Create an initial commit in the repo."""
    readme = repo_path / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo_path), check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(repo_path), check=True, capture_output=True)


def _create_worktree(main_repo: Path, worktree_path: Path, branch_name: str) -> None:
    """Create a git worktree."""
    subprocess.run(
        ["git", "worktree", "add", str(worktree_path), "-b", branch_name],
        cwd=str(main_repo),
        check=True,
        capture_output=True,
    )


def _run_hook(hook_path: Path, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    """Run a hook script."""
    full_env = os.environ.copy()
    full_env.update(env)
    return subprocess.run(
        ["python", str(hook_path)],
        cwd=str(cwd),
        env=full_env,
        capture_output=True,
        text=True,
    )


def _git_add(repo_path: Path, file_path: str) -> None:
    """Stage a file in a git repository."""
    subprocess.run(["git", "add", file_path], cwd=str(repo_path), check=True)


def _git_config(repo_path: Path, key: str, value: str) -> None:
    """Set a git config value."""
    subprocess.run(["git", "config", key, value], cwd=str(repo_path), check=True)


# =============================================================================
# Basic Worktree Installation Tests
# =============================================================================


@pytest.mark.asyncio
async def test_guard_install_in_worktree(isolated_env, tmp_path: Path):
    """Test guard installation in a git worktree."""
    settings = get_settings()

    # Create main repo with initial commit
    main_repo = tmp_path / "main_repo"
    main_repo.mkdir(parents=True)
    _init_git_repo(main_repo)
    _create_initial_commit(main_repo)

    # Create worktree
    worktree = tmp_path / "worktree"
    _create_worktree(main_repo, worktree, "feature-branch")

    # Install guard in worktree
    await ensure_archive(settings, "worktree-test")
    hook_path = await install_guard(settings, "worktree-test", worktree)

    # Hook should be installed in the worktree's git dir
    assert hook_path.exists()
    assert "pre-commit" in hook_path.name


@pytest.mark.asyncio
async def test_guard_conflict_detection_in_worktree(isolated_env, tmp_path: Path):
    """Test that guard detects conflicts in worktree context."""
    settings = get_settings()

    # Create main repo
    main_repo = tmp_path / "main_repo"
    main_repo.mkdir(parents=True)
    _init_git_repo(main_repo)
    _create_initial_commit(main_repo)

    # Create worktree
    worktree = tmp_path / "worktree"
    _create_worktree(main_repo, worktree, "feature-branch")

    # Create archive with file reservation
    archive = await ensure_archive(settings, "worktree-test")
    await write_file_reservation_record(
        archive,
        {
            "agent": "OtherAgent",
            "path_pattern": "src/*.py",
            "exclusive": True,
        },
    )

    # Render and write the guard script
    script = render_precommit_script(archive)
    script_path = tmp_path / "precommit.py"
    script_path.write_text(script, encoding="utf-8")

    # Stage a conflicting file
    src_dir = worktree / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "app.py").write_text("print('hello')", encoding="utf-8")
    _git_add(worktree, "src/app.py")

    # Run the guard script with WORKTREES_ENABLED
    result = _run_hook(
        script_path,
        worktree,
        {"AGENT_NAME": "MyAgent", "WORKTREES_ENABLED": "1"},
    )

    # Should detect conflict
    assert result.returncode == 1
    assert "conflict" in result.stderr.lower() or "file_reservation" in result.stderr.lower()


# =============================================================================
# Custom core.hooksPath Tests
# =============================================================================


@pytest.mark.asyncio
async def test_guard_install_custom_hookspath(isolated_env, tmp_path: Path):
    """Test guard installation with custom core.hooksPath."""
    settings = get_settings()

    # Create repo
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    # Set custom hooksPath
    custom_hooks = tmp_path / "custom-hooks"
    custom_hooks.mkdir(parents=True)
    _git_config(repo, "core.hooksPath", str(custom_hooks))

    # Install guard
    hook_path = await install_guard(settings, "hookspath-test", repo)

    # Hook should be in custom hooks directory
    assert hook_path.parent == custom_hooks or str(custom_hooks) in str(hook_path)


@pytest.mark.asyncio
async def test_guard_install_relative_hookspath(isolated_env, tmp_path: Path):
    """Test guard installation with relative core.hooksPath."""
    settings = get_settings()

    # Create repo
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    # Set relative hooksPath
    (repo / "my-hooks").mkdir(parents=True)
    _git_config(repo, "core.hooksPath", "my-hooks")

    # Install guard
    hook_path = await install_guard(settings, "rel-hookspath-test", repo)

    # Hook should be resolved relative to repo root
    assert hook_path.exists()


# =============================================================================
# Husky v9 hooksPath Tests (tracked hook must survive the .orig rename)
# =============================================================================


def _write_husky_v9_layout(
    repo: Path,
    tracked_body: str,
    *,
    stub_body: str | None = None,
) -> tuple[Path, Path]:
    """Create a minimal husky v9 layout: .husky/_/{h,pre-commit} + tracked hook.

    Mirrors husky v9's real resolver: ``h`` derives the tracked hook name from
    basename($0) and runs ``.husky/<name>`` if present, else exits 0.

    ``stub_body`` overrides the contents of ``.husky/_/pre-commit`` -- the file
    install_guard preserves as ``pre-commit.orig`` -- so tests can install a
    hand-written hook in the slot where a pure husky stub normally sits.
    """
    husky_dir = repo / ".husky"
    runtime_dir = husky_dir / "_"
    runtime_dir.mkdir(parents=True, exist_ok=True)

    resolver = runtime_dir / "h"
    resolver.write_text(
        "#!/usr/bin/env sh\n"
        'hook_name="${0##*/}"\n'
        'tracked="${0%/*/*}/$hook_name"\n'
        '[ ! -f "$tracked" ] && exit 0\n'
        'sh -e "$tracked" "$@"\n'
        "exit $?\n",
        encoding="utf-8",
    )
    resolver.chmod(0o755)

    stub = runtime_dir / "pre-commit"
    stub.write_text(
        stub_body if stub_body is not None else '#!/usr/bin/env sh\n. "$(dirname "$0")/h"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)

    tracked = husky_dir / "pre-commit"
    tracked.write_text(tracked_body, encoding="utf-8")
    tracked.chmod(0o755)
    return runtime_dir, tracked


@pytest.mark.asyncio
async def test_guard_install_husky_v9_still_runs_tracked_hook(isolated_env, tmp_path: Path):
    """After install_guard on a husky v9 repo, the tracked hook must still run.

    install_guard renames the husky stub to pre-commit.orig. Exec'ing that
    renamed stub gives husky's resolver basename($0) == 'pre-commit.orig',
    so it looks up .husky/pre-commit.orig, misses, and exits 0 -- silently
    skipping the user's tracked hook. The chain-runner must instead invoke
    the resolver with argv0 presenting the real hook name.
    """
    settings = get_settings()
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    runtime_dir, _tracked = _write_husky_v9_layout(
        repo, "#!/usr/bin/env sh\necho HUSKY_TRACKED_RAN\n"
    )
    _git_config(repo, "core.hooksPath", ".husky/_")

    hook_path = await install_guard(settings, "husky-v9-test", repo)

    # Stub was preserved as .orig in the husky runtime dir
    assert (runtime_dir / "pre-commit.orig").exists()

    result = _run_hook(hook_path, repo, {"AGENT_NAME": "TestAgent", "WORKTREES_ENABLED": "1"})
    assert result.returncode == 0, (
        f"chain-runner failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "HUSKY_TRACKED_RAN" in result.stdout, (
        f"tracked husky hook did not run: exit={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


@pytest.mark.asyncio
async def test_guard_install_husky_v9_propagates_tracked_hook_failure(isolated_env, tmp_path: Path):
    """A failing tracked husky hook must fail the whole chain-runner."""
    settings = get_settings()
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    _write_husky_v9_layout(repo, "#!/usr/bin/env sh\necho HUSKY_TRACKED_RAN\nexit 23\n")
    _git_config(repo, "core.hooksPath", ".husky/_")

    hook_path = await install_guard(settings, "husky-v9-fail-test", repo)

    result = _run_hook(hook_path, repo, {"AGENT_NAME": "TestAgent", "WORKTREES_ENABLED": "1"})
    assert "HUSKY_TRACKED_RAN" in result.stdout
    assert result.returncode != 0


@pytest.mark.asyncio
async def test_guard_husky_stub_detection_accepts_shell_param_expansion(isolated_env, tmp_path: Path):
    """A pure stub written the way husky v9 actually emits it is still diverted.

    Real ``.husky/_/<hook>`` stubs source the resolver as ``. "${0%/*}/h"``,
    not via ``dirname``. The stub detector must accept both spellings, or the
    diversion (argv0 = real hook name) never happens and the tracked hook is
    silently skipped again.
    """
    settings = get_settings()
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    _write_husky_v9_layout(
        repo,
        "#!/usr/bin/env sh\necho HUSKY_TRACKED_RAN\n",
        stub_body='#!/usr/bin/env sh\n\n# husky stub\n. "${0%/*}/h"\n',
    )
    _git_config(repo, "core.hooksPath", ".husky/_")

    hook_path = await install_guard(settings, "husky-v9-param-expansion", repo)

    result = _run_hook(hook_path, repo, {"AGENT_NAME": "TestAgent", "WORKTREES_ENABLED": "1"})
    assert result.returncode == 0, (
        f"chain-runner failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "HUSKY_TRACKED_RAN" in result.stdout, (
        f"tracked husky hook did not run: stdout={result.stdout!r} stderr={result.stderr!r}"
    )


@pytest.mark.asyncio
async def test_guard_hybrid_custom_hook_that_also_sources_husky_h_still_runs(
    isolated_env, tmp_path: Path
):
    """A hand-written hook that ALSO sources husky's ``h`` must run its own body.

    A bare ``'/h"' in text`` check misclassifies such a hook as a pure husky
    stub, so the chain-runner sources ``h`` instead of exec'ing the preserved
    ``.orig`` -- and the user's entire custom body is silently discarded. The
    detector must only divert files that are *nothing but* a husky stub.
    """
    settings = get_settings()
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    # NOTE: statements AFTER the source line are deliberately not asserted --
    # husky's resolver ends in `exit`, which terminates the sourcing script.
    # The regression is about the body BEFORE it being dropped entirely.
    _write_husky_v9_layout(
        repo,
        "#!/usr/bin/env sh\necho HUSKY_TRACKED_RAN\n",
        stub_body=(
            "#!/usr/bin/env sh\n"
            "echo CUSTOM_ORIG_RAN\n"
            'touch "$(git rev-parse --show-toplevel)/CUSTOM_RAN"\n'
            '. "$(dirname "$0")/h"\n'
            "echo CUSTOM_TAIL_RAN\n"
        ),
    )
    _git_config(repo, "core.hooksPath", ".husky/_")

    hook_path = await install_guard(settings, "husky-v9-hybrid", repo)

    result = _run_hook(hook_path, repo, {"AGENT_NAME": "TestAgent", "WORKTREES_ENABLED": "1"})
    assert result.returncode == 0, (
        f"chain-runner failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "CUSTOM_ORIG_RAN" in result.stdout, (
        f"custom hook body was silently skipped: stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    assert (repo / "CUSTOM_RAN").exists(), (
        "custom hook body did not execute its side effect"
    )


@pytest.mark.asyncio
async def test_guard_plain_custom_orig_runs_alongside_husky_layout(isolated_env, tmp_path: Path):
    """A custom .orig with no husky reference still runs when ``h`` exists."""
    settings = get_settings()
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    _write_husky_v9_layout(
        repo,
        "#!/usr/bin/env sh\necho HUSKY_TRACKED_RAN\n",
        stub_body=(
            "#!/usr/bin/env sh\n"
            "echo PLAIN_CUSTOM_RAN\n"
            'touch "$(git rev-parse --show-toplevel)/PLAIN_CUSTOM_RAN"\n'
        ),
    )
    _git_config(repo, "core.hooksPath", ".husky/_")

    hook_path = await install_guard(settings, "husky-v9-plain-custom", repo)

    result = _run_hook(hook_path, repo, {"AGENT_NAME": "TestAgent", "WORKTREES_ENABLED": "1"})
    assert result.returncode == 0, (
        f"chain-runner failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "PLAIN_CUSTOM_RAN" in result.stdout
    assert (repo / "PLAIN_CUSTOM_RAN").exists()
    assert "HUSKY_TRACKED_RAN" not in result.stdout, (
        "a custom hook must not be diverted through husky's resolver"
    )


@pytest.mark.asyncio
async def test_guard_husky_stub_with_extra_statement_is_treated_as_custom(
    isolated_env, tmp_path: Path
):
    """A failing custom body that also sources ``h`` must fail the commit.

    Proves the misclassification is not merely cosmetic: when the custom body
    is diverted away, a hook that should have BLOCKED the commit exits 0.
    """
    settings = get_settings()
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    _write_husky_v9_layout(
        repo,
        "#!/usr/bin/env sh\necho HUSKY_TRACKED_RAN\n",
        stub_body=(
            "#!/usr/bin/env sh\n"
            "echo CUSTOM_GATE_RAN\n"
            "exit 42\n"
            '. "$(dirname "$0")/h"\n'
        ),
    )
    _git_config(repo, "core.hooksPath", ".husky/_")

    hook_path = await install_guard(settings, "husky-v9-custom-gate", repo)

    result = _run_hook(hook_path, repo, {"AGENT_NAME": "TestAgent", "WORKTREES_ENABLED": "1"})
    assert "CUSTOM_GATE_RAN" in result.stdout, (
        f"custom gate was skipped: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert result.returncode == 42, (
        f"custom hook failure did not propagate: exit={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


# =============================================================================
# Hook Preservation Tests
# =============================================================================


@pytest.mark.asyncio
async def test_guard_preserves_existing_hook(isolated_env, tmp_path: Path):
    """Test that guard preserves existing pre-commit hook as .orig."""
    settings = get_settings()

    # Create repo
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    # Create existing pre-commit hook
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    existing_hook = hooks_dir / "pre-commit"
    existing_hook.write_text("#!/bin/bash\necho 'existing hook'\n", encoding="utf-8")
    existing_hook.chmod(0o755)

    # Install guard
    await install_guard(settings, "preserve-test", repo)

    # Original hook should be preserved as .orig
    orig_hook = hooks_dir / "pre-commit.orig"
    assert orig_hook.exists()
    assert "existing hook" in orig_hook.read_text()


@pytest.mark.asyncio
async def test_guard_doesnt_overwrite_own_orig(isolated_env, tmp_path: Path):
    """Test that reinstalling guard doesn't overwrite .orig file."""
    settings = get_settings()

    # Create repo
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    # Create existing pre-commit hook
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    existing_hook = hooks_dir / "pre-commit"
    existing_hook.write_text("#!/bin/bash\necho 'original'\n", encoding="utf-8")
    existing_hook.chmod(0o755)

    # Install guard first time
    await install_guard(settings, "preserve-test", repo)

    # Verify .orig was created
    orig_hook = hooks_dir / "pre-commit.orig"
    assert orig_hook.exists()
    original_content = orig_hook.read_text()

    # Install guard second time
    await install_guard(settings, "preserve-test", repo)

    # .orig should still have original content
    assert orig_hook.read_text() == original_content


# =============================================================================
# Gate Variations Tests
# =============================================================================


@pytest.mark.asyncio
async def test_guard_gate_worktrees_enabled_true(isolated_env, tmp_path: Path):
    """Test guard runs when WORKTREES_ENABLED=1."""
    settings = get_settings()
    archive = await ensure_archive(settings, "gate-test")
    script = render_precommit_script(archive)
    script_path = tmp_path / "guard.py"
    script_path.write_text(script, encoding="utf-8")

    # Create repo with staged file
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)
    (repo / "file.txt").write_text("content", encoding="utf-8")
    _git_add(repo, "file.txt")

    # Run with WORKTREES_ENABLED=1
    result = _run_hook(script_path, repo, {"AGENT_NAME": "TestAgent", "WORKTREES_ENABLED": "1"})

    # Should run (no conflicts, so exit 0)
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_guard_gate_worktrees_enabled_false(isolated_env, tmp_path: Path):
    """Test guard exits early when WORKTREES_ENABLED=0."""
    settings = get_settings()
    archive = await ensure_archive(settings, "gate-test")

    # Add a conflicting reservation
    await write_file_reservation_record(
        archive,
        {
            "agent": "OtherAgent",
            "path_pattern": "*.txt",
            "exclusive": True,
        },
    )

    script = render_precommit_script(archive)
    script_path = tmp_path / "guard.py"
    script_path.write_text(script, encoding="utf-8")

    # Create repo with staged file that would conflict
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)
    (repo / "file.txt").write_text("content", encoding="utf-8")
    _git_add(repo, "file.txt")

    # Run with WORKTREES_ENABLED=0 (disabled)
    result = _run_hook(script_path, repo, {"AGENT_NAME": "TestAgent", "WORKTREES_ENABLED": "0"})

    # Should exit early with 0 (no conflict check)
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_guard_gate_git_identity_enabled(isolated_env, tmp_path: Path):
    """Test guard enforces conflicts when only GIT_IDENTITY_ENABLED=1 is set."""
    settings = get_settings()
    archive = await ensure_archive(settings, "gate-test")
    script = render_precommit_script(archive)
    script_path = tmp_path / "guard.py"
    script_path.write_text(script, encoding="utf-8")

    # Create repo with staged file
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)
    (repo / "file.txt").write_text("content", encoding="utf-8")
    _git_add(repo, "file.txt")
    await write_file_reservation_record(
        archive,
        {
            "agent": "OtherAgent",
            "path_pattern": "file.txt",
            "exclusive": True,
        },
    )

    # Run with GIT_IDENTITY_ENABLED=1 (alternative gate) and WORKTREES_ENABLED explicitly off.
    result = _run_hook(
        script_path,
        repo,
        {
            "AGENT_NAME": "TestAgent",
            "WORKTREES_ENABLED": "0",
            "GIT_IDENTITY_ENABLED": "1",
        },
    )

    assert result.returncode == 1
    assert "conflict" in result.stderr.lower()


@pytest.mark.asyncio
async def test_guard_gate_various_true_values(isolated_env, tmp_path: Path):
    """Test guard recognizes various truthy values for gate."""
    settings = get_settings()
    archive = await ensure_archive(settings, "gate-test")
    script = render_precommit_script(archive)
    script_path = tmp_path / "guard.py"
    script_path.write_text(script, encoding="utf-8")

    # Create repo
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)
    (repo / "file.txt").write_text("content", encoding="utf-8")
    _git_add(repo, "file.txt")

    # Test various truthy values
    for value in ["1", "true", "True", "TRUE", "yes", "Yes", "t", "T", "y", "Y"]:
        result = _run_hook(script_path, repo, {"AGENT_NAME": "TestAgent", "WORKTREES_ENABLED": value})
        # All should run (return 0 for no conflicts)
        assert result.returncode == 0, f"Gate value '{value}' should be truthy"


# =============================================================================
# Advisory Mode Tests
# =============================================================================


@pytest.mark.asyncio
async def test_guard_advisory_mode_warn(isolated_env, tmp_path: Path):
    """Test guard in advisory/warn mode doesn't block on conflicts."""
    settings = get_settings()
    archive = await ensure_archive(settings, "advisory-test")

    # Add conflicting reservation
    await write_file_reservation_record(
        archive,
        {
            "agent": "OtherAgent",
            "path_pattern": "*.py",
            "exclusive": True,
        },
    )

    script = render_precommit_script(archive)
    script_path = tmp_path / "guard.py"
    script_path.write_text(script, encoding="utf-8")

    # Create repo with conflicting file
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)
    (repo / "app.py").write_text("print('hello')", encoding="utf-8")
    _git_add(repo, "app.py")

    # Run in advisory mode
    result = _run_hook(
        script_path,
        repo,
        {
            "AGENT_NAME": "TestAgent",
            "WORKTREES_ENABLED": "1",
            "AGENT_MAIL_GUARD_MODE": "warn",
        },
    )

    # Should exit 0 in advisory mode (warn but don't block)
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_guard_bypass_flag(isolated_env, tmp_path: Path):
    """Test AGENT_MAIL_BYPASS=1 bypasses all checks."""
    settings = get_settings()
    archive = await ensure_archive(settings, "bypass-test")

    # Add conflicting reservation
    await write_file_reservation_record(
        archive,
        {
            "agent": "OtherAgent",
            "path_pattern": "*.py",
            "exclusive": True,
        },
    )

    script = render_precommit_script(archive)
    script_path = tmp_path / "guard.py"
    script_path.write_text(script, encoding="utf-8")

    # Create repo with conflicting file
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)
    (repo / "app.py").write_text("print('hello')", encoding="utf-8")
    _git_add(repo, "app.py")

    # Run with bypass enabled
    result = _run_hook(
        script_path,
        repo,
        {
            "AGENT_NAME": "TestAgent",
            "WORKTREES_ENABLED": "1",
            "AGENT_MAIL_BYPASS": "1",
        },
    )

    # Should bypass all checks
    assert result.returncode == 0
    assert "bypass" in result.stderr.lower()


# =============================================================================
# Pre-push Guard Tests
# =============================================================================


@pytest.mark.asyncio
async def test_prepush_guard_install(isolated_env, tmp_path: Path):
    """Test pre-push guard installation."""
    settings = get_settings()

    # Create repo
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    # Install pre-push guard
    hook_path = await install_prepush_guard(settings, "prepush-test", repo)

    assert hook_path.exists()
    assert "pre-push" in hook_path.name


@pytest.mark.asyncio
async def test_prepush_script_generation(isolated_env, tmp_path: Path):
    """Test pre-push script includes STDIN handling."""
    settings = get_settings()
    archive = await ensure_archive(settings, "prepush-test")

    script = render_prepush_script(archive)

    # Should have pre-push specific handling
    assert "pre-push" in script
    assert "stdin" in script.lower() or "STDIN" in script


# =============================================================================
# Uninstall Tests
# =============================================================================


@pytest.mark.asyncio
async def test_guard_uninstall(isolated_env, tmp_path: Path):
    """Test guard uninstall removes hooks properly."""
    settings = get_settings()

    # Create repo
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    # Install guard
    await install_guard(settings, "uninstall-test", repo)

    # Uninstall
    removed = await uninstall_guard(repo)

    assert removed is True


@pytest.mark.asyncio
async def test_guard_uninstall_nonexistent(isolated_env, tmp_path: Path):
    """Test uninstall on repo without guard returns False."""
    # Create repo without guard
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    # Uninstall (nothing to remove)
    removed = await uninstall_guard(repo)

    assert removed is False


# =============================================================================
# Chain Runner Tests
# =============================================================================


@pytest.mark.asyncio
async def test_chain_runner_executes_plugins(isolated_env, tmp_path: Path):
    """Test chain runner executes plugins in hooks.d directory."""
    settings = get_settings()

    # Create repo
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    # Install guard (creates chain runner)
    hook_path = await install_guard(settings, "chain-test", repo)

    # Create additional plugin in hooks.d
    hooks_d = hook_path.parent / "hooks.d" / "pre-commit"
    hooks_d.mkdir(parents=True, exist_ok=True)

    # Plugin that creates a marker file
    plugin = hooks_d / "99-test-plugin.py"
    marker_file = tmp_path / "plugin_ran.txt"
    plugin.write_text(
        f"#!/usr/bin/env python3\n"
        f"from pathlib import Path\n"
        f"Path('{marker_file}').write_text('ran')\n",
        encoding="utf-8",
    )
    plugin.chmod(0o755)

    # Stage a file
    (repo / "test.txt").write_text("test", encoding="utf-8")
    _git_add(repo, "test.txt")

    # Run chain runner
    _run_hook(hook_path, repo, {"AGENT_NAME": "TestAgent", "WORKTREES_ENABLED": "1"})

    # Plugin should have run
    assert marker_file.exists()
    assert marker_file.read_text() == "ran"


# =============================================================================
# Windows chain-runner dispatch (issue #262: preserved .orig must not be
# exec'd bare -- CreateProcess cannot honor shebangs -> WinError 193)
# =============================================================================


class _RecordingRun:
    """Stand-in for subprocess.run that records argv instead of spawning."""

    def __init__(self, git_exec_path: str = ""):
        self.calls: list[list[str]] = []
        self._git_exec_path = git_exec_path

    def __call__(self, argv, **kwargs):
        argv = [str(a) for a in argv]
        if argv[:2] == ["git", "--exec-path"]:
            return subprocess.CompletedProcess(argv, 0, stdout=self._git_exec_path, stderr="")
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)


def _exec_chain_runner(hook_path: Path, script_text: str, *, os_name: str | None = None) -> None:
    """Execute the rendered chain-runner in-process (it ends with sys.exit).

    ``os_name`` simulates another platform for the script only: its
    ``import os`` is served a shim whose ``name`` differs, while pathlib and
    everything else keep using the real ``os`` (patching ``os.name`` globally
    would make ``Path()`` instantiate WindowsPath and blow up on POSIX).
    """
    import builtins
    import types

    import pytest as _pytest

    exec_globals: dict = {"__file__": str(hook_path), "__name__": "__main__"}
    if os_name is not None:
        os_shim = types.SimpleNamespace(name=os_name)
        real_import = builtins.__import__

        def _import(name, *args, **kwargs):
            if name == "os":
                return os_shim
            return real_import(name, *args, **kwargs)

        exec_globals["__builtins__"] = {**vars(builtins), "__import__": _import}

    with _pytest.raises(SystemExit) as excinfo:
        exec(  # deliberately running the rendered hook under test
            compile(script_text, str(hook_path), "exec"),
            exec_globals,
        )
    assert excinfo.value.code in (0, None)


def _make_windows_hook_layout(tmp_path: Path) -> Path:
    """hooks dir with a .py plugin, a shebang shell plugin, and a sh .orig."""
    hooks = tmp_path / "hooks"
    run_dir = hooks / "hooks.d" / "pre-commit"
    run_dir.mkdir(parents=True)
    (run_dir / "10-plugin.py").write_text("print('py plugin')\n", encoding="utf-8")
    (run_dir / "20-plugin").write_text("#!/usr/bin/env python3\nprint('x')\n", encoding="utf-8")
    orig = hooks / "pre-commit.orig"
    orig.write_text("#!/usr/bin/env sh\necho original hook\n", encoding="utf-8")
    return hooks


def test_chain_runner_windows_dispatches_shebang_orig_via_sh(tmp_path: Path, monkeypatch):
    """On Windows a preserved shell-script .orig must run through sh, not bare.

    Regression test for issue #262: the runner special-cased only ``.py`` on
    Windows, so ``pre-commit.orig`` (suffix ``.orig``, ``#!/usr/bin/env sh``)
    was passed bare to CreateProcess and every commit failed with WinError 193.
    """
    import shutil
    import sys as _sys

    from mcp_agent_mail.guard import _render_chain_runner_script

    hooks = _make_windows_hook_layout(tmp_path)
    hook_path = hooks / "pre-commit"
    recorder = _RecordingRun()

    monkeypatch.setattr(subprocess, "run", recorder)
    monkeypatch.setattr(shutil, "which", lambda _cmd: "C:/Git/usr/bin/sh.exe")
    monkeypatch.setattr(_sys, "argv", [str(hook_path)])

    _exec_chain_runner(hook_path, _render_chain_runner_script("pre-commit"), os_name="nt")

    by_target = {call[-1].replace("\\", "/").rsplit("/", 1)[-1]: call for call in recorder.calls}
    # .py plugin dispatched via python
    assert by_target["10-plugin.py"][0] == "python"
    # extensionless plugin with a python shebang dispatched via python
    assert by_target["20-plugin"][0] == "python"
    # the preserved sh-shebang .orig dispatched via sh -- never invoked bare
    assert by_target["pre-commit.orig"][0] == "C:/Git/usr/bin/sh.exe"


def test_chain_runner_windows_resolves_bundled_git_sh(tmp_path: Path, monkeypatch):
    """With no sh on PATH, the runner finds git-for-windows' bundled sh.exe."""
    import shutil
    import sys as _sys

    from mcp_agent_mail.guard import _render_chain_runner_script

    hooks = _make_windows_hook_layout(tmp_path)
    hook_path = hooks / "pre-commit"

    # Fake git-for-windows install tree: <root>/mingw64/libexec/git-core
    # as --exec-path, sh.exe at <root>/usr/bin/sh.exe.
    git_root = tmp_path / "Git"
    exec_path = git_root / "mingw64" / "libexec" / "git-core"
    exec_path.mkdir(parents=True)
    bundled_sh = git_root / "usr" / "bin" / "sh.exe"
    bundled_sh.parent.mkdir(parents=True)
    bundled_sh.write_text("", encoding="utf-8")

    recorder = _RecordingRun(git_exec_path=str(exec_path))
    monkeypatch.setattr(subprocess, "run", recorder)
    monkeypatch.setattr(shutil, "which", lambda _cmd: None)
    monkeypatch.setattr(_sys, "argv", [str(hook_path)])

    _exec_chain_runner(hook_path, _render_chain_runner_script("pre-commit"), os_name="nt")

    orig_calls = [c for c in recorder.calls if c[-1].endswith("pre-commit.orig")]
    assert orig_calls, f"orig was never invoked: {recorder.calls}"
    assert orig_calls[0][0] == str(bundled_sh)


def test_chain_runner_posix_dispatch_unchanged(tmp_path: Path, monkeypatch):
    """On POSIX the runner still execs children bare (kernel honors shebangs)."""
    import sys as _sys

    from mcp_agent_mail.guard import _render_chain_runner_script

    hooks = _make_windows_hook_layout(tmp_path)
    hook_path = hooks / "pre-commit"
    orig = hooks / "pre-commit.orig"
    orig.chmod(0o755)
    for p in (hooks / "hooks.d" / "pre-commit").iterdir():
        p.chmod(0o755)

    recorder = _RecordingRun()
    monkeypatch.setattr(subprocess, "run", recorder)
    monkeypatch.setattr(_sys, "argv", [str(hook_path)])

    _exec_chain_runner(hook_path, _render_chain_runner_script("pre-commit"))

    orig_calls = [c for c in recorder.calls if c[-1].endswith("pre-commit.orig")]
    assert orig_calls == [[str(orig)]]


def test_prepush_chain_runner_shares_windows_dispatch():
    """pre-push renders the same runner, so it gets the same Windows fix."""
    from mcp_agent_mail.guard import _render_chain_runner_script

    script = _render_chain_runner_script("pre-push")
    assert "_read_shebang" in script
    assert "_win_sh" in script
    assert "'.exe', '.com', '.bat', '.cmd'" in script
