#!/bin/bash
# A script to add, commit, and push changes to GitHub.
# It uses a default timestamped message if none is provided.
# Portable version - works in any git repository

# Ensure we are in the correct directory for git commands
# Use git to find the project root instead of hardcoded path
PROJECT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
if [[ -z "$PROJECT_ROOT" ]]; then
    echo "❌ Error: Not in a git repository. Please run this script from within a git project."
    exit 1
fi

cd "$PROJECT_ROOT" || {
    echo "❌ Error: Could not change to project root: $PROJECT_ROOT"
    exit 1
}

# --- New Logic ---
# Generate the Pacific Time timestamp.
# Using "America/Los_Angeles" is the correct way to handle PST/PDT automatically.
TIMESTAMP=$(TZ='America/Los_Angeles' date '+%Y-%m-%d %H:%M:%S %Z')

# Check if a commit message argument was provided.
if [ "$#" -eq 0 ]; then
  # If no argument, create the default message.
  COMMIT_MSG="commit at this time ${TIMESTAMP}"
else
  # If an argument exists, combine it with the timestamp.
  COMMIT_MSG="$* ${TIMESTAMP}"
fi
# --- End New Logic ---

echo "Staging all changes..."
git add .

# Use the dynamically created commit message
echo "Committing with message: '${COMMIT_MSG}'..."
git commit -m "${COMMIT_MSG}"

echo "Pushing changes to GitHub..."
if git push 2>/dev/null; then
    echo "✅ Push complete."
else
    echo "⚠️  Push failed - likely no git remote configured."
    echo "   To add a remote: git remote add origin <repository-url>"
    echo "   Changes have been committed locally."
fi

# Start test server for current branch (if script exists)
current_branch=$(git branch --show-current)
if [ "$current_branch" != "main" ] && [ -f "./test_server_manager.sh" ]; then
    echo ""
    echo "🚀 Starting test server for branch '$current_branch'..."
    ./test_server_manager.sh start "$current_branch"
elif [ "$current_branch" != "main" ]; then
    echo ""
    echo "ℹ️  Test server manager not found - skipping server startup"
else
    echo ""
    echo "ℹ️  Skipping test server startup for main branch"
fi
