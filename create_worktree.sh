#!/bin/bash

# This script creates a new Git worktree with a specified name.
# It prepends "worktree_" to the given name and automatically navigates to it.
#
# Usage (MUST be sourced to navigate):
#   source ./create_worktree.sh [name]
#   . ./create_worktree.sh [name]
#
# It also includes checks for name collisions with existing directories and local branches.

# Function to display error messages and exit the script
error_exit() {
    echo "Error: $1" >&2
    return 1 2>/dev/null || exit 1
}

# Function to display informational messages
info_message() {
    echo "Info: $1"
}

# --- 1. Get Worktree Name ---
RAW_WORKTREE_NAME=""

# Check if a worktree name was provided as a command-line argument
if [ -n "$1" ]; then
    RAW_WORKTREE_NAME="$1"
else
    # If no argument, prompt the user for the worktree name
    read -p "Enter the desired base name for the new worktree (e.g., 'my-feature'): " RAW_WORKTREE_NAME
fi

# Validate that a name was entered
if [ -z "$RAW_WORKTREE_NAME" ]; then
    error_exit "Worktree base name cannot be empty. Please provide a name."
fi

# Prepend "worktree_" to the provided name and make it a sibling directory
WORKTREE_NAME="worktree_${RAW_WORKTREE_NAME}"
WORKTREE_PATH="../${WORKTREE_NAME}"

info_message "Attempting to create a new worktree named '$WORKTREE_NAME'..."

# --- 2. Collision Checks ---

# Check 1: Does a directory with this name already exist as a sibling?
if [ -d "$WORKTREE_PATH" ]; then
    info_message "Directory '$WORKTREE_PATH' already exists as sibling. Navigating into it instead of creating a new worktree."
    
    if ! cd "$WORKTREE_PATH"; then
        echo "Error: Failed to navigate into existing directory '$WORKTREE_PATH'." >&2
        return 1 2>/dev/null || exit 1
    fi
    
    # Verify it's a Git repository/worktree
    if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "Error: Directory '$WORKTREE_PATH' exists but is not a Git repository/worktree." >&2
        return 1 2>/dev/null || exit 1
    fi
    
    current_branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '')"
    if [ "$current_branch" != "$WORKTREE_NAME" ]; then
        info_message "Warning: current branch '$current_branch' does not match expected '$WORKTREE_NAME'."
    fi
    
    echo "=============================================="
    echo "✅ Navigated to existing worktree directory!"
    echo "=============================================="
    info_message "You are now in: $(pwd)"
    
    echo ""
    echo "Useful commands:"
    echo "  git worktree list        # List all worktrees"
    echo "  git worktree remove      # Remove a worktree"
    echo "  git branch -d            # Delete the branch when done"
    
    return 0 2>/dev/null || exit 0  # Works whether sourced or executed
fi

# Check 2: Does a local branch with this name already exist?
# git show-ref --verify --quiet checks if a ref (like a branch) exists without outputting anything.
# It exits with 0 if it exists, non-zero otherwise.
if git show-ref --verify --quiet "refs/heads/$WORKTREE_NAME"; then
    error_exit "A local Git branch named '$WORKTREE_NAME' already exists. Please choose a different base name for the worktree to create a new, unique branch."
fi

# Check 3: Is this script being run inside a Git repository?
# This is crucial for 'git worktree add' to function.
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    error_exit "You are not currently inside a Git repository. Please navigate to a Git repository to create a worktree."
fi

# --- 3. Create the Worktree ---

# Use 'git worktree add' to create the new worktree as a sibling directory.
# The first argument is the path (directory name) for the new worktree.
# The -b flag explicitly tells Git to create a new branch with the specified name.
# If no starting point is given after the branch name, it defaults to HEAD.
if git worktree add "$WORKTREE_PATH" -b "$WORKTREE_NAME"; then
    info_message "Successfully created new Git worktree '$WORKTREE_NAME' at '$WORKTREE_PATH'."
    echo "A new branch '$WORKTREE_NAME' has been created and checked out in this worktree."
    echo ""

    # --- 4. Navigate into the new Worktree ---
    info_message "Navigating into the new worktree directory: '$WORKTREE_PATH'..."
    cd "$WORKTREE_PATH" || error_exit "Failed to navigate into the new worktree directory."

    echo "=============================================="
    echo "✅ Worktree created and navigated to!"
    echo "=============================================="
    info_message "You are now in: $(pwd)"

    echo ""
    echo "Useful commands:"
    echo "  git worktree list        # List all worktrees"
    echo "  git worktree remove      # Remove a worktree"
    echo "  git branch -d            # Delete the branch when done"
else
    # If 'git worktree add' fails for any reason (e.g., permissions, internal Git error)
    error_exit "Failed to create worktree '$WORKTREE_NAME'. Please check the error messages from Git above."
fi
