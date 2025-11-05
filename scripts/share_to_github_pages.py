#!/usr/bin/env python3
"""Interactive wizard for exporting mailboxes and deploying to GitHub Pages.

This script automates the entire workflow:
1. Select projects and export options
2. Preview the bundle locally
3. Create/update GitHub repository
4. Enable GitHub Pages
5. Push and deploy

Requirements:
- gh CLI installed and authenticated (gh auth status)
- git configured with user.name and user.email
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import webbrowser
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn

console = Console()


def check_prerequisites() -> bool:
    """Check if required tools are installed and configured."""
    issues = []

    # Check gh CLI
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            issues.append("❌ gh CLI not authenticated. Run: gh auth login")
    except FileNotFoundError:
        issues.append("❌ gh CLI not installed. Install from: https://cli.github.com/")

    # Check git config
    try:
        subprocess.run(["git", "config", "user.name"], capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        issues.append("❌ git not configured. Run: git config --global user.name/user.email")

    if issues:
        console.print("[bold red]Prerequisites missing:[/]")
        for issue in issues:
            console.print(f"  {issue}")
        return False

    console.print("[green]✓ All prerequisites satisfied[/]")
    return True


def get_projects() -> list[dict[str, str]]:
    """Get list of projects from the database."""
    try:
        result = subprocess.run(
            ["uv", "run", "python", "-m", "mcp_agent_mail.cli", "list-projects"],
            capture_output=True,
            text=True,
            check=True,
        )
        # Parse output (format: "slug | human_key | created")
        projects = []
        for line in result.stdout.strip().split("\n"):
            if "|" in line and "slug" not in line.lower():  # Skip header
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 2:
                    projects.append({"slug": parts[0], "human_key": parts[1]})
        return projects
    except subprocess.CalledProcessError:
        return []


def select_projects(projects: list[dict[str, str]]) -> list[str]:
    """Interactive project selection."""
    if not projects:
        console.print("[yellow]No projects found. Create some messages first![/]")
        sys.exit(1)

    console.print("\n[bold]Available Projects:[/]")
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("#", style="dim")
    table.add_column("Slug")
    table.add_column("Path")

    for idx, proj in enumerate(projects, 1):
        table.add_row(str(idx), proj["slug"], proj["human_key"])

    console.print(table)

    choice = Prompt.ask(
        "\n[bold]Select projects to export[/]",
        choices=["all"] + [str(i) for i in range(1, len(projects) + 1)],
        default="all",
    )

    if choice == "all":
        return [p["human_key"] for p in projects]
    else:
        idx = int(choice) - 1
        return [projects[idx]["human_key"]]


def select_scrub_preset() -> str:
    """Select redaction preset."""
    console.print("\n[bold]Redaction Preset:[/]")
    console.print("  [cyan]standard[/]: Pseudonymize agents, scrub secrets, keep message bodies")
    console.print("  [cyan]strict[/]: Replace all message bodies with placeholders, remove attachments")

    return Prompt.ask(
        "Choose preset",
        choices=["standard", "strict"],
        default="standard",
    )


def select_deployment_target() -> dict[str, Any]:
    """Select where to deploy."""
    console.print("\n[bold]Deployment Target:[/]")
    console.print("  1. New GitHub repository (we'll create it)")
    console.print("  2. Existing repo - docs/ subdirectory")
    console.print("  3. Existing repo - root directory")
    console.print("  4. Export locally only (no GitHub)")

    choice = Prompt.ask("Choose option", choices=["1", "2", "3", "4"], default="1")

    if choice == "4":
        output_dir = Prompt.ask("Output directory", default="./mailbox-export")
        return {"type": "local", "path": output_dir}

    # GitHub deployment
    if choice == "1":
        repo_name = Prompt.ask("Repository name", default="mailbox-viewer")
        is_private = Confirm.ask("Make repository private?", default=False)
        description = Prompt.ask(
            "Repository description",
            default="MCP Agent Mail static viewer",
        )
        return {
            "type": "github-new",
            "repo_name": repo_name,
            "private": is_private,
            "description": description,
            "path": "root",
        }
    elif choice == "2":
        repo = Prompt.ask("Repository (owner/name)", default="")
        return {"type": "github-existing", "repo": repo, "path": "docs/mailbox"}
    else:  # choice == "3"
        repo = Prompt.ask("Repository (owner/name)", default="")
        return {"type": "github-existing", "repo": repo, "path": "root"}


def generate_signing_key() -> Path:
    """Generate Ed25519 signing key."""
    key_path = Path(tempfile.gettempdir()) / f"signing-{secrets.token_hex(4)}.key"
    key_path.write_bytes(secrets.token_bytes(32))
    key_path.chmod(0o600)
    return key_path


def export_bundle(
    output_dir: Path,
    projects: list[str],
    scrub_preset: str,
    signing_key: Path | None = None,
) -> tuple[bool, Path | None]:
    """Export mailbox bundle."""
    cmd = [
        "uv",
        "run",
        "python",
        "-m",
        "mcp_agent_mail.cli",
        "share",
        "export",
        "--output",
        str(output_dir),
        "--no-zip",
    ]

    if scrub_preset != "none":
        cmd.extend(["--scrub-preset", scrub_preset])

    for project in projects:
        cmd.extend(["--project", project])

    signing_pub_path = None
    if signing_key:
        signing_pub_path = signing_key.with_suffix(".pub")
        cmd.extend([
            "--signing-key",
            str(signing_key),
            "--signing-public-out",
            str(signing_pub_path),
        ])

    console.print("\n[bold]Exporting mailbox bundle...[/]")
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Exporting...", total=None)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            progress.update(task, completed=True)
            console.print("[green]✓ Export complete[/]")
            return True, signing_pub_path
        except subprocess.CalledProcessError as e:
            progress.update(task, completed=True)
            console.print(f"[red]Export failed:[/]\n{e.stderr}")
            return False, None


def preview_bundle(output_dir: Path) -> bool:
    """Launch preview server and ask user to confirm."""
    console.print("\n[bold cyan]Launching preview server...[/]")
    console.print("[dim]Press Ctrl+C in the preview window to stop the server[/]")

    try:
        # Open browser first
        time.sleep(1)
        webbrowser.open("http://127.0.0.1:9000")

        # Start preview server (blocking)
        subprocess.run(
            [
                "uv",
                "run",
                "python",
                "-m",
                "mcp_agent_mail.cli",
                "share",
                "preview",
                str(output_dir),
                "--port",
                "9000",
            ],
            check=False,
        )

        # After server stops, ask if satisfied
        console.print("\n[bold]Preview complete.[/]")
        return Confirm.ask("Are you satisfied with the preview?", default=True)

    except KeyboardInterrupt:
        console.print("\n[yellow]Preview interrupted[/]")
        return Confirm.ask("Continue with deployment anyway?", default=False)


def create_github_repo(name: str, private: bool, description: str) -> tuple[bool, str]:
    """Create new GitHub repository and return owner/name."""
    visibility = "--private" if private else "--public"

    try:
        result = subprocess.run(
            [
                "gh",
                "repo",
                "create",
                name,
                visibility,
                "--description",
                description,
                "--clone=false",
            ],
            capture_output=True,
            text=True,
            check=True,
        )

        # Get the full repo name (owner/repo)
        result = subprocess.run(
            ["gh", "repo", "view", name, "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            capture_output=True,
            text=True,
            check=True,
        )
        full_name = result.stdout.strip()
        console.print(f"[green]✓ Created repository: {full_name}[/]")
        return True, full_name

    except subprocess.CalledProcessError as e:
        console.print(f"[red]Failed to create repository:[/]\n{e.stderr}")
        return False, ""


def init_and_push_repo(output_dir: Path, repo_full_name: str, branch: str = "main") -> bool:
    """Initialize git repo and push to GitHub."""
    try:
        os.chdir(output_dir)

        # Init and commit
        subprocess.run(["git", "init"], check=True, capture_output=True)
        subprocess.run(["git", "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial mailbox export"],
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "branch", "-M", branch], check=True, capture_output=True)

        # Add remote and push
        repo_url = f"git@github.com:{repo_full_name}.git"
        subprocess.run(
            ["git", "remote", "add", "origin", repo_url],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push", "-u", "origin", branch],
            check=True,
            capture_output=True,
        )

        console.print(f"[green]✓ Pushed to {repo_full_name}[/]")
        return True

    except subprocess.CalledProcessError as e:
        console.print(f"[red]Git operation failed:[/]\n{e.stderr}")
        return False


def enable_github_pages(repo_full_name: str, branch: str = "main", path: str = "/") -> tuple[bool, str]:
    """Enable GitHub Pages for the repository."""
    try:
        # Enable Pages via gh API
        # The path should be "/" for root or "/docs" for docs directory
        gh_path = "/docs" if path.startswith("docs") else "/"

        subprocess.run(
            [
                "gh",
                "api",
                f"repos/{repo_full_name}/pages",
                "-X",
                "POST",
                "-f",
                f"source[branch]={branch}",
                "-f",
                f"source[path]={gh_path}",
            ],
            check=True,
            capture_output=True,
        )

        # Get the Pages URL
        time.sleep(2)  # Wait for Pages to be configured
        result = subprocess.run(
            ["gh", "api", f"repos/{repo_full_name}/pages", "-q", ".html_url"],
            capture_output=True,
            text=True,
            check=True,
        )
        pages_url = result.stdout.strip()

        console.print(f"[green]✓ GitHub Pages enabled[/]")
        return True, pages_url

    except subprocess.CalledProcessError as e:
        # Pages might already be enabled
        if "already exists" in e.stderr or "409" in e.stderr:
            console.print("[yellow]GitHub Pages already enabled[/]")
            # Try to get the URL anyway
            try:
                result = subprocess.run(
                    ["gh", "api", f"repos/{repo_full_name}/pages", "-q", ".html_url"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                pages_url = result.stdout.strip()
                return True, pages_url
            except subprocess.CalledProcessError:
                # Construct URL manually
                owner, name = repo_full_name.split("/")
                pages_url = f"https://{owner}.github.io/{name}/"
                return True, pages_url
        else:
            console.print(f"[red]Failed to enable GitHub Pages:[/]\n{e.stderr}")
            return False, ""


def main() -> None:
    """Main wizard flow."""
    console.print(
        Panel.fit(
            "[bold cyan]MCP Agent Mail → GitHub Pages[/]\n\n"
            "This wizard will:\n"
            "  1. Export your mailbox to a static HTML bundle\n"
            "  2. Preview it locally\n"
            "  3. Deploy to GitHub Pages\n\n"
            "[dim]Press Ctrl+C anytime to cancel[/]",
            title="Welcome",
        )
    )

    # Check prerequisites
    if not check_prerequisites():
        sys.exit(1)

    # Get projects
    projects_list = get_projects()
    if not projects_list:
        console.print("[yellow]No projects found. Create some messages first![/]")
        sys.exit(1)

    console.print(f"\n[green]Found {len(projects_list)} project(s)[/]")

    # Interactive selections
    selected_projects = select_projects(projects_list)
    scrub_preset = select_scrub_preset()

    # Signing key
    signing_key = None
    if Confirm.ask("\nSign the bundle with Ed25519?", default=True):
        if Confirm.ask("Generate a new signing key?", default=True):
            signing_key = generate_signing_key()
            console.print(f"[green]✓ Generated signing key: {signing_key}[/]")
        else:
            key_path = Prompt.ask("Path to existing signing key")
            signing_key = Path(key_path)

    deployment = select_deployment_target()

    # Export to temp directory first for preview
    with tempfile.TemporaryDirectory(prefix="mailbox-preview-") as temp_dir:
        temp_path = Path(temp_dir)

        success, signing_pub = export_bundle(temp_path, selected_projects, scrub_preset, signing_key)
        if not success:
            sys.exit(1)

        # Preview
        if not Confirm.ask("\nPreview the bundle before deploying?", default=True):
            satisfied = True
        else:
            satisfied = preview_bundle(temp_path)

        if not satisfied:
            console.print("[yellow]Deployment cancelled[/]")
            sys.exit(0)

        # Deploy based on target
        if deployment["type"] == "local":
            output_path = Path(deployment["path"]).expanduser().resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(temp_path, output_path, dirs_exist_ok=True)
            console.print(f"\n[bold green]✓ Exported to: {output_path}[/]")

            if signing_pub:
                console.print(f"[green]✓ Signing public key: {signing_pub}[/]")

        elif deployment["type"] == "github-new":
            # Create repo
            success, repo_full_name = create_github_repo(
                deployment["repo_name"],
                deployment["private"],
                deployment["description"],
            )
            if not success:
                sys.exit(1)

            # Copy to final location
            final_path = Path(temp_dir) / "final"
            shutil.copytree(temp_path, final_path)

            # Init and push
            if not init_and_push_repo(final_path, repo_full_name):
                sys.exit(1)

            # Enable Pages
            success, pages_url = enable_github_pages(repo_full_name)
            if success:
                console.print(
                    Panel.fit(
                        f"[bold green]Deployment Complete![/]\n\n"
                        f"Repository: https://github.com/{repo_full_name}\n"
                        f"GitHub Pages: {pages_url}\n\n"
                        f"[dim]Note: Pages may take 1-2 minutes to become available[/]",
                        title="Success",
                        border_style="green",
                    )
                )

                if signing_pub:
                    console.print(f"\n[cyan]Signing public key saved to:[/] {signing_pub}")
                    console.print("[dim]Share this with viewers to verify bundle authenticity[/]")
            else:
                console.print(f"\n[yellow]Repository created but Pages setup failed[/]")
                console.print(f"Visit https://github.com/{repo_full_name}/settings/pages to enable manually")

        else:  # github-existing
            console.print("\n[yellow]Existing repo deployment not yet implemented[/]")
            console.print("Use 'local' export and push to your repo manually for now")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled by user[/]")
        sys.exit(0)
    except Exception as e:
        console.print(f"\n[bold red]Error:[/] {e}")
        sys.exit(1)
