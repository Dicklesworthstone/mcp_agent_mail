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
from rich.align import Align
from rich.columns import Columns
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

# Global console instance for logging
# Force truecolor to ensure vivid themes and consistent styling in real TTYs
# and exec'd foreground runs; enable soft wrap for wide panels.
console = Console(stderr=True, force_terminal=True, color_system="truecolor", soft_wrap=True)


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


def _create_syntax_panel(
    title: str,
    content: str,
    language: str = "json",
    theme: str = "monokai",
    border_style: str = "cyan",
    title_style: str = "bold cyan",
) -> Panel:
    """Create a Rich Panel with syntax-highlighted content."""
    syntax = Syntax(
        content,
        language,
        theme=theme,
        line_numbers=False,
        word_wrap=True,
        background_color="default",
    )
    return Panel(
        syntax,
        title=f"[{title_style}]{title}[/{title_style}]",
        border_style=border_style,
        box=box.ROUNDED,
        padding=(0, 1),
    )


def _create_info_table(ctx: ToolCallContext) -> Table:
    """Create a table with tool call metadata."""
    table = Table(
        show_header=False,
        box=box.SIMPLE,
        padding=(0, 1),
        border_style="bright_blue",
        show_edge=False,
    )
    table.add_column("Icon", style="bold", width=3, no_wrap=True)
    table.add_column("Key", style="bold bright_yellow", width=17)
    table.add_column("Value", style="white", overflow="fold")

    # Add rows with icons
    table.add_row("🔧", "Tool", f"[bold bright_green]{ctx.tool_name}[/bold bright_green]")
    table.add_row("🕐", "Timestamp", f"[dim]{ctx.timestamp}[/dim]")

    if ctx.project:
        table.add_row("📦", "Project", f"[bright_cyan]{escape(ctx.project)}[/bright_cyan]")

    if ctx.agent:
        table.add_row("🤖", "Agent", f"[bright_magenta]{escape(ctx.agent)}[/bright_magenta]")

    # Duration and status
    if ctx.end_time:
        # Duration with gradient colors
        if ctx.duration_ms < 50:
            duration_style = "bold bright_green"
            duration_icon = "⚡"
        elif ctx.duration_ms < 100:
            duration_style = "bold green"
            duration_icon = "⚡"
        elif ctx.duration_ms < 500:
            duration_style = "bold yellow"
            duration_icon = "⏱"
        elif ctx.duration_ms < 1000:
            duration_style = "bold bright_yellow"
            duration_icon = "⏱"
        else:
            duration_style = "bold red"
            duration_icon = "🐌"

        table.add_row(duration_icon, "Duration", f"[{duration_style}]{ctx.duration_ms:.2f}ms[/{duration_style}]")

        # Status with visual indicator
        if ctx.success:
            table.add_row("✅", "Status", "[bold bright_green]SUCCESS[/bold bright_green]")
        else:
            table.add_row("❌", "Status", "[bold bright_red]FAILED[/bold bright_red]")

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

    # Use syntax highlighting for JSON-serializable data
    json_content = _safe_json_format(filtered_params)
    return _create_syntax_panel(
        "📥 Input Parameters",
        json_content,
        "json",
        theme="dracula",
        border_style="bright_blue",
        title_style="bold bright_white",
    )


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
            title="[bold bright_red]❌ Error Details[/bold bright_red]",
            border_style="bright_red",
            box=box.HEAVY,
            padding=(0, 1),
        )

    # Format result with enhanced styling
    result_str = _safe_json_format(ctx.result)
    return _create_syntax_panel(
        "📤 Result",
        result_str,
        "json",
        theme="dracula",
        border_style="bright_green",
        title_style="bold bright_white",
    )


def _create_tool_call_summary_table(ctx: ToolCallContext) -> Table:
    """Create a compact summary table for tool calls."""
    table = Table(
        box=box.DOUBLE_EDGE,
        border_style="bright_cyan",
        show_header=True,
        header_style="bold bright_white on bright_blue",
        title="[bold bright_yellow]⚡ MCP Tool Call Summary[/bold bright_yellow]",
        title_style="bold",
        padding=(0, 1),
        show_edge=True,
    )

    table.add_column("Field", style="bold bright_cyan", width=15, no_wrap=True)
    table.add_column("Value", style="white", overflow="fold")

    # Tool name with icon
    table.add_row("🔧 Tool", f"[bold bright_green]{ctx.tool_name}[/bold bright_green]")

    # Context info with icons
    if ctx.agent:
        table.add_row("🤖 Agent", f"[bright_magenta]{escape(ctx.agent)}[/bright_magenta]")
    if ctx.project:
        table.add_row("📦 Project", f"[bright_cyan]{escape(ctx.project)}[/bright_cyan]")

    # Timing with icon
    table.add_row("🕐 Started", f"[dim]{ctx.timestamp}[/dim]")

    if ctx.end_time:
        # Duration with gradient styling
        if ctx.duration_ms < 50:
            duration_display = f"[bold bright_green]⚡ {ctx.duration_ms:.2f}ms[/bold bright_green]"
        elif ctx.duration_ms < 100:
            duration_display = f"[bold green]⚡ {ctx.duration_ms:.2f}ms[/bold green]"
        elif ctx.duration_ms < 500:
            duration_display = f"[bold yellow]⏱ {ctx.duration_ms:.2f}ms[/bold yellow]"
        elif ctx.duration_ms < 1000:
            duration_display = f"[bold bright_yellow]⏱ {ctx.duration_ms:.2f}ms[/bold bright_yellow]"
        else:
            duration_display = f"[bold red]🐌 {ctx.duration_ms:.2f}ms[/bold red]"

        table.add_row("⏱ Duration", duration_display)

        # Status with enhanced visual indicator
        if ctx.success:
            table.add_row("📊 Status", "[bold bright_green]✅ SUCCESS[/bold bright_green]")
        else:
            error_msg = str(ctx.error) if ctx.error else "Unknown error"
            table.add_row("📊 Status", "[bold bright_red]❌ FAILED[/bold bright_red]")
            table.add_row("⚠️  Error", f"[red]{escape(error_msg[:100])}[/red]")

    return table


def log_tool_call_start(ctx: ToolCallContext) -> None:
    """Log the start of a tool call with full details."""
    # Create the main panel with all information
    components = []

    # Add a rule separator for visual clarity
    components.append(Rule(style="bright_blue"))

    # Add info table
    info_table = _create_info_table(ctx)
    components.append(info_table)

    # Add parameters if present
    params_panel = _create_params_display(ctx)
    if params_panel:
        components.append(Text())  # Spacer
        components.append(params_panel)

    # Create main panel with enhanced styling
    group = Group(*components)
    main_panel = Panel(
        group,
        title="[bold bright_white on bright_blue]🚀 MCP TOOL CALL STARTED[/bold bright_white on bright_blue]",
        subtitle="[dim]Executing...[/dim]",
        border_style="bright_blue",
        box=box.DOUBLE,
        padding=(1, 2),
    )

    console.print()
    console.print(main_panel)
    console.print()


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

    # Add a rule separator
    if ctx.success:
        components.append(Rule(style="bright_green", characters="═"))
    else:
        components.append(Rule(style="bright_red", characters="═"))

    # Add summary table
    summary = _create_tool_call_summary_table(ctx)
    components.append(summary)

    # Add spacer
    components.append(Text())

    # Add result panel
    result_panel = _create_result_display(ctx)
    components.append(result_panel)

    # Determine title and styling based on success
    if ctx.success:
        title = "[bold bright_white on bright_green]✅ MCP TOOL CALL COMPLETED[/bold bright_white on bright_green]"
        border_style = "bright_green"
        # Add performance indicator in subtitle
        if ctx.duration_ms < 100:
            subtitle = "[bold bright_green]⚡ Lightning Fast![/bold bright_green]"
        elif ctx.duration_ms < 500:
            subtitle = "[bold green]✓ Fast[/bold green]"
        else:
            subtitle = "[dim]Completed[/dim]"
    else:
        title = "[bold bright_white on bright_red]❌ MCP TOOL CALL FAILED[/bold bright_white on bright_red]"
        border_style = "bright_red"
        subtitle = "[bold red]Please review error details above[/bold red]"

    group = Group(*components)
    return Panel(
        group,
        title=title,
        subtitle=subtitle,
        border_style=border_style,
        box=box.DOUBLE,
        padding=(1, 2),
    )


def _render_panel_to_text(panel: Panel) -> str:
    """Render a Rich panel to plain text (no ANSI color codes)."""
    capture_console = Console(stderr=True, force_terminal=True, record=True, color_system=None)
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
    text = Text(f"ℹ️  {message}", style="bold bright_cyan")  # noqa: RUF001
    if kwargs:
        details = _safe_json_format(kwargs, max_length=500)
        syntax = Syntax(details, "json", theme="dracula", line_numbers=False, word_wrap=True)
        panel = Panel(
            syntax,
            title="[bold bright_cyan]ℹ️  Details[/bold bright_cyan]",  # noqa: RUF001
            border_style="bright_cyan",
            box=box.ROUNDED,
            padding=(0, 1),
        )
        console.print(text)
        console.print(panel)
    else:
        console.print(text)


def log_warning(message: str, **kwargs) -> None:
    """Log a warning message with Rich formatting."""
    text = Text(f"⚠️  {message}", style="bold bright_yellow")
    if kwargs:
        details = _safe_json_format(kwargs, max_length=500)
        syntax = Syntax(details, "json", theme="monokai", line_numbers=False, word_wrap=True)
        panel = Panel(
            syntax,
            title="[bold bright_yellow]⚠️  Warning Details[/bold bright_yellow]",
            border_style="bright_yellow",
            box=box.HEAVY,
            padding=(0, 1),
        )
        console.print(text)
        console.print(panel)
    else:
        console.print(text)


def log_error(message: str, error: Optional[Exception] = None, **kwargs) -> None:
    """Log an error message with Rich formatting."""
    text = Text(f"❌ {message}", style="bold bright_red")
    console.print(text)

    if error or kwargs:
        error_data = kwargs.copy()
        if error:
            error_data["error_type"] = type(error).__name__
            error_data["error_message"] = str(error)

        details = _safe_json_format(error_data, max_length=500)
        syntax = Syntax(details, "json", theme="monokai", line_numbers=False, word_wrap=True)
        panel = Panel(
            syntax,
            title="[bold bright_red]❌ Error Details[/bold bright_red]",
            border_style="bright_red",
            box=box.HEAVY,
            padding=(0, 1),
        )
        console.print(panel)


def log_success(message: str, **kwargs) -> None:
    """Log a success message with Rich formatting."""
    text = Text(f"✅ {message}", style="bold bright_green")
    if kwargs:
        details = _safe_json_format(kwargs, max_length=500)
        syntax = Syntax(details, "json", theme="dracula", line_numbers=False, word_wrap=True)
        panel = Panel(
            syntax,
            title="[bold bright_green]✅ Success Details[/bold bright_green]",
            border_style="bright_green",
            box=box.ROUNDED,
            padding=(0, 1),
        )
        console.print(text)
        console.print(panel)
    else:
        console.print(text)


def create_startup_panel(config: dict[str, Any]) -> Panel:
    """Create a beautiful startup panel showing configuration."""
    # Create main tree with enhanced styling
    tree = Tree("🚀 [bold bright_white]MCP Agent Mail Server[/bold bright_white]")

    # Add configuration branches with icons
    icon_map = {
        "environment": "🌍",
        "server": "🖥️",
        "database": "💾",
        "storage": "📁",
        "features": "✨",
        "security": "🔒",
        "logging": "📝",
    }

    for section, values in config.items():
        # Get icon for section
        section_icon = icon_map.get(section.lower(), "⚙️")
        section_branch = tree.add(f"{section_icon} [bold bright_cyan]{section}[/bold bright_cyan]")

        if isinstance(values, dict):
            for key, value in values.items():
                # Mask sensitive values
                if "token" in key.lower() or "secret" in key.lower() or "password" in key.lower():
                    display_value = "[dim red]●●●●●●●●[/dim red]" if value else "[dim]not set[/dim]"
                    key_style = "bright_red"
                else:
                    display_value = escape(str(value))
                    key_style = "bright_yellow"

                section_branch.add(f"[{key_style}]{key}[/{key_style}]: [white]{display_value}[/white]")
        else:
            section_branch.add(f"[white]{escape(str(values))}[/white]")

    return Panel(
        tree,
        title="[bold bright_white on bright_blue]🚀 Server Configuration[/bold bright_white on bright_blue]",
        subtitle="[dim]Ready to serve![/dim]",
        border_style="bright_blue",
        box=box.DOUBLE,
        padding=(1, 2),
    )


def create_metadata_table(metadata: dict[str, Any], title: str = "Metadata") -> Table:
    """Create a beautiful metadata table with icons and colors."""
    table = Table(
        title=f"[bold bright_cyan]{title}[/bold bright_cyan]",
        box=box.ROUNDED,
        border_style="bright_cyan",
        show_header=True,
        header_style="bold bright_white on bright_blue",
        padding=(0, 1),
    )

    table.add_column("Property", style="bold bright_yellow", width=20)
    table.add_column("Value", style="white", overflow="fold")

    for key, value in metadata.items():
        # Add visual indicators for different value types
        if isinstance(value, bool):
            display_value = "[bold bright_green]✓ True[/bold bright_green]" if value else "[dim]✗ False[/dim]"
        elif isinstance(value, (int, float)):
            display_value = f"[bright_cyan]{value}[/bright_cyan]"
        elif value is None:
            display_value = "[dim italic]null[/dim italic]"
        else:
            display_value = escape(str(value))

        table.add_row(key, display_value)

    return table


def create_data_tree(data: dict[str, Any], root_label: str = "Data") -> Tree:
    """Create a rich tree view for nested data structures."""
    tree = Tree(f"[bold bright_white]{root_label}[/bold bright_white]")

    def add_items(parent, items):
        """Recursively add items to the tree."""
        if isinstance(items, dict):
            for key, value in items.items():
                if isinstance(value, dict):
                    branch = parent.add(f"[bold bright_cyan]{escape(str(key))}[/bold bright_cyan]")
                    add_items(branch, value)
                elif isinstance(value, list):
                    branch = parent.add(f"[bold bright_magenta]{escape(str(key))}[/bold bright_magenta] [dim](list)[/dim]")
                    for i, item in enumerate(value):
                        if isinstance(item, (dict, list)):
                            subbranch = branch.add(f"[dim]{i}[/dim]")
                            add_items(subbranch, item)
                        else:
                            branch.add(f"[dim]{i}:[/dim] [white]{escape(str(item))}[/white]")
                else:
                    parent.add(f"[bright_yellow]{escape(str(key))}[/bright_yellow]: [white]{escape(str(value))}[/white]")
        elif isinstance(items, list):
            for i, item in enumerate(items):
                if isinstance(item, (dict, list)):
                    branch = parent.add(f"[dim]{i}[/dim]")
                    add_items(branch, item)
                else:
                    parent.add(f"[dim]{i}:[/dim] [white]{escape(str(item))}[/white]")

    add_items(tree, data)
    return tree


def log_message_with_metadata(
    message: str,
    metadata: dict[str, Any] | None = None,
    body: str | None = None,
    message_type: str = "info",
) -> None:
    """Log a rich message with optional metadata and body content.

    Args:
        message: The main message text
        metadata: Optional dictionary of metadata to display
        body: Optional body content (supports markdown)
        message_type: Type of message ('info', 'success', 'warning', 'error')
    """
    components = []

    # Add message header
    if message_type == "success":
        header = Text(f"✅ {message}", style="bold bright_green")
        border_style = "bright_green"
    elif message_type == "warning":
        header = Text(f"⚠️  {message}", style="bold bright_yellow")
        border_style = "bright_yellow"
    elif message_type == "error":
        header = Text(f"❌ {message}", style="bold bright_red")
        border_style = "bright_red"
    else:
        header = Text(f"ℹ️  {message}", style="bold bright_cyan")  # noqa: RUF001
        border_style = "bright_cyan"

    components.append(header)

    # Add metadata if present
    if metadata:
        components.append(Text())  # Spacer
        metadata_table = create_metadata_table(metadata)
        components.append(metadata_table)

    # Add body if present
    if body:
        components.append(Text())  # Spacer
        # Try to render as markdown first, fallback to plain text
        try:
            md_content = Markdown(body)
            body_panel = Panel(
                md_content,
                title="[bold bright_white]Message Body[/bold bright_white]",
                border_style=border_style,
                box=box.ROUNDED,
                padding=(0, 1),
            )
        except Exception:
            # Fallback to plain text
            body_panel = Panel(
                escape(body),
                title="[bold bright_white]Message Body[/bold bright_white]",
                border_style=border_style,
                box=box.ROUNDED,
                padding=(0, 1),
            )
        components.append(body_panel)

    # Print everything
    group = Group(*components)
    console.print(group)
    console.print()


def display_startup_banner(settings: Any, host: str, port: int, path: str) -> None:
    """Display an awesome startup banner with ASCII art, database stats, and Rich showcase."""
    from rich import box
    from rich.syntax import Syntax

    console.print()
    console.print()

    # ASCII Art Mail Logo
    mail_art = """
    ┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
    ┃                                                                      ┃
    ┃     ███╗   ███╗ ██████╗██████╗     ███╗   ███╗ █████╗ ██╗██╗         ┃
    ┃     ████╗ ████║██╔════╝██╔══██╗    ████╗ ████║██╔══██╗██║██║         ┃
    ┃     ██╔████╔██║██║     ██████╔╝    ██╔████╔██║███████║██║██║         ┃
    ┃     ██║╚██╔╝██║██║     ██╔═══╝     ██║╚██╔╝██║██╔══██║██║██║         ┃
    ┃     ██║ ╚═╝ ██║╚██████╗██║         ██║ ╚═╝ ██║██║  ██║██║███████╗    ┃
    ┃     ╚═╝     ╚═╝ ╚═════╝╚═╝         ╚═╝     ╚═╝╚═╝  ╚═╝╚═╝╚══════╝    ┃
    ┃                                                                      ┃
    ┃               📬  Agent Coordination via Message Passing  📨         ┃
    ┃                                                                      ┃
    ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
    """

    console.print(Text(mail_art, style="bold bright_cyan"))
    console.print()

    # Get database statistics
    db_stats = _get_database_stats()

    # Create two-column layout: Server Config + Database Stats
    server_table = Table(
        box=box.ROUNDED,
        border_style="bright_blue",
        show_header=True,
        header_style="bold bright_white on bright_blue",
        title="[bold bright_yellow]🚀 Server Configuration[/bold bright_yellow]",
        padding=(0, 1),
    )
    server_table.add_column("Setting", style="bold bright_cyan", width=18)
    server_table.add_column("Value", style="white", overflow="fold")

    server_table.add_row("🌍 Environment", f"[bold bright_green]{settings.environment}[/bold bright_green]")
    server_table.add_row("🔗 Endpoint", f"[bold bright_magenta]http://{host}:{port}{path}[/bold bright_magenta]")
    server_table.add_row("💾 Database", f"[dim]{settings.database.url}[/dim]")
    server_table.add_row("📁 Storage", f"[dim]{settings.storage.root}[/dim]")
    server_table.add_row(
        "🔒 Auth",
        "[bold bright_green]ENABLED[/bold bright_green]" if settings.http.bearer_token else "[dim]disabled[/dim]"
    )
    server_table.add_row(
        "📝 Tool Logging",
        "[bold bright_green]ENABLED[/bold bright_green]" if settings.tools_log_enabled else "[dim]disabled[/dim]"
    )

    # Database stats table
    stats_table = Table(
        box=box.ROUNDED,
        border_style="bright_magenta",
        show_header=True,
        header_style="bold bright_white on bright_magenta",
        title="[bold bright_yellow]📊 Database Statistics[/bold bright_yellow]",
        padding=(0, 1),
    )
    stats_table.add_column("Resource", style="bold bright_cyan", width=18)
    stats_table.add_column("Count", style="bright_yellow", justify="right")

    stats_table.add_row("📦 Projects", f"[bold bright_green]{db_stats['projects']}[/bold bright_green]")
    stats_table.add_row("🤖 Agents", f"[bold bright_green]{db_stats['agents']}[/bold bright_green]")
    stats_table.add_row("📬 Messages", f"[bold bright_green]{db_stats['messages']}[/bold bright_green]")
    stats_table.add_row("🔐 File Reservations", f"[bold bright_green]{db_stats['file_reservations']}[/bold bright_green]")
    stats_table.add_row("🔗 Contact Links", f"[bold bright_green]{db_stats['contact_links']}[/bold bright_green]")

    # Display tables side by side
    columns = Columns([server_table, stats_table], equal=True, expand=True)
    console.print(columns)
    console.print()

    # Prominent Web UI section
    from rich.style import Style  # local import to avoid global dependency

    ui_host_display = "localhost" if host in ("0.0.0.0", "::") else host
    ui_url = f"http://{ui_host_display}:{port}/mail"

    link_style = Style(bold=True, color="bright_cyan", link=ui_url)
    ui_text = Text()
    ui_text.append("Open the Web UI to view all agent messages:\n", style="bold bright_white")
    ui_text.append(ui_url, style=link_style)
    ui_text.append("\n\n", style="white")
    ui_text.append("Tip: Per-agent inbox: ", style="dim")
    ui_text.append(f"http://{ui_host_display}:{port}/mail/{{project}}/inbox/{{agent}}", style="bright_magenta")

    ui_panel = Panel(
        Align.center(ui_text),
        title="[bold bright_white on bright_blue]🌐 Web UI[/bold bright_white on bright_blue]",
        border_style="bright_blue",
        box=box.HEAVY,
        padding=(1, 2),
    )
    console.print(ui_panel)
    console.print()

    # Sample JSON with syntax highlighting (showcase!)
    sample_json = {
        "stats": db_stats,
    }

    json_str = _safe_json_format(sample_json)
    syntax = Syntax(
        json_str,
        "json",
        theme="dracula",
        line_numbers=False,
        word_wrap=True,
        background_color="default",
    )

    showcase_panel = Panel(
        syntax,
        title="[bold bright_white on bright_green]Stats Showcase[/bold bright_white on bright_green]",
        border_style="bright_green",
        box=box.DOUBLE,
        padding=(1, 2),
    )
    console.print(showcase_panel)
    console.print()

    # Success message
    if settings.tools_log_enabled:
        success_msg = Text()
        success_msg.append("✅ ", style="bold bright_green")
        success_msg.append("Rich Logging ENABLED", style="bold bright_white")
        success_msg.append(" — All MCP tool calls will be displayed with ", style="white")
        success_msg.append("beautiful panels", style="bold bright_cyan")
        success_msg.append(", ", style="white")
        success_msg.append("syntax highlighting", style="bold bright_magenta")
        success_msg.append(", and ", style="white")
        success_msg.append("performance metrics", style="bold bright_yellow")
        success_msg.append("! 🎨✨", style="white")

        console.print(Panel(
            Align.center(success_msg),
            border_style="bright_green",
            box=box.HEAVY,
            padding=(0, 2),
        ))

    console.print()
    console.print(Rule(style="bright_blue", characters="═"))
    console.print()


def _get_database_stats() -> dict[str, int]:
    """Get database statistics for startup banner."""
    try:
        import asyncio

        from sqlalchemy import func, select

        from .db import get_session
        from .models import Agent, AgentLink, FileReservation, Message, Project

        async def fetch_stats() -> dict[str, int]:
            try:
                async with get_session() as session:
                    projects = await session.scalar(select(func.count()).select_from(Project))
                    agents = await session.scalar(select(func.count()).select_from(Agent))
                    messages = await session.scalar(select(func.count()).select_from(Message))
                    file_reservations = await session.scalar(select(func.count()).select_from(FileReservation))
                    contact_links = await session.scalar(select(func.count()).select_from(AgentLink))

                    return {
                        "projects": projects or 0,
                        "agents": agents or 0,
                        "messages": messages or 0,
                        "file_reservations": file_reservations or 0,
                        "contact_links": contact_links or 0,
                    }
            except Exception:
                return {
                    "projects": 0,
                    "agents": 0,
                    "messages": 0,
                    "file_reservations": 0,
                    "contact_links": 0,
                }

        # Try to get stats, but don't fail startup if DB isn't ready
        try:
            loop = asyncio.get_event_loop()
            return loop.run_until_complete(fetch_stats())
        except RuntimeError:
            # No event loop, create one
            return asyncio.run(fetch_stats())

    except Exception:
        # If anything fails, return zeros
        return {
            "projects": 0,
            "agents": 0,
            "messages": 0,
            "file_reservations": 0,
            "contact_links": 0,
        }
