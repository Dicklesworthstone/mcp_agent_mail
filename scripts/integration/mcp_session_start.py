import sys
import os
import subprocess
import socket
import signal

# Capture real args BEFORE any imports
real_args = sys.argv[1:]
sys.argv = [sys.argv[0]] # Reset for Typer safety

try:
    from mcp_agent_mail.cli import file_reservations_active, acks_pending
except ImportError as e:
    print(f"Error importing mcp_agent_mail: {e}", file=sys.stderr)
    sys.exit(1)

def is_port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) == 0

def launch_ui_server():
    if is_port_open(8765): return
    try:
        # Assumes scripts/mcp_ui.sh exists or you use a direct command
        subprocess.Popen(
            ["uv", "run", "--dev", "python", "-m", "mcp_agent_mail.cli", "serve-http", "--port", "8765"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
        )
    except Exception:
        pass

def timeout_handler(signum, frame):
    print("Timeout checking Agent Mail status.", file=sys.stderr)
    os._exit(1)

if __name__ == "__main__":
    # Set global timeout of 3 seconds
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(3)

    if len(real_args) < 1:
        print("Usage: python mcp_session_start.py <project_path> [agent_name]", file=sys.stderr)
        sys.exit(1)

    # Launch UI server if needed (non-blocking)
    launch_ui_server()

    project = real_args[0]

    # Identity Management: Session-based via Environment
    # We prefer AGENT_NAME provided by user shell.
    # If not provided, we auto-register a new one and export it to the session environment via CLAUDE_ENV_FILE.

    agent = os.environ.get("AGENT_NAME") or os.environ.get("MCP_AGENT_NAME")

    if not agent or agent == "unknown":
        # Auto-register logic
        try:
            # We need full app context for registration (slower but one-time)
            from mcp_agent_mail.config import get_settings
            from mcp_agent_mail.db import ensure_schema, get_session
            from mcp_agent_mail.app import _get_or_create_agent
            from mcp_agent_mail.models import Project
            from sqlalchemy import select

            import asyncio
            async def register_auto():
                await ensure_schema()
                settings = get_settings()
                async with get_session() as session:
                    # Resolve project first
                    stmt = select(Project).where(Project.human_key == project)
                    p = (await session.execute(stmt)).scalars().first()
                    if not p:
                        from mcp_agent_mail.storage import ProjectArchive
                        archive = await ProjectArchive.get_or_create(human_key=project)
                        p = archive.project_record

                    # Register with None name to auto-generate
                    new_agent = await _get_or_create_agent(p, None, "claude-code", "auto", "Auto-registered by SessionStart", settings)
                    print(f"🎉 Auto-registered new agent: {new_agent.name}", file=sys.stderr)
                    # Update env var recommendation (can't set parent env, but can inform user)
                    print(f"ℹ️  To keep this identity, run: export AGENT_NAME={new_agent.name}", file=sys.stderr)
                    return new_agent.name

            agent = asyncio.run(register_auto())

            # Export to current session context via CLAUDE_ENV_FILE
            env_file = os.environ.get("CLAUDE_ENV_FILE")
            if env_file:
                with open(env_file, "a") as f:
                    f.write(f"\nexport AGENT_NAME={agent}\n")
                    f.write(f"export MCP_AGENT_NAME={agent}\n")

            # Update args for current run
            real_args[1] = agent
        except Exception as e:
            print(f"Failed to auto-register agent: {e}", file=sys.stderr)

    # Run File Reservations Check
    try:
        # project arg is required
        file_reservations_active(project=project, limit=20)
    except Exception as e:
        print(f"Error checking file reservations: {e}", file=sys.stderr)

    print("") # Spacer

    # Run Acks Check
    try:
        acks_pending(project=project, agent=agent, limit=20)
    except Exception as e:
        print(f"Error checking acks: {e}", file=sys.stderr)

    # Force exit to avoid waiting for slow cleanup (e.g. DB pools, threads)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
