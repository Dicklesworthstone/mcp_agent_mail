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
import re
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

# Configuration directory
CONFIG_DIR = Path.home() / ".mcp-agent-mail"
CONFIG_FILE = CONFIG_DIR / "wizard-config.json"


def find_available_port(start: int = 9000, end: int = 9100) -> int:
    """Find an available port in the given range."""
    import socket
    for port in range(start, end):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    raise RuntimeError(f"No available ports in range {start}-{end}")


def parse_selection(choice: str, max_items: int) -> list[int]:
    """Parse selection string like '1,3,5' or '1-3,5' into list of indices."""
    if choice.strip().lower() == "all":
        return list(range(max_items))

    indices = []
    try:
        for part in choice.split(","):
            part = part.strip()
            if "-" in part:
                start_str, end_str = part.split("-", 1)
                start, end = int(start_str.strip()), int(end_str.strip())
                if start < 1 or end > max_items or start > end:
                    raise ValueError(f"Invalid range: {part}")
                indices.extend(range(start - 1, end))
            else:
                idx = int(part)
                if idx < 1 or idx > max_items:
                    raise ValueError(f"Invalid index: {idx}")
                indices.append(idx - 1)
        return sorted(set(indices))  # Remove duplicates and sort
    except ValueError as e:
        console.print(f"[red]Invalid selection:[/] {e}")
        return []


def save_config(config: dict[str, Any]) -> None:
    """Save wizard configuration for next run."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        import json
        with CONFIG_FILE.open("w") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        console.print(f"[yellow]Could not save config:[/] {e}")


def load_last_config() -> dict[str, Any] | None:
    """Load last wizard configuration if it exists."""
    if not CONFIG_FILE.exists():
        return None
    try:
        import json
        with CONFIG_FILE.open("r") as f:
            return json.load(f)
    except Exception:
        return None


def estimate_bundle_size(projects: list[str]) -> str:
    """Estimate bundle size based on project count (rough approximation)."""
    # Very rough estimate: 5-20MB per project depending on message count
    base_size = 2  # Static assets ~2MB
    project_size = len(projects) * 10  # ~10MB per project average
    total_mb = base_size + project_size

    if total_mb < 1:
        return "< 1 MB"
    elif total_mb < 1024:
        return f"~{total_mb} MB"
    else:
        return f"~{total_mb / 1024:.1f} GB"


def validate_github_repo_available(repo_name: str) -> tuple[bool, str]:
    """Check if GitHub repo name is available."""
    try:
        result = subprocess.run(
            ["gh", "repo", "view", repo_name],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return False, f"Repository '{repo_name}' already exists in your account"
        return True, ""
    except FileNotFoundError:
        return True, ""  # Can't check, assume available


def detect_existing_github_repo(repo_name: str) -> bool:
    """Check if GitHub repo already exists."""
    try:
        result = subprocess.run(
            ["gh", "repo", "view", repo_name],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def detect_package_manager() -> str | None:
    """Detect the available package manager on this system."""
    managers = {
        "brew": ["brew", "--version"],
        "apt": ["apt", "--version"],
        "dnf": ["dnf", "--version"],
        "npm": ["npm", "--version"],
    }
    for name, cmd in managers.items():
        try:
            subprocess.run(cmd, capture_output=True, check=True)
            return name
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    return None


def install_gh_cli() -> bool:
    """Offer to install gh CLI automatically."""
    console.print("\n[yellow]gh CLI is not installed.[/]")

    pkg_mgr = detect_package_manager()
    if not pkg_mgr or pkg_mgr == "npm":
        console.print("[cyan]Install gh CLI from:[/] https://cli.github.com/")
        return False

    # Only brew is simple enough to automate reliably
    if pkg_mgr == "brew":
        if Confirm.ask("Install gh CLI using Homebrew?", default=True):
            console.print("[cyan]Running:[/] brew install gh")
            try:
                subprocess.run(["brew", "install", "gh"], check=True)
                console.print("[green]✓ gh CLI installed successfully[/]")
                return True
            except subprocess.CalledProcessError:
                console.print("[red]Installation failed.[/]")
                return False
        return False

    # For apt/dnf, show manual instructions (requires adding repo first)
    if pkg_mgr == "apt":
        console.print("\n[cyan]To install gh CLI on Ubuntu/Debian:[/]")
        console.print("  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg")
        console.print('  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null')
        console.print("  sudo apt update")
        console.print("  sudo apt install gh -y")
    elif pkg_mgr == "dnf":
        console.print("\n[cyan]To install gh CLI on Fedora/RHEL:[/]")
        console.print("  sudo dnf install 'dnf-command(config-manager)'")
        console.print("  sudo dnf config-manager --add-repo https://cli.github.com/packages/rpm/gh-cli.repo")
        console.print("  sudo dnf install gh -y")
    else:
        console.print("[cyan]Install gh CLI from:[/] https://cli.github.com/")

    console.print("\n[yellow]Press Enter after installing to continue...[/]")
    input()

    # Check if it's now available
    try:
        subprocess.run(["gh", "--version"], capture_output=True, check=True)
        console.print("[green]✓ gh CLI detected[/]")
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        console.print("[red]gh CLI still not found. Continuing anyway...[/]")
        return False


def install_wrangler_cli() -> bool:
    """Offer to install wrangler CLI automatically."""
    console.print("\n[yellow]wrangler CLI is not installed.[/]")

    # Check if npm is available
    try:
        subprocess.run(["npm", "--version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        console.print("[red]npm is required to install wrangler.[/]")
        console.print("[cyan]Install Node.js from:[/] https://nodejs.org/")
        return False

    if Confirm.ask("Install wrangler CLI using npm?", default=True):
        console.print("[cyan]Running:[/] npm install -g wrangler")
        try:
            subprocess.run(["npm", "install", "-g", "wrangler"], check=True)
            console.print("[green]✓ wrangler CLI installed successfully[/]")
            return True
        except subprocess.CalledProcessError:
            console.print("[red]Installation failed. Try manually:[/] npm install -g wrangler")
            return False
    return False


def authenticate_gh_cli() -> bool:
    """Guide user through gh CLI authentication."""
    console.print("\n[bold cyan]GitHub CLI Authentication[/]")
    console.print("You need to authenticate with GitHub to create repositories and enable Pages.")
    console.print("\n[dim]The next command will open your browser to authenticate.[/]")

    if not Confirm.ask("Run 'gh auth login' now?", default=True):
        console.print("[yellow]Skipping authentication. You can run 'gh auth login' manually later.[/]")
        return False

    try:
        # Run gh auth login interactively (don't capture output)
        subprocess.run(["gh", "auth", "login"], check=True)
        console.print("[green]✓ GitHub authentication complete[/]")
        return True
    except subprocess.CalledProcessError:
        console.print("[red]Authentication failed.[/]")
        return False


def authenticate_wrangler_cli() -> bool:
    """Guide user through wrangler CLI authentication."""
    console.print("\n[bold cyan]Cloudflare Wrangler Authentication[/]")
    console.print("You need to authenticate with Cloudflare to deploy to Pages.")
    console.print("\n[dim]The next command will open your browser to authenticate.[/]")

    if not Confirm.ask("Run 'wrangler login' now?", default=True):
        console.print("[yellow]Skipping authentication. You can run 'wrangler login' manually later.[/]")
        return False

    try:
        # Run wrangler login interactively
        subprocess.run(["wrangler", "login"], check=True)
        console.print("[green]✓ Cloudflare authentication complete[/]")
        return True
    except subprocess.CalledProcessError:
        console.print("[red]Authentication failed.[/]")
        return False


def check_prerequisites(require_github: bool = False, require_cloudflare: bool = False) -> bool:
    """Check if required tools are installed and configured."""
    all_satisfied = True

    # Check gh CLI (only if GitHub deployment selected)
    if require_github:
        try:
            result = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                console.print("[green]✓ gh CLI installed and authenticated[/]")
            else:
                console.print("[yellow]⚠ gh CLI installed but not authenticated[/]")
                if not authenticate_gh_cli():
                    all_satisfied = False
        except FileNotFoundError:
            if install_gh_cli():
                # After install, try to authenticate
                if not authenticate_gh_cli():
                    all_satisfied = False
            else:
                all_satisfied = False

    # Check wrangler CLI (only if Cloudflare deployment selected)
    if require_cloudflare:
        try:
            result = subprocess.run(
                ["wrangler", "whoami"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                console.print("[green]✓ wrangler CLI installed and authenticated[/]")
            else:
                console.print("[yellow]⚠ wrangler CLI installed but not authenticated[/]")
                if not authenticate_wrangler_cli():
                    all_satisfied = False
        except FileNotFoundError:
            if install_wrangler_cli():
                if not authenticate_wrangler_cli():
                    all_satisfied = False
            else:
                all_satisfied = False

    # Check git config (only needed for GitHub deployment)
    if require_github:
        try:
            subprocess.run(["git", "config", "user.name"], capture_output=True, check=True, text=True)
            subprocess.run(["git", "config", "user.email"], capture_output=True, check=True, text=True)
            console.print("[green]✓ git configured[/]")
        except (FileNotFoundError, subprocess.CalledProcessError):
            console.print("[red]❌ git not configured[/]")
            console.print("[cyan]Run:[/] git config --global user.name \"Your Name\"")
            console.print("[cyan]Run:[/] git config --global user.email \"you@example.com\"")
            all_satisfied = False

    if not all_satisfied:
        console.print("\n[yellow]Some prerequisites are missing. Please address them and try again.[/]")

    return all_satisfied


def get_projects() -> list[dict[str, str]]:
    """Get list of projects from the database."""
    try:
        result = subprocess.run(
            ["uv", "run", "python", "-m", "mcp_agent_mail.cli", "list-projects", "--json"],
            capture_output=True,
            text=True,
            check=True,
        )
        # Parse JSON output
        import json
        projects_data = json.loads(result.stdout)
        return [{"slug": p["slug"], "human_key": p["human_key"]} for p in projects_data]
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, TypeError):
        return []


def select_projects(projects: list[dict[str, str]]) -> list[str]:
    """Interactive project selection with support for ranges and lists."""
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

    while True:
        choice = Prompt.ask(
            "\n[bold]Select projects to export[/] (e.g., 'all', '1,3,5', or '1-3')",
            default="all",
        )

        indices = parse_selection(choice, len(projects))
        if indices:  # Valid selection
            return [projects[idx]["human_key"] for idx in indices]
        # If empty, parse_selection already printed error, loop continues


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
    console.print("  1. GitHub Pages (create new repository)")
    console.print("  2. Cloudflare Pages (fast global CDN)")
    console.print("  3. Export locally only")

    choice = Prompt.ask("Choose option", choices=["1", "2", "3"], default="1")

    if choice == "3":
        output_dir = Prompt.ask("Output directory", default="./mailbox-export")
        return {"type": "local", "path": output_dir}

    if choice == "2":
        # Cloudflare Pages deployment
        project_name = Prompt.ask("Cloudflare Pages project name", default="mailbox-viewer")
        return {
            "type": "cloudflare-pages",
            "project_name": project_name,
        }

    # GitHub Pages deployment - create new repo
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
    }


def generate_signing_key() -> Path:
    """Generate Ed25519 signing key in current directory."""
    # Save to current directory (not /tmp) so it persists
    key_path = Path.cwd() / f"signing-{secrets.token_hex(4)}.key"
    key_path.write_bytes(secrets.token_bytes(32))
    # Set secure permissions (best-effort on Windows where this may not apply)
    try:
        key_path.chmod(0o600)
    except (OSError, NotImplementedError):
        pass  # Windows or other platform without Unix permissions
    console.print(f"[yellow]⚠ Private signing key saved to:[/] {key_path}")
    console.print("[yellow]⚠ Back up this file securely - you'll need it to update the bundle[/]")
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
    import socket

    console.print("\n[bold cyan]Launching preview server...[/]")

    # Find available port
    try:
        port = find_available_port()
        console.print(f"[dim]Using port {port} (Ctrl+C to stop server)[/]")
    except RuntimeError as e:
        console.print(f"[red]{e}[/]")
        return False

    process = None
    try:
        # Start preview server in background
        process = subprocess.Popen(
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
                str(port),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Wait for server to be ready by polling the port
        console.print("[cyan]Waiting for server to start...[/]")
        max_attempts = 30
        for attempt in range(max_attempts):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except (ConnectionRefusedError, OSError):
                if process.poll() is not None:
                    console.print("[red]Preview server failed to start[/]")
                    return False
                time.sleep(0.5)
        else:
            console.print("[red]Preview server did not start in time[/]")
            process.terminate()
            return False

        # Server is ready, open browser
        console.print(f"[green]✓ Server ready, opening browser at http://127.0.0.1:{port}[/]")
        webbrowser.open(f"http://127.0.0.1:{port}")

        # Wait for server process to complete (user will Ctrl+C)
        process.wait()

        # After server stops, ask if satisfied
        console.print("\n[bold]Preview complete.[/]")
        return Confirm.ask("Are you satisfied with the preview?", default=True)

    except KeyboardInterrupt:
        console.print("\n[yellow]Preview interrupted[/]")
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                console.print("[yellow]Warning: Preview server did not stop cleanly[/]")
                process.kill()
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
        # Use cwd parameter instead of os.chdir() to avoid side effects
        subprocess.run(["git", "init"], cwd=output_dir, check=True, capture_output=True, text=True)
        subprocess.run(["git", "add", "."], cwd=output_dir, check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial mailbox export"],
            cwd=output_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(["git", "branch", "-M", branch], cwd=output_dir, check=True, capture_output=True, text=True)

        # Add remote and push
        repo_url = f"git@github.com:{repo_full_name}.git"
        subprocess.run(
            ["git", "remote", "add", "origin", repo_url],
            cwd=output_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "push", "-u", "origin", branch],
            cwd=output_dir,
            check=True,
            capture_output=True,
            text=True,
        )

        console.print(f"[green]✓ Pushed to {repo_full_name}[/]")
        return True

    except subprocess.CalledProcessError as e:
        console.print(f"[red]Git operation failed:[/]\n{e.stderr}")
        return False


def enable_github_pages(repo_full_name: str, branch: str = "main") -> tuple[bool, str]:
    """Enable GitHub Pages for the repository (root directory)."""
    try:
        # Enable Pages via gh API (always use root "/" for our use case)
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
                "source[path]=/",
            ],
            check=True,
            capture_output=True,
            text=True,
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


def deploy_to_cloudflare_pages(output_dir: Path, project_name: str) -> tuple[bool, str]:
    """Deploy bundle to Cloudflare Pages using wrangler."""
    console.print(f"\n[bold cyan]Deploying to Cloudflare Pages...[/]")

    try:
        # Use wrangler pages deploy command
        result = subprocess.run(
            [
                "wrangler",
                "pages",
                "deploy",
                str(output_dir),
                "--project-name", project_name,
                "--branch", "main",
            ],
            capture_output=True,
            text=True,
            check=True,
        )

        # Parse output for deployment URL
        # Wrangler outputs URLs in format: https://xxx.pages.dev
        pages_url = ""
        for line in result.stdout.split("\n"):
            if ".pages.dev" in line:
                # Extract URL from the line
                url_match = re.search(r"https://[^\s]+\.pages\.dev[^\s]*", line)
                if url_match:
                    pages_url = url_match.group(0)
                    break

        if not pages_url:
            # Fallback: construct expected URL
            pages_url = f"https://{project_name}.pages.dev"

        console.print(f"[green]✓ Deployed to Cloudflare Pages[/]")
        return True, pages_url

    except subprocess.CalledProcessError as e:
        console.print(f"[red]Cloudflare Pages deployment failed:[/]\n{e.stderr}")
        return False, ""


def main() -> None:
    """Main wizard flow."""
    console.print(
        Panel.fit(
            "[bold cyan]MCP Agent Mail → Deployment Wizard[/]\n\n"
            "This wizard will:\n"
            "  1. Export your mailbox to a static HTML bundle\n"
            "  2. Preview it locally\n"
            "  3. Deploy to GitHub Pages or Cloudflare Pages\n\n"
            "[dim]Press Ctrl+C anytime to cancel[/]",
            title="Welcome",
        )
    )

    # Check if we have a previous configuration
    last_config = load_last_config()
    use_last_config = False

    if last_config:
        console.print("\n[bold cyan]Previous Configuration Found[/]")
        console.print(f"  Projects: {last_config.get('project_count', '?')} selected")
        console.print(f"  Redaction: {last_config.get('scrub_preset', 'standard')}")
        console.print(f"  Target: {last_config.get('deployment_type', 'unknown')}")

        use_last_config = Confirm.ask(
            "\nUse these settings again?",
            default=True,
        )

    # If not using last config, go through interactive setup
    if not use_last_config:
        # Get deployment target first to know which CLIs we need
        deployment = select_deployment_target()
    else:
        # Reconstruct deployment config from saved settings
        deployment = last_config.get("deployment", {})

    # Check prerequisites based on deployment choice
    require_gh = deployment["type"] == "github-new"
    require_cf = deployment["type"] == "cloudflare-pages"
    if not check_prerequisites(require_github=require_gh, require_cloudflare=require_cf):
        sys.exit(1)

    # Get projects
    projects_list = get_projects()
    if not projects_list:
        console.print("[yellow]No projects found. Create some messages first![/]")
        sys.exit(1)

    console.print(f"\n[green]Found {len(projects_list)} project(s)[/]")

    # Interactive selections (or use saved config)
    if not use_last_config:
        selected_projects = select_projects(projects_list)
        scrub_preset = select_scrub_preset()
        # Record selected project indices for saving config later
        selected_indices = [i for i, p in enumerate(projects_list) if p["human_key"] in selected_projects]
    else:
        # Use saved project indices
        saved_indices = last_config.get("project_indices", list(range(len(projects_list))))
        # Validate indices are still valid
        selected_indices = [i for i in saved_indices if i < len(projects_list)]
        if not selected_indices:
            console.print("[yellow]Saved project selection invalid, please select again[/]")
            selected_projects = select_projects(projects_list)
            selected_indices = [i for i, p in enumerate(projects_list) if p["human_key"] in selected_projects]
        else:
            selected_projects = [projects_list[idx]["human_key"] for idx in selected_indices]
            console.print(f"[green]Using saved selection: {len(selected_projects)} project(s)[/]")

        scrub_preset = last_config.get("scrub_preset", "standard")

    # Signing key (use saved preference if available)
    signing_key = None
    if not use_last_config:
        use_signing = Confirm.ask("\nSign the bundle with Ed25519?", default=True)
        generate_new_key = False
        if use_signing:
            generate_new_key = Confirm.ask("Generate a new signing key?", default=True)
            if generate_new_key:
                signing_key = generate_signing_key()
            else:
                key_path = Prompt.ask("Path to existing signing key")
                signing_key = Path(key_path)
    else:
        use_signing = last_config.get("use_signing", True)
        generate_new_key = last_config.get("generate_new_key", True)
        if use_signing:
            if generate_new_key:
                signing_key = generate_signing_key()
            else:
                # Ask for key path again (don't save sensitive paths)
                key_path = Prompt.ask("Path to existing signing key")
                signing_key = Path(key_path)

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

            # Save config for next run
            save_config({
                "project_indices": selected_indices,
                "project_count": len(selected_projects),
                "scrub_preset": scrub_preset,
                "deployment": deployment,
                "deployment_type": "local",
                "use_signing": use_signing,
                "generate_new_key": generate_new_key,
            })

        elif deployment["type"] == "github-new":
            # Create repo
            success, repo_full_name = create_github_repo(
                deployment["repo_name"],
                deployment["private"],
                deployment["description"],
            )
            if not success:
                sys.exit(1)

            # Init and push (use temp_path directly, no need to copy)
            if not init_and_push_repo(temp_path, repo_full_name):
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

                # Save config for next run
                save_config({
                    "project_indices": selected_indices,
                    "project_count": len(selected_projects),
                    "scrub_preset": scrub_preset,
                    "deployment": deployment,
                    "deployment_type": "github-new",
                    "use_signing": use_signing,
                    "generate_new_key": generate_new_key,
                })
            else:
                console.print(f"\n[yellow]Repository created but Pages setup failed[/]")
                console.print(f"Visit https://github.com/{repo_full_name}/settings/pages to enable manually")

        elif deployment["type"] == "cloudflare-pages":
            # Deploy to Cloudflare Pages
            success, pages_url = deploy_to_cloudflare_pages(temp_path, deployment["project_name"])
            if success:
                console.print(
                    Panel.fit(
                        f"[bold green]Deployment Complete![/]\n\n"
                        f"Cloudflare Pages: {pages_url}\n\n"
                        f"[dim]Note: Your site should be live immediately[/]",
                        title="Success",
                        border_style="green",
                    )
                )

                if signing_pub:
                    console.print(f"\n[cyan]Signing public key saved to:[/] {signing_pub}")
                    console.print("[dim]Share this with viewers to verify bundle authenticity[/]")

                # Save config for next run
                save_config({
                    "project_indices": selected_indices,
                    "project_count": len(selected_projects),
                    "scrub_preset": scrub_preset,
                    "deployment": deployment,
                    "deployment_type": "cloudflare-pages",
                    "use_signing": use_signing,
                    "generate_new_key": generate_new_key,
                })
            else:
                console.print(f"\n[yellow]Cloudflare Pages deployment failed[/]")
                sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled by user[/]")
        sys.exit(0)
    except Exception as e:
        console.print(f"\n[bold red]Error:[/] {e}")
        sys.exit(1)
