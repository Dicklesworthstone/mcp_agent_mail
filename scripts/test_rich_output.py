#!/usr/bin/env python3
"""Test script to verify Rich library panel and syntax highlighting output.

This script tests whether Rich panels and JSON syntax highlighting are working
correctly in the current environment. Run this to debug why MCP tool logging
might not be showing beautiful panels.

Usage:
    python scripts/test_rich_output.py
    # OR
    chmod +x scripts/test_rich_output.py
    ./scripts/test_rich_output.py
"""
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from rich import box
from rich.panel import Panel
from rich.syntax import Syntax

from mcp_agent_mail.rich_logger import (
    ToolCallContext,
    _create_params_display,
    _create_result_display,
    console,
)


def test_basic_panel():
    """Test 1: Verify basic Rich panel rendering."""
    print("\n" + "=" * 80)
    print("TEST 1: Basic Rich Panel")
    print("=" * 80)
    print("Expected: Green double-bordered box with 'Hello World' inside\n")

    panel = Panel(
        "Hello World",
        title="[bold white]Test Panel[/bold white]",
        border_style="green",
        box=box.DOUBLE,
        padding=(1, 2)
    )
    console.print(panel)
    console.print()


def test_syntax_highlighting():
    """Test 2: Verify JSON syntax highlighting with Dracula theme."""
    print("\n" + "=" * 80)
    print("TEST 2: JSON Syntax Highlighting (Dracula Theme)")
    print("=" * 80)
    print("Expected: Blue double-bordered box with colorized JSON\n")

    json_str = """{
  "test": "value",
  "number": 42,
  "boolean": true,
  "array": [1, 2, 3],
  "nested": {
    "key": "nested_value"
  }
}"""

    syntax = Syntax(
        json_str,
        "json",
        theme="dracula",
        line_numbers=False,
        word_wrap=True,
        background_color="default",
    )

    panel = Panel(
        syntax,
        title="[bold bright_white]JSON Syntax Test[/bold bright_white]",
        border_style="bright_blue",
        box=box.DOUBLE,
        padding=(1, 2),
    )
    console.print(panel)
    console.print()


def test_params_display():
    """Test 3: Verify _create_params_display function."""
    print("\n" + "=" * 80)
    print("TEST 3: Input Parameters Display Function")
    print("=" * 80)
    print("Expected: Blue rounded box with syntax-highlighted JSON parameters\n")

    ctx = ToolCallContext(
        tool_name="test_tool",
        args=[],
        kwargs={
            "project_key": "/data/projects/test",
            "agent_name": "TestAgent",
            "limit": 10,
            "urgent_only": False,
        },
    )

    params_panel = _create_params_display(ctx)
    if params_panel:
        console.print(params_panel)
        console.print()
    else:
        print("❌ ERROR: _create_params_display returned None")


def test_result_display():
    """Test 4: Verify _create_result_display function."""
    print("\n" + "=" * 80)
    print("TEST 4: Result Display Function")
    print("=" * 80)
    print("Expected: Green rounded box with syntax-highlighted JSON result\n")

    ctx = ToolCallContext(
        tool_name="test_tool",
        args=[],
        kwargs={},
    )
    ctx.result = {
        "deliveries": [
            {
                "project": "/data/projects/smartedgar_mcp",
                "payload": {
                    "id": 2,
                    "subject": "Test Message",
                    "importance": "normal",
                },
            }
        ],
        "count": 1,
    }

    result_panel = _create_result_display(ctx)
    console.print(result_panel)
    console.print()


def test_error_display():
    """Test 5: Verify error display with syntax highlighting."""
    print("\n" + "=" * 80)
    print("TEST 5: Error Display Function")
    print("=" * 80)
    print("Expected: Red heavy-bordered box with syntax-highlighted error details\n")

    ctx = ToolCallContext(
        tool_name="test_tool",
        args=[],
        kwargs={},
    )

    # Simulate an error
    try:
        raise ValueError("This is a test error message")
    except ValueError as e:
        ctx.error = e

    error_panel = _create_result_display(ctx)
    console.print(error_panel)
    console.print()


def print_summary():
    """Print test summary."""
    print("\n" + "=" * 80)
    print("ALL TESTS COMPLETE")
    print("=" * 80)
    print("\nWhat you should have seen:")
    print("  ✓ Test 1: Green double-bordered box")
    print("  ✓ Test 2: Blue double-bordered box with COLORIZED JSON")
    print("  ✓ Test 3: Blue rounded box with COLORIZED parameter JSON")
    print("  ✓ Test 4: Green rounded box with COLORIZED result JSON")
    print("  ✓ Test 5: Red heavy-bordered box with COLORIZED error JSON")
    print("\nIf you see:")
    print("  ❌ Plain text with no colors → ANSI codes are being stripped")
    print("  ❌ No boxes/borders → Rich panels are not rendering")
    print("  ❌ JSON but no colors → Syntax highlighting is not working")
    print("\nEnvironment Info:")
    print(f"  Console color system: {console.color_system}")
    print(f"  Console is_terminal: {console.is_terminal}")
    print(f"  Console width: {console.width}")
    print(f"  Force terminal: {console._force_terminal}")
    print()


def main():
    """Run all tests."""
    print("\n" + "=" * 80)
    print("RICH OUTPUT TEST SUITE")
    print("=" * 80)
    print("Testing Rich library panel and syntax highlighting functionality\n")

    test_basic_panel()
    test_syntax_highlighting()
    test_params_display()
    test_result_display()
    test_error_display()
    print_summary()


if __name__ == "__main__":
    main()
