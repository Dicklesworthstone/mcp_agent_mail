#!/bin/bash

# Comprehensive Python Linting Script
# Runs Ruff, isort, mypy, and Bandit for complete code quality analysis

set -euo pipefail  # Exit on any command failure, treat unset variables as errors, and catch pipeline failures

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
TARGET_DIR="${1:-src}"
FIX_MODE="${2:-false}"  # Pass 'fix' as second argument to auto-fix issues

echo -e "${BLUE}🔍 Running comprehensive Python linting on: ${TARGET_DIR}${NC}"
echo "=================================================="

# Ensure we're in virtual environment
if [[ "${VIRTUAL_ENV:-}" == "" ]]; then
    if [[ -f ".venv/bin/activate" ]]; then
        echo -e "${YELLOW}⚠️  Activating virtual environment...${NC}"
        source .venv/bin/activate
    elif [[ -f "venv/bin/activate" ]]; then
        echo -e "${YELLOW}⚠️  Activating virtual environment...${NC}"
        source venv/bin/activate
    else
        echo -e "${YELLOW}⚠️  No virtual environment found, using uv run...${NC}"
    fi
fi

# Function to run a linter with proper error handling
run_linter() {
    local tool_name="$1"
    local command="$2"
    local emoji="$3"

    echo -e "\n${BLUE}${emoji} Running ${tool_name}...${NC}"
    echo "Command: $command"

    if eval "$command"; then
        echo -e "${GREEN}✅ ${tool_name}: PASSED${NC}"
        return 0
    else
        echo -e "${RED}❌ ${tool_name}: FAILED${NC}"
        return 1
    fi
}

# Track overall status
overall_status=0

# 1. Ruff - Linting and Formatting
echo -e "\n${BLUE}📋 STEP 1: Ruff (Linting)${NC}"
if [[ "$FIX_MODE" == "fix" ]]; then
    ruff_cmd="ruff check $TARGET_DIR --fix"
else
    ruff_cmd="ruff check $TARGET_DIR"
fi

if ! run_linter "Ruff Linting" "$ruff_cmd" "📋"; then
    overall_status=1
fi

# Ruff formatting (always show what would change)
echo -e "\n${BLUE}🎨 STEP 1b: Ruff (Formatting)${NC}"
if [[ "$FIX_MODE" == "fix" ]]; then
    ruff_format_cmd="ruff format $TARGET_DIR"
else
    ruff_format_cmd="ruff format $TARGET_DIR --diff"
fi

if ! run_linter "Ruff Formatting" "$ruff_format_cmd" "🎨"; then
    overall_status=1
fi

# 2. isort - Import Sorting
echo -e "\n${BLUE}📚 STEP 2: isort (Import Sorting)${NC}"
if [[ "$FIX_MODE" == "fix" ]]; then
    isort_cmd="isort $TARGET_DIR"
else
    isort_cmd="isort $TARGET_DIR --check-only --diff"
fi

if ! run_linter "isort" "$isort_cmd" "📚"; then
    overall_status=1
fi

# 3. mypy - Static Type Checking
echo -e "\n${BLUE}🔬 STEP 3: mypy (Type Checking)${NC}"
mypy_cmd="mypy $TARGET_DIR"

if ! run_linter "mypy" "$mypy_cmd" "🔬"; then
    overall_status=1
fi

# 4. Bandit - Security Analysis
echo -e "\n${BLUE}🛡️  STEP 4: Bandit (Security Scanning)${NC}"
bandit_cmd="bandit -c pyproject.toml -r $TARGET_DIR -f txt"

if ! run_linter "Bandit" "$bandit_cmd" "🛡️"; then
    overall_status=1
fi

# Summary
echo -e "\n=================================================="
if [[ $overall_status -eq 0 ]]; then
    echo -e "${GREEN}🎉 ALL LINTING CHECKS PASSED!${NC}"
    echo -e "${GREEN}✅ Ruff linting, formatting, isort, mypy, and Bandit all successful${NC}"
else
    echo -e "${RED}❌ SOME LINTING CHECKS FAILED${NC}"
    echo -e "${YELLOW}💡 Run with 'fix' argument to auto-fix some issues:${NC}"
    echo -e "${YELLOW}   ./run_lint.sh $TARGET_DIR fix${NC}"
fi

echo -e "\n${BLUE}📊 Linting Summary:${NC}"
echo "  • Target: $TARGET_DIR"
echo "  • Mode: $([ "$FIX_MODE" == "fix" ] && echo "Auto-fix enabled" || echo "Check-only")"
echo "  • Tools: Ruff (lint+format), isort, mypy, Bandit"

exit $overall_status
