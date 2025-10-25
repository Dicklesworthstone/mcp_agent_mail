"""Rich-based comprehensive logging for MCP tool calls.

This module provides beautiful, detailed console logging using the Rich library
with panels, syntax highlighting, tables, and more to give full visibility into
agent tool calls and system operations.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from rich import box
from rich.console import Console, Group
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

# Global console instance for logging
console = Console(stderr=True, force_terminal=True, width=120)


@dataclass
class ToolCallContext:
    """Context information for a tool call."""

    tool_name: str
    args: list[Any]  # Positional arguments as a list
    kwargs: dict[str, Any]  # Keyword arguments as a dict
    project: Optional[str] = None
    agent: Optional[str] = None
    start_time: float = field(default_factory=time.perf_counter)
    end_time: Optional[float] = None
    result: Any = None
    error: Optional[Exception] = None
    success: bool = True
    _created_at: datetime = field(default_factory=datetime.now)  # Capture at creation time
    rendered_panel: Optional[str] = None

    @property
    def duration_ms(self) -> float:
        """Get duration in milliseconds."""
        end = self.end_time if self.end_time else time.perf_counter()
        return (end - self.start_time) * 1000

    @property
    def timestamp(self) -> str:
        """Get formatted timestamp (captured at creation)."""
        return self._created_at.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _safe_json_format(data: Any, max_length: int = 2000) -> str:
    """Format data as JSON with truncation."""
    json_str = json.dumps(data, indent=2, default=str, ensure_ascii=False)
    if len(json_str) > max_length:
        json_str = json_str[:max_length] + "\n... (truncated)"
    return json_str


def _create_syntax_panel(title: str, content: str, language: str = "json") -> Panel:
    """Create a Rich Panel with syntax-highlighted content."""
    syntax = Syntax(content, language, theme="monokai", line_numbers=False, word_wrap=True)
    return Panel(
        syntax,
        title=f"[bold cyan]{title}[/bold cyan]",
        border_style="cyan",
        box=box.ROUNDED,
        padding=(0, 1),
    )


def _create_info_table(ctx: ToolCallContext) -> Table:
    """Create a table with tool call metadata."""
    table = Table(
        show_header=False,
        box=box.SIMPLE,
        padding=(0, 1),
        border_style="blue",
    )
    table.add_column("Key", style="bold yellow", width=20)
    table.add_column("Value", style="white")

    # Add rows
    table.add_row("Tool Name", f"[bold green]{ctx.tool_name}[/bold green]")  # We control tool names
    table.add_row("Timestamp", ctx.timestamp)

    if ctx.project:
        table.add_row("Project", f"[cyan]{escape(ctx.project)}[/cyan]")  # User data, needs escape

    if ctx.agent:
        table.add_row("Agent", f"[magenta]{escape(ctx.agent)}[/magenta]")  # User data, needs escape

    # Duration and status
    if ctx.end_time:
        duration_color = "green" if ctx.duration_ms < 100 else "yellow" if ctx.duration_ms < 1000 else "red"
        table.add_row("Duration", f"[{duration_color}]{ctx.duration_ms:.2f}ms[/{duration_color}]")

        status = "✓ SUCCESS" if ctx.success else "✗ ERROR"
        status_color = "green" if ctx.success else "red"
        table.add_row("Status", f"[{status_color} bold]{status}[/{status_color} bold]")

    return table


def _create_params_display(ctx: ToolCallContext) -> Panel | None:
    """Create a panel displaying input parameters."""
    # Combine positional and keyword arguments
    all_params = {}

    # Add positional args (if any, numbered)
    if ctx.args:
        for i, arg in enumerate(ctx.args):
            all_params[f"arg_{i}"] = arg

    # Add keyword args
    if ctx.kwargs:
        all_params.update(ctx.kwargs)

    # Filter out internal/context parameters
    filtered_params = {
        k: v for k, v in all_params.items()
        if k not in {"ctx", "context", "_ctx"}
    }

    if not filtered_params:
        return None

    json_content = _safe_json_format(filtered_params)
    return _create_syntax_panel("Input Parameters", json_content, "json")


def _create_result_display(ctx: ToolCallContext) -> Panel:
    """Create a panel displaying the result or error."""
    if ctx.error:
        error_info = {
            "error_type": type(ctx.error).__name__,
            "error_message": str(ctx.error),
        }

        # Add additional error details if available
        if hasattr(ctx.error, "error_code"):
            error_info["error_code"] = ctx.error.error_code
        if hasattr(ctx.error, "data"):
            error_info["error_data"] = ctx.error.data

        json_content = _safe_json_format(error_info)
        return Panel(
            Syntax(json_content, "json", theme="monokai", line_numbers=False, word_wrap=True),
            title="[bold red]Error Details[/bold red]",
            border_style="red",
            box=box.ROUNDED,
            padding=(0, 1),
        )

    # Format result based on type
    result_str = _safe_json_format(ctx.result)
    return _create_syntax_panel("Result", result_str, "json")


def _create_tool_call_summary_table(ctx: ToolCallContext) -> Table:
    """Create a compact summary table for tool calls."""
    table = Table(
        box=box.HEAVY_HEAD,
        border_style="bright_blue",
        show_header=True,
        header_style="bold bright_white on bright_blue",
        title="[bold bright_yellow]⚡ MCP Tool Call[/bold bright_yellow]",
        title_style="bold",
    )

    table.add_column("Field", style="bold cyan", width=15)
    table.add_column("Value", style="white", overflow="fold")

    # Tool name
    table.add_row("Tool", f"[bold green]{ctx.tool_name}[/bold green]")  # We control tool names

    # Context info
    if ctx.agent:
        table.add_row("Agent", f"[magenta]{escape(ctx.agent)}[/magenta]")  # User data, needs escape
    if ctx.project:
        table.add_row("Project", f"[cyan]{escape(ctx.project)}[/cyan]")  # User data, needs escape

    # Timing
    table.add_row("Started", ctx.timestamp)

    if ctx.end_time:
        duration_color = "green" if ctx.duration_ms < 100 else "yellow" if ctx.duration_ms < 1000 else "red"
        table.add_row("Duration", f"[{duration_color}]{ctx.duration_ms:.2f}ms[/{duration_color}]")

        # Status
        if ctx.success:
            table.add_row("Status", "[bold green]✓ SUCCESS[/bold green]")
        else:
            error_msg = str(ctx.error) if ctx.error else "Unknown error"
            table.add_row("Status", "[bold red]✗ FAILED[/bold red]")
            table.add_row("Error", f"[red]{escape(error_msg[:100])}[/red]")  # External error messages, needs escape

    return table


def log_tool_call_start(ctx: ToolCallContext) -> None:
    """Log the start of a tool call with full details."""
    # Create the main panel with all information
    components = []

    # Add info table
    info_table = _create_info_table(ctx)
    components.append(info_table)

    # Add parameters if present
    params_panel = _create_params_display(ctx)
    if params_panel:
        components.append(params_panel)

    # Create main panel
    group = Group(*components)
    main_panel = Panel(
        group,
        title="[bold white on blue]🚀 MCP TOOL CALL STARTED [/bold white on blue]",
        border_style="bright_blue",
        box=box.DOUBLE,
        padding=(1, 2),
    )

    console.print()
    console.print(main_panel)


def log_tool_call_end(ctx: ToolCallContext) -> Optional[str]:
    """Log the end of a tool call with results."""
    if not ctx.end_time:
        ctx.end_time = time.perf_counter()

    panel = _build_tool_call_end_panel(ctx)
    console.print(panel)
    console.print()
    try:
        ctx.rendered_panel = _render_panel_to_text(panel)
    except Exception:
        ctx.rendered_panel = None
    return ctx.rendered_panel


def render_tool_call_panel(ctx: ToolCallContext) -> str:
    """Render the completion panel for a tool call to plain text without printing."""
    return _render_panel_to_text(_build_tool_call_end_panel(ctx))


def _build_tool_call_end_panel(ctx: ToolCallContext) -> Panel:
    """Construct the Rich panel summarizing a completed tool call."""
    components = []

    summary = _create_tool_call_summary_table(ctx)
    components.append(summary)

    result_panel = _create_result_display(ctx)
    components.append(result_panel)

    if ctx.success:
        title = "[bold white on green]✓ MCP TOOL CALL COMPLETED [/bold white on green]"
        border_style = "bright_green"
    else:
        title = "[bold white on red]✗ MCP TOOL CALL FAILED [/bold white on red]"
        border_style = "bright_red"

    group = Group(*components)
    return Panel(
        group,
        title=title,
        border_style=border_style,
        box=box.DOUBLE,
        padding=(1, 2),
    )


def _render_panel_to_text(panel: Panel) -> str:
    """Render a Rich panel to plain text (no ANSI color codes)."""
    capture_console = Console(
        stderr=True,
        force_terminal=True,
        width=120,
        record=True,
        color_system=None,
    )
    capture_console.print(panel)
    capture_console.print()
    return capture_console.export_text(clear=True)


def log_tool_call_complete(
    tool_name: str,
    args: tuple,
    kwargs: dict[str, Any],
    result: Any = None,
    error: Optional[Exception] = None,
    duration_ms: float = 0.0,
    project: Optional[str] = None,
    agent: Optional[str] = None,
) -> None:
    """Log a complete tool call (alternative to start/end pattern)."""
    ctx = ToolCallContext(
        tool_name=tool_name,
        args=list(args),
        kwargs=kwargs,
        project=project,
        agent=agent,
        result=result,
        error=error,
        success=error is None,
    )
    ctx.start_time = time.perf_counter() - (duration_ms / 1000)
    ctx.end_time = time.perf_counter()

    log_tool_call_end(ctx)


@contextmanager
def tool_call_logger(
    tool_name: str,
    args: tuple = (),
    kwargs: dict[str, Any] | None = None,
    project: Optional[str] = None,
    agent: Optional[str] = None,
):
    """Context manager for logging a complete tool call lifecycle.

    Usage:
        with tool_call_logger("send_message", kwargs={"to": ["agent1"], "subject": "test"}):
            result = await some_tool_function()
    """
    ctx = ToolCallContext(
        tool_name=tool_name,
        args=list(args),
        kwargs=kwargs or {},
        project=project,
        agent=agent,
    )

    # Log start - suppress errors to avoid breaking user code
    with suppress(Exception):
        log_tool_call_start(ctx)

    try:
        yield ctx
        ctx.success = True
    except Exception as e:
        ctx.error = e
        ctx.success = False
        raise
    finally:
        # Log end - suppress errors to avoid suppressing original exceptions
        with suppress(Exception):
            ctx.end_time = time.perf_counter()
            log_tool_call_end(ctx)


def log_info(message: str, **kwargs) -> None:
    """Log an informational message with Rich formatting."""
    text = Text(f"INFO  {message}", style="bold cyan")
    if kwargs:
        details = _safe_json_format(kwargs, max_length=500)
        panel = Panel(details, border_style="cyan", box=box.ROUNDED)
        console.print(text)
        console.print(panel)
    else:
        console.print(text)


def log_warning(message: str, **kwargs) -> None:
    """Log a warning message with Rich formatting."""
    text = Text(f"⚠  {message}", style="bold yellow")
    if kwargs:
        details = _safe_json_format(kwargs, max_length=500)
        panel = Panel(details, border_style="yellow", box=box.ROUNDED)
        console.print(text)
        console.print(panel)
    else:
        console.print(text)


def log_error(message: str, error: Optional[Exception] = None, **kwargs) -> None:
    """Log an error message with Rich formatting."""
    text = Text(f"✗ {message}", style="bold red")
    console.print(text)

    if error or kwargs:
        error_data = kwargs.copy()
        if error:
            error_data["error_type"] = type(error).__name__
            error_data["error_message"] = str(error)

        details = _safe_json_format(error_data, max_length=500)
        panel = Panel(details, border_style="red", box=box.ROUNDED, title="[bold red]Error Details[/bold red]")
        console.print(panel)


def log_success(message: str, **kwargs) -> None:
    """Log a success message with Rich formatting."""
    text = Text(f"✓ {message}", style="bold green")
    if kwargs:
        details = _safe_json_format(kwargs, max_length=500)
        panel = Panel(details, border_style="green", box=box.ROUNDED)
        console.print(text)
        console.print(panel)
    else:
        console.print(text)


def create_startup_panel(config: dict[str, Any]) -> Panel:
    """Create a beautiful startup panel showing configuration."""
    tree = Tree("🚀 [bold bright_white]MCP Agent Mail Server[/bold bright_white]")

    # Add configuration branches
    for section, values in config.items():
        section_branch = tree.add(f"[bold cyan]{section}[/bold cyan]")  # We control section names
        if isinstance(values, dict):
            for key, value in values.items():
                # Mask sensitive values
                if "token" in key.lower() or "secret" in key.lower() or "password" in key.lower():
                    display_value = "***" if value else "[dim]not set[/dim]"
                else:
                    display_value = escape(str(value))  # User's .env data, needs escape
                section_branch.add(f"[yellow]{key}[/yellow]: [white]{display_value}[/white]")  # We control keys
        else:
            section_branch.add(f"[white]{escape(str(values))}[/white]")  # User data, needs escape

    return Panel(
        tree,
        title="[bold white on blue]Server Configuration[/bold white on blue]",
        border_style="bright_blue",
        box=box.DOUBLE,
        padding=(1, 2),
    )
