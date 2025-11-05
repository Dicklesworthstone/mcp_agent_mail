#!/bin/bash
set -euo pipefail

# Source bashrc to ensure PATH and environment variables are loaded
# This is necessary because some tools might be defined in bashrc
[[ -f ~/.bashrc ]] && source ~/.bashrc

# Check for correct number of arguments
if [ "$#" -lt 1 ] || [ "$#" -gt 3 ]; then
  echo "Usage: $0 <time-HH:MM> [remote-branch-name] [--continue]"
  echo "If a branch name is not specified, the script uses the current git branch."
  echo "Use --continue to resume the previous conversation instead of starting fresh."
  exit 1
fi

SCHEDULE_TIME="$1"
USE_CONTINUE=true

# Check if --continue flag is present (can be 2nd or 3rd argument)
for arg in "$@"; do
  if [[ "$arg" == "--continue" ]]; then
    USE_CONTINUE=true
    break
  fi
done

# Ensure the time is in HH:MM 24-hour format
if ! [[ "$SCHEDULE_TIME" =~ ^([01]?[0-9]|2[0-3]):[0-5][0-9]$ ]]; then
  echo "Error: time must be in HH:MM 24-hour format"
  exit 1
fi

# Parse arguments to get branch name based on position
REMOTE_BRANCH=""
# The branch name is the second argument if present and not --continue
if [ "$#" -ge 2 ]; then
  if [[ "$2" != "--continue" ]]; then
    REMOTE_BRANCH="$2"
  elif [ "$#" -ge 3 ] && [[ "$3" != "--continue" ]]; then
    # If 2nd arg is --continue, check if 3rd arg exists and is not --continue
    REMOTE_BRANCH="$3"
  fi
fi

# Resolve branch if not provided (with detached-HEAD guard)
if [ -z "$REMOTE_BRANCH" ]; then
  REMOTE_BRANCH=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || git branch --show-current || git rev-parse --abbrev-ref HEAD 2>/dev/null)
fi
if [ -z "$REMOTE_BRANCH" ] || [ "$REMOTE_BRANCH" = "HEAD" ]; then
  echo "Error: Could not determine current branch (detached HEAD?). Please specify a branch name or ensure you are in a valid Git repository."
  exit 1
fi

# Gather context uniformly for the resolved branch
echo "Gathering context for branch: $REMOTE_BRANCH"

# Check for an open PR on this branch using the 'gh' CLI tool
PR_INFO=""
if command -v gh >/dev/null 2>&1; then
  PR_INFO=$(gh pr list --head "$REMOTE_BRANCH" --state open --json number,title,url 2>/dev/null | jq -r '.[] | "PR #\(.number): \(.title)"' 2>/dev/null || echo "")
fi

# Check for a scratchpad file for additional context
SCRATCHPAD_INFO=""
SCRATCHPAD_FILE="roadmap/scratchpad_${REMOTE_BRANCH}.md"
if [ -f "$SCRATCHPAD_FILE" ]; then
  # Get the first few relevant lines of the scratchpad for context
  SCRATCHPAD_INFO=$(head -n 10 "$SCRATCHPAD_FILE" 2>/dev/null | grep -E "(Goal:|Task:|Current:|Status:)" | head -n 3 | tr '\n' ' ' || echo "")
fi

# Determine the default branch name (e.g., main, master, etc.)
# Use command substitution that won't fail with set -euo pipefail
DEFAULT_BRANCH=""
if git symbolic-ref --quiet refs/remotes/origin/HEAD >/dev/null 2>&1; then
  DEFAULT_BRANCH=$(git symbolic-ref --quiet refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@')
fi
if [ -z "$DEFAULT_BRANCH" ]; then
  # Fallback to 'main' if we can't determine the default branch
  DEFAULT_BRANCH="main"
fi

# Get recent commit messages from the current branch (not on default branch)
BRANCH_FOR_LOG="$REMOTE_BRANCH"
if [ -z "$BRANCH_FOR_LOG" ]; then
  BRANCH_FOR_LOG=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || git branch --show-current)
fi
RECENT_COMMITS=$(git log --oneline -3 origin/"$DEFAULT_BRANCH".."$BRANCH_FOR_LOG" 2>/dev/null | sed 's/^/  /' || echo "")

# Check for a TODO file that might provide context
TODO_INFO=""
TODO_FILE="TODO_${REMOTE_BRANCH}.md"
if [ -f "$TODO_FILE" ]; then
  TODO_INFO=$(head -n 5 "$TODO_FILE" 2>/dev/null | tr '\n' ' ' || echo "")
fi

# Build a comprehensive context message to be sent to Claude
BRANCH_MESSAGE="Resume work on branch: $REMOTE_BRANCH"

if [ -n "$PR_INFO" ]; then
  BRANCH_MESSAGE="$BRANCH_MESSAGE. Active $PR_INFO"
fi

if [ -n "$SCRATCHPAD_INFO" ]; then
  BRANCH_MESSAGE="$BRANCH_MESSAGE. Context: $SCRATCHPAD_INFO"
fi

if [ -n "$TODO_INFO" ]; then
  BRANCH_MESSAGE="$BRANCH_MESSAGE. TODO: $TODO_INFO"
fi

if [ -n "$RECENT_COMMITS" ]; then
  BRANCH_MESSAGE="$BRANCH_MESSAGE. Recent commits:$'\n'$RECENT_COMMITS"
fi

# Add a final instruction to review all available context
BRANCH_MESSAGE="$BRANCH_MESSAGE$'\n\n'Please review conversation history and any existing context to continue the work appropriately."

# --- SCHEDULING LOGIC ---
# Calculate the number of seconds to wait until the scheduled time.
# Cross-platform compatible date handling for GNU/Linux and macOS/BSD systems.

# Get current time in seconds since epoch
CURRENT_SECONDS=$(date +%s)

# Get target time in seconds since epoch (cross-platform compatible)
if [[ "$OSTYPE" == "darwin"* ]] || [[ "$OSTYPE" == "freebsd"* ]]; then
  # macOS/BSD date syntax
  TARGET_SECONDS=$(date -j -f "%H:%M" "$SCHEDULE_TIME" "+%s" 2>/dev/null)
  if [ $? -ne 0 ]; then
    echo "Error: Invalid time format for macOS/BSD date command"
    exit 1
  fi
else
  # GNU/Linux date syntax
  TARGET_SECONDS=$(date -d "$SCHEDULE_TIME" +%s 2>/dev/null)
  if [ $? -ne 0 ]; then
    echo "Error: Invalid time format for GNU date command"
    exit 1
  fi
fi

# If the target time has already passed today, schedule it for the same time tomorrow.
if [ "$TARGET_SECONDS" -lt "$CURRENT_SECONDS" ]; then
  TARGET_SECONDS=$((TARGET_SECONDS + 86400)) # Add 24 hours in seconds
fi

SLEEP_DURATION=$((TARGET_SECONDS - CURRENT_SECONDS))

# Validate sleep duration is reasonable (not negative, not more than 24 hours)
if [ "$SLEEP_DURATION" -lt 0 ] || [ "$SLEEP_DURATION" -gt 86400 ]; then
  echo "Error: Calculated sleep duration ($SLEEP_DURATION seconds) is invalid"
  exit 1
fi

# Format target time for display (cross-platform compatible)
if [[ "$OSTYPE" == "darwin"* ]] || [[ "$OSTYPE" == "freebsd"* ]]; then
  TARGET_TIME_DISPLAY=$(date -r "$TARGET_SECONDS" "+%Y-%m-%d %H:%M:%S")
else
  TARGET_TIME_DISPLAY=$(date -d "@$TARGET_SECONDS" "+%Y-%m-%d %H:%M:%S")
fi

echo "Waiting for $SLEEP_DURATION seconds until $TARGET_TIME_DISPLAY..."
echo "Press Ctrl+C to cancel."

# Set up signal handler to gracefully handle interruption
trap 'echo "\nScheduling cancelled by user."; exit 130' INT TERM

# Wait until the scheduled time and validate successful completion
if ! sleep "$SLEEP_DURATION"; then
  echo "Error: Sleep command was interrupted or failed"
  exit 1
fi

echo "Time reached! Launching Claude..."

# Run the claude command interactively, passing the gathered context as the initial prompt.
# Note: --dangerously-skip-permissions bypasses Claude's file access confirmation prompts.
# This is used here for automated scheduling but should be used carefully in interactive contexts.

# Ensure 'claude' CLI is available
if ! command -v claude >/dev/null 2>&1; then
  echo "Error: 'claude' CLI not found in PATH."
  exit 127
fi

if [ "$USE_CONTINUE" = true ]; then
  # Use --continue to resume the previous conversation
  claude --dangerously-skip-permissions --model sonnet --continue "$BRANCH_MESSAGE"
else
  # Start a fresh conversation with context
  claude --dangerously-skip-permissions --model sonnet "$BRANCH_MESSAGE"
fi
