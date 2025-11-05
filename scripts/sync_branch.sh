#!/bin/bash

# This script helps synchronize a local Git branch with a remote branch
# that might have a different name. It sets the upstream tracking
# configuration and then performs a pull and a push. It always uses
# 'origin' as the remote and handles cases where local and remote
# branch names differ.

# Function to display error messages and exit the script
error_exit() {
    echo "Error: $1" >&2
    exit 1
}

# Function to display informational messages
info_message() {
    echo "Info: $1"
}

# --- 1. Get Branch Names ---

# These are shell variables, local to this script's execution,
# not persistent environment variables.
LOCAL_BRANCH=""
REMOTE_NAME="origin" # Hardcoded to 'origin' as requested
REMOTE_BRANCH=""

# Check if arguments are provided
if [ -n "$1" ] && [ -n "$2" ]; then # Expects local_branch and remote_branch as arguments
    LOCAL_BRANCH="$1"
    REMOTE_BRANCH="$2"
else
    # Prompt user for input if arguments are not provided
    read -p "Enter the name of your local branch (e.g., 'my-feature'): " LOCAL_BRANCH
    read -p "Enter the name of the remote branch (e.g., 'feature-on-server'): " REMOTE_BRANCH
fi

# Validate that all required names were entered
if [ -z "$LOCAL_BRANCH" ] || [ -z "$REMOTE_BRANCH" ]; then
    error_exit "Both inputs (local branch, remote branch) are required. Please provide them."
fi

info_message "Attempting to sync local branch '$LOCAL_BRANCH' with remote branch '$REMOTE_NAME/$REMOTE_BRANCH'..."

# --- 2. Pre-checks ---

# Check if currently inside a Git repository
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    error_exit "You are not currently inside a Git repository. Please navigate to a Git repository to use this script."
fi

# Check if the remote 'origin' exists
if ! git remote get-url "$REMOTE_NAME" > /dev/null 2>&1; then
    error_exit "Remote '$REMOTE_NAME' does not exist. Please ensure 'origin' is configured as a remote."
fi

# Store the current branch to return to it later
# This is a shell variable, local to this script's execution.
CURRENT_BRANCH_AT_START=$(git rev-parse --abbrev-ref HEAD)

# --- 3. Prepare Branch and Set Upstream ---

# Check if the local branch already exists
# This is a shell variable, local to this script's execution.
LOCAL_BRANCH_EXISTS="true"
if ! git show-ref --verify --quiet "refs/heads/$LOCAL_BRANCH"; then
    LOCAL_BRANCH_EXISTS="false"
fi

# Fetch the remote branch first to ensure its reference is available locally
info_message "Fetching from remote '$REMOTE_NAME' to ensure remote branch '$REMOTE_BRANCH' is known..."
if ! git fetch "$REMOTE_NAME" "$REMOTE_BRANCH"; then
    error_exit "Failed to fetch remote branch '$REMOTE_NAME/$REMOTE_BRANCH'. Please check the remote branch name and your network connection."
fi

# Verify the remote-tracking branch exists after fetch
if ! git show-ref --verify --quiet "refs/remotes/$REMOTE_NAME/$REMOTE_BRANCH"; then
    error_exit "Remote branch '$REMOTE_NAME/$REMOTE_BRANCH' does not exist on the remote or could not be fetched. Please check the remote branch name."
fi

# Switch to or create the local branch
if [ "$LOCAL_BRANCH_EXISTS" = "false" ]; then
    info_message "Local branch '$LOCAL_BRANCH' does not exist. Creating it and checking it out from '$REMOTE_NAME/$REMOTE_BRANCH'..."
    if ! git checkout -b "$LOCAL_BRANCH" "$REMOTE_NAME/$REMOTE_BRANCH"; then
        error_exit "Failed to create and checkout local branch '$LOCAL_BRANCH' from '$REMOTE_NAME/$REMOTE_BRANCH'."
    fi
else
    info_message "Local branch '$LOCAL_BRANCH' already exists. Checking it out..."
    if [ "$CURRENT_BRANCH_AT_START" != "$LOCAL_BRANCH" ]; then
        git checkout "$LOCAL_BRANCH" || error_exit "Failed to checkout local branch '$LOCAL_BRANCH'."
    fi
fi

# Explicitly set the upstream using git config - this is the most reliable method
# This writes the configuration directly into .git/config, making the link permanent.
info_message "Setting upstream for local branch '$LOCAL_BRANCH' to '$REMOTE_NAME/$REMOTE_BRANCH' using 'git config'..."
if git config branch."$LOCAL_BRANCH".remote "$REMOTE_NAME" && \
   git config branch."$LOCAL_BRANCH".merge "refs/heads/$REMOTE_BRANCH"; then
    info_message "Upstream for '$LOCAL_BRANCH' successfully set to '$REMOTE_NAME/$REMOTE_BRANCH' using git config."
else
    error_exit "Failed to set upstream for '$LOCAL_BRANCH' using git config. Check Git configuration and permissions."
fi


# --- 4. Perform Pull and Push ---

info_message "Performing 'git pull' for '$LOCAL_BRANCH'..."
# git pull will now use the permanently configured upstream
if git pull; then
    info_message "'git pull' completed successfully."
else
    echo "Warning: 'git pull' encountered issues. You might need to resolve conflicts manually."
    echo "Please check the output above for details."
fi

info_message "Performing 'git push' for '$LOCAL_BRANCH' to its specified upstream '$REMOTE_NAME/$REMOTE_BRANCH'..."
# Use explicit refspec for push to avoid ambiguity when local and remote branch names differ.
# This directly addresses the "The upstream branch of your current branch does not match..." message.
# This ensures the push goes to the intended remote branch, regardless of push.default setting.
if git push "$REMOTE_NAME" "$LOCAL_BRANCH":"$REMOTE_BRANCH"; then
    info_message "'git push' completed successfully."
else
    echo "Warning: 'git push' encountered issues. Your local changes might not have been pushed."
    echo "Please check the output above for details."
fi

echo ""
info_message "Synchronization process completed for branch '$LOCAL_BRANCH'."
echo "You can verify the upstream with: git branch -vv"

# Switch back to the original branch if we changed it and it's not the one we just synced
if [ "$CURRENT_BRANCH_AT_START" != "$LOCAL_BRANCH" ]; then
    info_message "Switching back to original branch '$CURRENT_BRANCH_AT_START'."
    git checkout "$CURRENT_BRANCH_AT_START" || error_exit "Failed to checkout original branch '$CURRENT_BRANCH_AT_START'."
fi
