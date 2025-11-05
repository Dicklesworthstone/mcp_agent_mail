#!/bin/bash
# integrate.sh - This script helps developers integrate the latest changes from main and start fresh on a new branch.
# This script implements the standard integration pattern for the project
#
# Usage: ./integrate.sh [branch-name] [--force] [--new-branch] [--help]
#   branch-name: Optional custom branch name (default: dev{timestamp})
#   --force: Override hard stops for uncommitted/unpushed changes and integration PR warnings
#   --new-branch: Skip deleting the current branch (just create new branch)
#   --help: Show detailed help information
#
# Examples:
#   ./integrate.sh              # Creates dev{timestamp} branch
#   ./integrate.sh feature/foo  # Creates feature/foo branch
#   ./integrate.sh --force      # Force mode with dev{timestamp}
#   ./integrate.sh newb --force # Creates newb branch in force mode
#   ./integrate.sh --new-branch # Creates new dev{timestamp} without deleting current
#   ./integrate.sh --new-branch feature/bar # Creates feature/bar without deleting current

set -euo pipefail  # Exit on any error with stricter error handling

# Graceful terminator: exits when executed directly; returns when sourced.
# Optional args: die [exit_code] [message]
die() {
    local code="${1:-1}"
    local msg="${2:-}"
    # Tolerate unset color vars if called before they're defined
    local red="${RED:-}"
    local nc="${NC:-}"
    if [[ -n "$msg" ]]; then
      echo -e "${red}❌ ERROR: $msg${nc}" >&2
    fi
    # Only prompt in interactive TTYs (skip in CI/non-interactive shells)
    if [[ -t 1 && -z "${CI:-}" && -z "${NONINTERACTIVE:-}" ]]; then
      echo "Press Enter to continue or Ctrl+C to abort..."
      # Avoid failing set -e pipelines on read errors
      read -r || true
    fi
    if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
      return "$code"
    else
      exit "$code"
    fi
}

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Function to detect if commits were squash-merged into origin/main
detect_squash_merged_commits() {
    local commit_count=$1
    local squash_merged_count=0
    
    echo "   🔍 Checking if commits were squash-merged..."
    
    # Get list of commits not in origin/main
    local commits_list=$(git rev-list origin/main..HEAD 2>/dev/null)
    
    for commit_hash in $commits_list; do
        # Get commit subject (first line of commit message)
        local commit_subject=$(git log --format="%s" -n 1 "$commit_hash" 2>/dev/null)
        if [ -n "$commit_subject" ]; then
            # Remove PR number suffix to match squash-merged commits (e.g., "Fix bug (#123)" -> "Fix bug")
            # Use POSIX-compatible regex that matches single and multi-digit PR numbers
            local base_subject=$(echo "$commit_subject" | sed 's/ (#[0-9]\+)$//')
            
            # Skip if base_subject is empty (prevents matching all commits)
            if [ -z "$base_subject" ]; then
                echo -e "   ${YELLOW}?${NC} $commit_hash → empty subject after stripping PR number"
                continue
            fi
            
            # Search for similar commit message in recent origin/main commits (configurable depth)
            local search_depth="${DETECT_SQUASH_SEARCH_DEPTH:-200}"
            local similar_commit
            similar_commit=$(git log origin/main --oneline "-${search_depth}" --fixed-strings --grep="$base_subject" 2>/dev/null | head -1)
            
            if [ -n "$similar_commit" ]; then
                local main_commit_hash=$(echo "$similar_commit" | cut -d' ' -f1)
                local local_files=$(git diff-tree --no-commit-id --name-only -r "$commit_hash" | sort)
                local main_files=$(git diff-tree --no-commit-id --name-only -r "$main_commit_hash" | sort)
                
                # If same files changed, likely squash-merged
                if [ "$local_files" = "$main_files" ] && [ -n "$local_files" ]; then
                    squash_merged_count=$((squash_merged_count + 1))
                    echo -e "   ${GREEN}✓${NC} $commit_hash → squash-merged as $main_commit_hash"
                else
                    echo -e "   ${YELLOW}?${NC} $commit_hash → similar message but different files"
                fi
            else
                echo -e "   ${RED}✗${NC} $commit_hash → no similar commit found in origin/main"
            fi
        fi
    done
    
    # Return success if all commits appear squash-merged
    if [ $commit_count -eq $squash_merged_count ] && [ $squash_merged_count -gt 0 ]; then
        echo -e "   ${GREEN}🎉 All $commit_count commit(s) were squash-merged into origin/main${NC}"
        return 0
    else
        echo -e "   ${YELLOW}⚠️  Only $squash_merged_count of $commit_count commits appear squash-merged${NC}"
        return 1
    fi
}

# Source ~/.bashrc to ensure environment is properly set up
if [ -f ~/.bashrc ]; then
    source ~/.bashrc
fi

# Ensure PATH includes common local binary locations
export PATH="$HOME/.local/bin:$PATH"

# Check for required tools and provide helpful messages
check_dependencies() {
    local missing_tools=()
    
    if ! command -v gh >/dev/null 2>&1; then
        missing_tools+=("gh (GitHub CLI)")
    fi
    
    if ! command -v jq >/dev/null 2>&1; then
        missing_tools+=("jq")
    fi
    
    if [ ${#missing_tools[@]} -gt 0 ]; then
        echo -e "${YELLOW}⚠️  Some optional tools are missing:${NC}"
        for tool in "${missing_tools[@]}"; do
            echo "   - $tool"
        done
        echo "   Integration will continue but some features may be limited."
        echo ""
    fi
}

# Check dependencies early
check_dependencies

# Utility function for safe command execution with fallback
safe_gh_command() {
    local cmd="$*"
    if command -v gh >/dev/null 2>&1; then
        # Suppress auth credential errors that don't affect functionality
        if eval "$cmd" 2>/dev/null; then
            return 0
        else
            # Command failed, but this might be expected (e.g., no PRs found)
            return 1
        fi
    else
        # gh not available
        return 1
    fi
}

# Show help if requested
show_help() {
    cat << 'EOF'
integrate.sh - Integration workflow for fresh branch creation

USAGE:
    ./integrate.sh [branch-name] [--force] [--new-branch] [--help]

ARGUMENTS:
    branch-name     Optional custom branch name (default: dev{timestamp})

OPTIONS:
    --force         Override hard stops for uncommitted/unpushed changes and integration PR warnings
    --new-branch    Skip deleting the current branch (just create new branch)
    --help          Show this help message

EXAMPLES:
    ./integrate.sh                    # Creates dev{timestamp} branch
    ./integrate.sh feature/foo        # Creates feature/foo branch
    ./integrate.sh --force            # Force mode with dev{timestamp}
    ./integrate.sh newb --force       # Creates newb branch in force mode
    ./integrate.sh --new-branch       # Creates new dev{timestamp} without deleting current
    ./integrate.sh --new-branch bar   # Creates bar branch without deleting current

SAFETY FEATURES:
    • Hard stops for uncommitted changes (override with --force)
    • Hard stops for unpushed commits (override with --force)
    • Warnings for integration PR conflicts (override with --force)
    • Smart branch deletion only when safe (merged/clean branches)
    • Divergence detection with manual resolution options

WORKFLOW:
    1. Check current branch safety (uncommitted/unpushed changes)
    2. Switch to main branch
    3. Smart sync with origin/main (detects divergence)
    4. Check for problematic integration PRs
    5. Create fresh branch from updated main
    6. Optionally delete old branch if safe

EOF
}

# Parse arguments
FORCE_MODE=false
NEW_BRANCH_MODE=false
CUSTOM_BRANCH_NAME=""

# Single-pass argument parsing
while (( $# )); do
    case "$1" in
        --new-branch)
            NEW_BRANCH_MODE=true
            echo "🌿 NEW BRANCH MODE: Will not delete current branch"
            # Check if next argument exists and is not a flag
            if [[ $# -gt 1 && "$2" != --* ]]; then
                CUSTOM_BRANCH_NAME="$2"
                shift  # consume the branch name
            fi
            ;;
        --force)
            FORCE_MODE=true
            echo -e "${RED}🚨 FORCE MODE: Overriding safety checks${NC}"
            ;;
        -h|--help)
            show_help
            die 0
            ;;
        --*)
            die 1 "Unknown flag: $1"
            ;;
        *)
            if [[ -z "$CUSTOM_BRANCH_NAME" ]]; then
                CUSTOM_BRANCH_NAME="$1"
            else
                echo "Multiple branch names provided. Using: $CUSTOM_BRANCH_NAME" >&2
            fi
            ;;
    esac
    shift
done

echo -e "${GREEN}🔄 Starting integration process...${NC}"

# Fetch latest changes from origin/main first to ensure accurate comparisons
echo "📡 Fetching latest changes from origin/main..."
err_file="$(mktemp -t integrate_fetch_err.XXXXXX)"
if ! GIT_TERMINAL_PROMPT=0 git fetch --prune origin main 2>"$err_file"; then
    echo "❌ Error: Failed to fetch updates from origin/main."
    echo "   Possible causes: network issues, authentication problems, or repository unavailability."
    # Try to provide more specific error information
    if ! git ls-remote --exit-code origin >/dev/null 2>&1; then
        echo "   Remote 'origin' appears to be unreachable."
    fi
    if grep -qi 'auth' "$err_file"; then
        echo "   Authentication seems to be required or failing. Try: gh auth login or reconfigure your git credentials."
    fi
    echo "   Details (last 10 lines):"
    tail -n 10 "$err_file" || true
    rm -f "$err_file"
    die 1 "Fetch failed; cannot safely compare against origin/main."
else
    rm -f "$err_file"
fi

# Stop test server for current branch if running
current_branch=$(git branch --show-current)
if [ "$current_branch" != "main" ]; then
    echo "🛑 Stopping test server for branch '$current_branch'..."
    ./test_server_manager.sh stop "$current_branch" 2>/dev/null || true
fi

# Check for unmerged changes on current branch
should_delete_branch=false
if [ "$current_branch" != "main" ] && [ "$NEW_BRANCH_MODE" = false ]; then
    echo "⚠️  WARNING: You are on branch '$current_branch'"

    # Check if current branch has uncommitted changes - HARD STOP
    if ! git diff --quiet || ! git diff --cached --quiet; then
        echo -e "${RED}❌ HARD STOP: You have uncommitted changes on '$current_branch'${NC}"
        echo "   Staged changes:"
        git diff --cached --name-only | sed 's/^/     /'
        echo "   Unstaged changes:"
        git diff --name-only | sed 's/^/     /'
        echo ""
        if [ "$FORCE_MODE" = true ]; then
            echo -e "${RED}🚨 FORCE MODE: Stashing uncommitted changes to proceed${NC}"
            if git stash push -m "integrate.sh --force: auto-stash on $(date -u +"%Y-%m-%d %H:%M:%S UTC")"; then
                echo "   ✅ Changes stashed successfully"
                echo "   To recover: git stash pop"
            else
                die 1 "Failed to stash changes"
            fi
        else
            echo "   Please commit or stash your changes before integrating."
            echo "   Use: git add -A && git commit -m \"your message\""
            echo "   Or:  git stash"
            die 1 "Uncommitted changes must be handled before integration"
        fi
    fi

    # Check if current branch has unmerged commits - HARD STOP
    # First check: Compare local branch to its remote tracking branch (preferred)
    if git_upstream=$(git rev-parse --abbrev-ref @{upstream} 2>/dev/null) && [ -n "$git_upstream" ]; then
        echo "   Checking sync status with remote tracking branch: $git_upstream"
        if [[ "$(git rev-parse HEAD)" == "$(git rev-parse "$git_upstream" 2>/dev/null)" ]]; then
            # Local branch has same commit hash as remote tracking branch - safe to delete
            should_delete_branch=true
            echo -e "${GREEN}✅ Branch '$current_branch' is synced with remote and will be deleted after integration${NC}"
        else
            # Local branch differs from remote tracking branch
            local_commits=$(git rev-list --count "$git_upstream"..HEAD 2>/dev/null || echo "0")
            remote_commits=$(git rev-list --count HEAD.."$git_upstream" 2>/dev/null || echo "0")
            echo -e "${RED}❌ HARD STOP: Branch '$current_branch' is not synced with remote '$git_upstream':${NC}"
            echo "   • Local commits ahead: $local_commits"
            echo "   • Remote commits ahead: $remote_commits"
            echo ""
            if [ "$local_commits" -gt 0 ]; then
                echo "   📋 LOCAL-ONLY COMMITS:"
                git log --oneline "$git_upstream"..HEAD | head -5 | sed 's/^/     /'
                [ "$local_commits" -gt 5 ] && echo "     ...and $((local_commits - 5)) more commits"
                echo ""
            fi
            if [ "$remote_commits" -gt 0 ]; then
                echo "   📋 REMOTE-ONLY COMMITS:"
                git log --oneline HEAD.."$git_upstream" | head -5 | sed 's/^/     /'
                [ "$remote_commits" -gt 5 ] && echo "     ...and $((remote_commits - 5)) more commits"
                echo ""
            fi
            if [ "$FORCE_MODE" = true ]; then
                echo -e "${RED}🚨 FORCE MODE: Proceeding anyway (unsync will be ignored)${NC}"
                # Initialize should_delete_branch in FORCE_MODE to prevent uninitialized variable
                should_delete_branch=false
            else
                echo "   Options to sync branch:"
                echo "   • If PR merged: Branch is likely safe to delete with --force"
                echo "   • Pull latest: git pull origin $current_branch"
                echo "   • Push changes: git push origin HEAD:$current_branch"
                die 1 "Branch '$current_branch' has unsynced commits with remote"
            fi
        fi
    else
        # Fallback: No remote tracking branch - use origin/main comparison (current logic)
        echo "   No remote tracking branch found, checking against origin/main"
        commit_count=$(git rev-list --count origin/main..HEAD 2>/dev/null || echo "0")
        if [[ $commit_count -gt 0 ]]; then
            echo -e "${RED}❌ HARD STOP: Branch '$current_branch' has $commit_count commit(s) not in origin/main:${NC}"
            echo ""
            echo "   📋 COMMIT SUMMARY:"
            git log --oneline origin/main..HEAD | head -10 | sed 's/^/     /'
            echo ""
            echo "   📊 FILES CHANGED:"
            git diff --name-only origin/main..HEAD | head -10 | sed 's/^/     /'
            echo ""
            
            # Check if commits were squash-merged before requiring --force
            if detect_squash_merged_commits $commit_count; then
                echo -e "${GREEN}✅ Proceeding automatically - all commits were squash-merged into origin/main${NC}"
                should_delete_branch=true
            elif [ "$FORCE_MODE" = true ]; then
                echo -e "${RED}🚨 FORCE MODE: Proceeding anyway (commits not in origin/main will be abandoned)${NC}"
                # Initialize should_delete_branch in FORCE_MODE to prevent uninitialized variable
                should_delete_branch=false
            else
                echo "   These commits are not in origin/main. Options:"
                echo "   • If already merged via PR: Changes were likely squash-merged, safe to proceed with --force"
                echo "   • If not merged: Push changes first: git push origin HEAD:$current_branch"
                echo "   • Create PR: gh pr create"
                die 1 "Branch '$current_branch' has unmerged commits"
            fi
        else
            # Branch is clean (no uncommitted changes, no commits not in origin/main)
            should_delete_branch=true
            echo -e "${GREEN}✅ Branch '$current_branch' is clean and will be deleted after integration${NC}"
        fi
    fi
fi

echo -e "\n${GREEN}1. Switching to main branch...${NC}"
git checkout main

echo -e "\n${GREEN}2. Smart sync with origin/main...${NC}"
# Skip fetch here - origin/main was already fetched at script start to ensure accurate branch comparisons

# Helper function to extract GitHub repository URL from git remote
get_github_repo_url() {
    git config --get remote.origin.url | sed 's/.*github.com[:/]\([^/]*\/[^/]*\).*/\1/' | sed 's/.git$//'
}

# Helper function to check if we need to wait for existing integration-related PRs
check_existing_sync_pr() {
    if command -v gh >/dev/null 2>&1 && command -v jq >/dev/null 2>&1; then
        # Check for sync PRs created by this script (exact title match) - collect into proper JSON array
        # Robust PR fetching with comprehensive error handling
        if pr_data=$(gh pr list --author "@me" --state open --json number,url,title 2>/dev/null); then
            existing_sync_prs=$(echo "$pr_data" | jq -c '[ .[] | select(.title == "Sync main branch commits (integrate.sh)") ]' 2>/dev/null || echo '[]')
        else
            existing_sync_prs='[]'
        fi
        sync_count=$(echo "$existing_sync_prs" | jq 'length')

        if [ "$sync_count" -gt 0 ]; then
            if [ "$sync_count" -eq 1 ]; then
                # Single sync PR - extract details
                pr_number=$(echo "$existing_sync_prs" | jq -r '.[0].number')
                pr_url=$(echo "$existing_sync_prs" | jq -r '.[0].url')
                echo "⚠️  Found existing sync PR #$pr_number: $pr_url"
                echo "   This PR was created by integrate.sh to sync main branch"
            else
                # Multiple sync PRs - list them all
                echo "⚠️  Found $sync_count existing sync PRs created by integrate.sh:"
                echo "$existing_sync_prs" | jq -r '.[] | "   PR #\(.number): \(.url)"'
                echo "   Please merge these PRs first, then re-run integrate.sh"
            fi

            if [ "$FORCE_MODE" = true ]; then
                echo "🚨 FORCE MODE: Proceeding with integration despite sync PR(s)"
                return 0
            else
                if [ "$sync_count" -eq 1 ]; then
                    pr_number=$(echo "$existing_sync_prs" | jq -r '.[0].number')
                    echo "   Please merge this PR first, then re-run integrate.sh"
                    echo "   Or run: gh pr merge $pr_number --merge"
                else
                    echo "   Please merge these PRs first, then re-run integrate.sh"
                fi
                die 1 "Active sync PRs must be handled before creating new branch"
            fi
        fi

        # Check for any open PRs that modify integrate.sh or integration workflows (informational only)
        # Robust integration PR checking with error handling
        if pr_files_data=$(gh pr list --state open --limit 50 --json number,url,title,files 2>/dev/null); then
            integration_prs=$(echo "$pr_files_data" | jq -c '[ .[] | select(.files[]?.filename | test("integrate\\.sh|integration")) ]' 2>/dev/null || echo '[]')
        else
            integration_prs='[]'
        fi
        pr_count=$(echo "$integration_prs" | jq 'length')

        if [ "$pr_count" -gt 0 ]; then
            echo "ℹ️  Found $pr_count open PR(s) modifying integration workflows:"
            echo "$integration_prs" | jq -r '.[] | "   PR #\(.number): \(.title) - \(.url)"'
            echo ""
            echo "   These PRs modify integration infrastructure but don't block your current branch."
            echo "   Integration will proceed normally."
            echo ""
        fi
    elif command -v gh >/dev/null 2>&1; then
        # Fallback when jq is not available - only check for exact sync PRs
        echo "ℹ️  Checking for integration conflicts (jq not available, using basic check)..."
        # Simplified fallback check for sync PRs
        if sync_prs=$(gh pr list --author "@me" --state open 2>/dev/null | grep -i "sync.*main" || true); then
            if [ -n "$sync_prs" ]; then
                echo "⚠️  Found potential sync PR(s). If integration fails, try:"
                echo "   ./integrate.sh --force"
            fi
        fi
    fi
}

# Check for existing sync PRs before proceeding
check_existing_sync_pr

# Detect relationship between local main and origin/main
if git merge-base --is-ancestor HEAD origin/main; then
    # Local main is behind origin/main → safe fast-forward
    echo -e "${GREEN}✅ Fast-forwarding to latest origin/main${NC}"
    if ! git merge --ff-only origin/main; then
        die 1 "Fast-forward merge with origin/main failed. Please resolve manually."
    fi

elif git merge-base --is-ancestor origin/main HEAD; then
    # Local main is ahead of origin/main → create PR for commits
    echo -e "${GREEN}✅ Local main ahead, creating PR to sync${NC}"
    commit_count=$(git rev-list --count origin/main..HEAD)
    echo "   Found $commit_count commits ahead of origin/main"

    # Generate timestamp for branch naming
    timestamp=$(date +%Y%m%d-%H%M%S)

    # Create temporary branch for PR
    sync_branch="sync-main-$timestamp"
    echo "   Creating sync branch: $sync_branch"

    if ! git checkout -b "$sync_branch"; then
        die 1 "Failed to create sync branch"
    fi

    if ! git push -u origin HEAD; then
        die 1 "Failed to push sync branch"
    fi

    # Create PR if gh is available
    if command -v gh >/dev/null 2>&1; then
        pr_title="Sync main branch commits (integrate.sh)"
        # Dynamic commit listing based on count
        commit_limit=${PR_COMMIT_LIMIT:-10}
        if [ "$commit_count" -le "$commit_limit" ]; then
            commit_list=$(git log --oneline origin/main..HEAD)
        else
            commit_list=$(git log --oneline origin/main..HEAD | head -"$commit_limit")
            commit_list="$commit_list
   ...and $((commit_count - commit_limit)) more commits not shown"
        fi

        pr_body="Auto-generated PR to sync $commit_count commits that were ahead on local main.

This PR was created by integrate.sh to handle repository branch protection rules.

Commits included:
$commit_list

Please review and merge to complete the integration process."

        if pr_url=$(gh pr create --title "$pr_title" --body "$pr_body" 2>/dev/null); then
            echo -e "${GREEN}✅ Created PR: $pr_url${NC}"
            echo "   Please review and merge the PR, then re-run integrate.sh"
            die 0
        else
            echo "⚠️  Could not create PR automatically. Please create one manually:"
            echo "   Branch: $sync_branch"
            echo "   URL: https://github.com/$(get_github_repo_url)/compare/$sync_branch"
            die 1 "Could not create PR automatically. Please create one manually using the URL above"
        fi
    else
        echo "⚠️  gh CLI not available. Please create PR manually:"
        echo "   Branch: $sync_branch"
        echo "   URL: https://github.com/$(get_github_repo_url)/compare/$sync_branch"
        die 1 "gh CLI not available. Please create PR manually using the URL above"
    fi

else
    # Branches have diverged → warn and stop
    echo -e "${RED}❌ DIVERGENCE DETECTED: Local main and origin/main have diverged${NC}"
    echo ""
    echo "📊 Divergence Details:"
    echo "   • Local main has commits that aren't on origin/main"
    echo "   • Origin/main has commits that aren't on local main"
    echo "   • Manual resolution required to prevent contaminated branches"
    echo ""

    # Show divergence information
    local_only=$(git rev-list --count origin/main..HEAD)
    remote_only=$(git rev-list --count HEAD..origin/main)
    echo "📈 Commit Counts:"
    echo "   • Local-only commits: $local_only"
    echo "   • Remote-only commits: $remote_only"
    echo ""

    if [ "$local_only" -gt 0 ]; then
        echo "🔍 Recent local-only commits:"
        git log --oneline origin/main..HEAD | head -5 | sed 's/^/   /'
        [ "$local_only" -gt 5 ] && echo "   ...and $((local_only - 5)) more commits"
        echo ""
    fi

    if [ "$remote_only" -gt 0 ]; then
        echo "🔍 Recent remote-only commits:"
        git log --oneline HEAD..origin/main | head -5 | sed 's/^/   /'
        [ "$remote_only" -gt 5 ] && echo "   ...and $((remote_only - 5)) more commits"
        echo ""
    fi

    echo -e "${YELLOW}🛠️  Resolution Options:${NC}"
    echo ""
    echo "1. 🔄 Merge origin/main into local main:"
    echo "   git merge origin/main"
    echo "   (Creates merge commit, preserves both histories)"
    echo ""
    echo "2. ⏮️  Reset local main to match origin/main:"
    echo "   git reset --hard origin/main"
    echo "   (⚠️  WARNING: Discards local commits permanently)"
    echo ""
    echo "3. 🚀 Push local commits as separate PR:"
    echo "   git checkout -b sync-local-commits"
    echo "   git push -u origin sync-local-commits"
    echo "   gh pr create"
    echo "   git checkout main && git reset --hard origin/main"
    echo ""
    echo "4. 🔍 Manual review and resolution:"
    echo "   Review each commit and decide what to keep"
    echo ""

    if [ "$FORCE_MODE" = true ]; then
        echo -e "${RED}🚨 FORCE MODE: Would normally stop here, but --force was used${NC}"
        echo "   Performing merge to resolve divergence..."
        if ! git merge --no-ff origin/main -m "integrate.sh: Force merge divergent main histories (--force mode)"; then
            die 1 "Force merge failed. Please resolve conflicts manually."
        fi
        echo "   ✅ Force merge completed"
    else
        echo -e "${RED}🛑 Integration stopped to prevent branch contamination${NC}"
        echo "   Choose one of the resolution options above, then re-run integrate.sh"
        die 1 "Integration stopped to prevent branch contamination"
    fi
fi

# Check if there are any local branches that haven't been pushed
echo -e "\n${GREEN}3. Checking for unmerged local branches...${NC}"
# Fix regex escaping for ahead branch detection and proper main branch filtering
unpushed_branches=$(git for-each-ref --format='%(refname:short) %(upstream:track)' refs/heads \
  | awk '$1!="main"' \
  | grep -F '[ahead' || true)
if [ -n "$unpushed_branches" ]; then
    echo "⚠️  WARNING: Found branches with unpushed commits:"
    echo "$unpushed_branches"
    echo ""
fi

echo -e "\n${GREEN}4. Determining branch name...${NC}"
if [ -n "$CUSTOM_BRANCH_NAME" ]; then
    branch_name="$CUSTOM_BRANCH_NAME"
    echo "   Using custom branch name: $branch_name"
else
    timestamp=$(date +%s)
    branch_name="dev${timestamp}"
    echo "   Using timestamp-based branch name: $branch_name"
fi

echo -e "\n${GREEN}5. Creating fresh branch from main...${NC}"
git checkout -b "$branch_name"

# Delete the old branch if it was clean (and not in --new-branch mode)
if [ "$should_delete_branch" = true ] && [ "$current_branch" != "main" ] && [ "$NEW_BRANCH_MODE" = false ]; then
    echo -e "\n${GREEN}6. Checking if branch '$current_branch' can be safely deleted...${NC}"
    # Check multiple conditions to determine if branch is safe to delete
    branch_can_be_deleted=false
    deletion_reason=""

    # Check 1: Is it merged into local main?
    if git branch --merged main | grep -q "^[[:space:]]*$current_branch$"; then
        branch_can_be_deleted=true
        deletion_reason="merged into local main"
    # Check 2: Is it merged into remote main?
    elif git ls-remote --heads origin | grep -q "refs/heads/$current_branch" && \
         git branch -r --merged origin/main | grep -q "origin/$current_branch"; then
        branch_can_be_deleted=true
        deletion_reason="merged into remote main"
    # Check 3: Does it have a merged PR?
    elif command -v gh >/dev/null 2>&1; then
        # Check for merged PRs with better error handling
        if merged_pr=$(gh pr list --state merged --head "$current_branch" --json number --jq '.[0].number // empty' 2>/dev/null) && [ -n "$merged_pr" ]; then
            branch_can_be_deleted=true
            deletion_reason="has merged PR #$merged_pr"
        fi
    fi

    if [ "$branch_can_be_deleted" = true ]; then
        echo "   ✓ Branch is safe to delete ($deletion_reason)"
        echo "   Deleting branch '$current_branch'..."
        git branch -D "$current_branch"
        echo -e "${GREEN}✅ Deleted clean branch '$current_branch'${NC}"
    else
        echo "⚠️  Branch '$current_branch' could not be verified as merged"
        echo "   The branch was clean locally but may have unmerged changes"
        echo "   To force delete: git branch -D $current_branch"
    fi
fi

echo -e "\n${GREEN}✅ Integration complete! You are now on a fresh '$branch_name' branch with latest main changes.${NC}"
echo -e "${GREEN}📍 Current branch: $(git branch --show-current)${NC}"
