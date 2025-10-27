#!/usr/bin/env python3
"""Check the console environment when running as MCP server.

This helps diagnose why Rich output might not be showing colors.
"""
import os
import sys

sys.path.insert(0, "src")
from rich.console import Console

print("\n" + "=" * 80)
print("CONSOLE ENVIRONMENT DIAGNOSTIC")
print("=" * 80 + "\n")

print("Python Info:")
print(f"  Python version: {sys.version}")
print(f"  Python executable: {sys.executable}\n")

print("TTY Status:")
print(f"  sys.stderr.isatty(): {sys.stderr.isatty()}")
print(f"  sys.stdout.isatty(): {sys.stdout.isatty()}")
print(f"  sys.stdin.isatty(): {sys.stdin.isatty()}\n")

print("Environment Variables:")
env_vars = [
    "TERM", "COLORTERM", "NO_COLOR", "FORCE_COLOR",
    "CLICOLOR", "CLICOLOR_FORCE", "TERM_PROGRAM",
    "WT_SESSION", "WT_PROFILE_ID",  # Windows Terminal
]
for var in env_vars:
    value = os.environ.get(var, "(not set)")
    print(f"  {var}: {value}")

print("\nRich Detection:")
console_auto = Console(stderr=True)
console_forced = Console(stderr=True, force_terminal=True, color_system="truecolor")

print("  Auto-detect console:")
print(f"    is_terminal: {console_auto.is_terminal}")
print(f"    color_system: {console_auto.color_system}")
print(f"    width: {console_auto.width}")

print("  Forced console:")
print(f"    is_terminal: {console_forced.is_terminal}")
print(f"    color_system: {console_forced.color_system}")
print(f"    width: {console_forced.width}")

print("\nANSI Test:")
print("  If you see colors below, ANSI codes are working:")
print("  \033[31mRED\033[0m \033[32mGREEN\033[0m \033[34mBLUE\033[0m \033[93mYELLOW\033[0m")
print("  \033[1;97;41m BOLD WHITE ON RED \033[0m")

print("\n" + "=" * 80)
print("END DIAGNOSTIC")
print("=" * 80 + "\n")
