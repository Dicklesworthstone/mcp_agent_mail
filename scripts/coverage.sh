#!/bin/bash

# Dedicated Coverage Script for WorldArchitect.ai
# Runs all tests with comprehensive coverage analysis
#
# Usage:
#   ./coverage.sh                   # Unit tests with coverage + HTML report (default)
#   ./coverage.sh --integration     # Unit + integration tests with coverage + HTML report
#   ./coverage.sh --no-html         # Generate text report only (skip HTML)
#   ./coverage.sh --integration --no-html  # All tests with text report only
#
# HTML output goes to: /tmp/$PROJECT_NAME/coverage/

# Store project root before changing directories
PROJECT_ROOT="$PWD"

# Source directory for project files
SOURCE_DIR="$PROJECT_ROOT/src"

# Coverage output directory
COVERAGE_DIR="/tmp/$PROJECT_NAME/coverage"

# Check if virtual environment exists
if [ ! -d "$PROJECT_ROOT/.venv" ] && [ ! -d "$PROJECT_ROOT/venv" ]; then
    echo "Error: Virtual environment not found at $PROJECT_ROOT/.venv or $PROJECT_ROOT/venv"
    echo "Please create it with: python3 -m venv .venv"
    exit 1
fi

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[PASS]${NC} $1"
}

print_error() {
    echo -e "${RED}[FAIL]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

# Check if we're in the right directory
if [ ! -d "$SOURCE_DIR" ]; then
    print_error "src directory not found. Please run this script from the project root."
    exit 1
fi

# Parse command line arguments
include_integration=false
generate_html=true  # Default to generating HTML

for arg in "$@"; do
    case $arg in
        --integration)
            include_integration=true
            ;;
        --no-html)
            generate_html=false
            ;;
        *)
            print_warning "Unknown argument: $arg"
            ;;
    esac
done

# Create coverage output directory
mkdir -p "$COVERAGE_DIR"

# Change to source directory
cd "$SOURCE_DIR"

# Activate virtual environment
print_status "Activating virtual environment..."
if [ -f "$PROJECT_ROOT/.venv/bin/activate" ]; then
    source "$PROJECT_ROOT/.venv/bin/activate"
elif [ -f "$PROJECT_ROOT/venv/bin/activate" ]; then
    source "$PROJECT_ROOT/venv/bin/activate"
else
    print_error "Virtual environment activation script not found"
    exit 1
fi

print_status "🧪 Running coverage analysis..."
print_status "Setting TESTING=true for faster AI model usage"
if [ "$generate_html" = true ]; then
    print_status "HTML report will be generated at: $COVERAGE_DIR/index.html"
else
    print_status "HTML report generation disabled (--no-html specified)"
fi

if [ "$include_integration" = true ]; then
    print_status "Integration tests enabled (--integration flag specified)"
else
    print_status "Skipping integration tests (use --integration to include them)"
fi

# Check if coverage is installed
print_status "Checking coverage installation..."

# Check if coverage is importable (venv already activated)
if ! python -c "import coverage" 2>/dev/null; then
    print_warning "Coverage tool not found. Installing..."
    if ! pip install coverage; then
        print_error "Failed to install coverage"
        exit 1
    fi
    print_success "Coverage installed successfully"
else
    print_status "Coverage already installed"
fi

# Find all test files in tests subdirectory, excluding venv, prototype, manual_tests, and test_integration
test_files=()
while IFS= read -r -d '' file; do
    test_files+=("$file")
done < <(find ./tests -name "test_*.py" -type f \
    ! -path "./venv/*" \
    ! -path "./node_modules/*" \
    ! -path "./prototype/*" \
    ! -path "./tests/manual_tests/*" \
    ! -path "./tests/test_integration/*" \
    -print0)

# Also include test_integration directories if requested
if [ "$include_integration" = true ]; then
    if [ -d "./test_integration" ]; then
        print_status "Including integration tests from test_integration/"
        while IFS= read -r -d '' file; do
            test_files+=("$file")
        done < <(find ./test_integration -name "test_*.py" -type f -print0)
    fi

    if [ -d "./tests/test_integration" ]; then
        print_status "Including integration tests from tests/test_integration/"
        while IFS= read -r -d '' file; do
            test_files+=("$file")
        done < <(find ./tests/test_integration -name "test_*.py" -type f -print0)
    fi
fi

# Check if any test files exist
if [ ${#test_files[@]} -eq 0 ]; then
    print_warning "No test files found"
    exit 0
fi

print_status "Found ${#test_files[@]} test file(s) for coverage analysis"
echo

# Start timing
start_time=$(date +%s)
print_status "⏱️  Starting coverage analysis at $(date)"

# Clear any previous coverage data
coverage erase

# Initialize counters
total_tests=0
passed_tests=0
failed_tests=0
failed_test_files=()

# Run tests sequentially with coverage (sequential is required for accurate coverage)
for test_file in "${test_files[@]}"; do
    if [ -f "$test_file" ]; then
        total_tests=$((total_tests + 1))
        echo -n "[$total_tests/${#test_files[@]}] Running: $test_file ... "

        if TESTING=true coverage run --append --source=. "$test_file" >/dev/null 2>&1; then
            passed_tests=$((passed_tests + 1))
            echo -e "${GREEN}✓${NC}"
        else
            failed_tests=$((failed_tests + 1))
            failed_test_files+=("$test_file")
            echo -e "${RED}✗${NC}"
        fi
    fi
done

# Calculate test execution time
test_end_time=$(date +%s)
test_duration=$((test_end_time - start_time))

echo
print_status "⏱️  Test execution completed in ${test_duration}s"
print_status "📊 Generating coverage report..."

# Generate coverage reports
coverage_start_time=$(date +%s)

# Generate terminal coverage report
print_status "Generating text coverage report..."
coverage report > "$COVERAGE_DIR/coverage_report.txt"
coverage_report_exit_code=$?

# Display key coverage metrics
if [ $coverage_report_exit_code -eq 0 ]; then
    print_success "Coverage report generated successfully"

    # Extract and display key metrics
    echo
    print_status "📈 Coverage Summary:"
    echo "----------------------------------------"

    # Show overall coverage
    overall_coverage=$(tail -1 "$COVERAGE_DIR/coverage_report.txt" | awk '{print $4}')
    echo "Overall Coverage: $overall_coverage"

    # Show key file coverage
    echo
    echo "Key Files Coverage:"
    grep -E "(main\.py|gemini_service\.py|game_state\.py|firestore_service\.py)" "$COVERAGE_DIR/coverage_report.txt" | head -10

    echo "----------------------------------------"

    # Save full report to tmp and display excerpt
    echo
    print_status "📋 Full Coverage Report (saved to $COVERAGE_DIR/coverage_report.txt):"
    cat "$COVERAGE_DIR/coverage_report.txt"

else
    print_error "Failed to generate coverage report"
fi

# Generate HTML report if enabled
if [ "$generate_html" = true ]; then
    print_status "🌐 Generating HTML coverage report..."
    if coverage html --directory="$COVERAGE_DIR"; then
        print_success "HTML coverage report generated in $COVERAGE_DIR/"
        print_status "Open $COVERAGE_DIR/index.html in your browser to view detailed coverage"

        # Create a convenient symlink if possible
        if [ -w "/tmp" ]; then
            ln -sf "$COVERAGE_DIR/index.html" "/tmp/coverage.html" 2>/dev/null
            if [ $? -eq 0 ]; then
                print_status "Quick access: file:///tmp/coverage.html"
            fi
        fi
    else
        print_error "Failed to generate HTML coverage report"
    fi
else
    print_status "HTML report skipped (--no-html specified)"
fi

# Calculate timing
coverage_end_time=$(date +%s)
coverage_duration=$((coverage_end_time - coverage_start_time))
total_duration=$((coverage_end_time - start_time))

# Print timing summary
echo
print_status "⏱️  Timing Summary:"
echo "  Test execution: ${test_duration}s"
echo "  Coverage generation: ${coverage_duration}s"
echo "  Total time: ${total_duration}s"

# Print test summary
echo
print_status "🧪 Test Summary:"
echo "  Total tests: $total_tests"
echo "  Passed: $passed_tests"
echo "  Failed: $failed_tests"

# Show failed test details if any
if [ $failed_tests -gt 0 ]; then
    echo
    print_warning "Failed test files:"
    for failed_file in "${failed_test_files[@]}"; do
        echo "  - $failed_file"
    done
fi

# Final status
echo
print_status "📁 Coverage files saved to: $COVERAGE_DIR"
print_status "  - Text report: $COVERAGE_DIR/coverage_report.txt"
if [ "$generate_html" = true ]; then
    print_status "  - HTML report: $COVERAGE_DIR/index.html"
    print_status "  - Quick link: file://$COVERAGE_DIR/index.html"
fi

if [ $failed_tests -eq 0 ]; then
    print_success "✅ Coverage analysis complete - all tests passed!"
    exit 0
else
    print_error "❌ Coverage analysis complete - $failed_tests test(s) failed"
    exit 1
fi
