#!/bin/bash
#
# GitHub Self-Hosted Runner Setup Script for Claude Bot Commands
# This script downloads, configures, and starts a GitHub Actions runner
#

set -e

# Check if --no-install flag is present (quick check before processing)
NO_AUTO_INSTALL="false"
for arg in "$@"; do
    if [[ "$arg" == "--no-install" ]]; then
        NO_AUTO_INSTALL="true"
        break
    fi
done

# Auto-install to home directory if running from project (unless --no-install)
SCRIPT_NAME="$(basename "$0")"
HOME_SCRIPT="$HOME/$SCRIPT_NAME"

if [[ "$NO_AUTO_INSTALL" != "true" && "$0" != "$HOME_SCRIPT" && ! -f "$HOME_SCRIPT" ]]; then
    echo "🔄 Installing script to home directory..."
    echo "   (Use --no-install to skip this behavior)"
    cp "$0" "$HOME_SCRIPT"
    chmod +x "$HOME_SCRIPT"
    echo "✅ Script installed to: $HOME_SCRIPT"
    echo "▶️  Running from home directory..."
    exec "$HOME_SCRIPT" "$@"
elif [[ "$NO_AUTO_INSTALL" != "true" && "$0" != "$HOME_SCRIPT" && -f "$HOME_SCRIPT" ]]; then
    echo "🔄 Script already exists in home directory, running from there..."
    echo "   (Use --no-install to run from current location)"
    exec "$HOME_SCRIPT" "$@"
fi

# Configuration
RUNNER_DIR="$HOME/actions-runner"
RUNNER_VERSION="2.311.0"
LABELS="self-hosted,claude"

# Determine repository URL: argument > env var > git remote > error
REPO_URL=""
# Parse --repo argument
while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo)
            REPO_URL="$2"
            shift 2
            ;;
        --token)
            RUNNER_TOKEN="$2"
            shift 2
            ;;
        --help|-h)
            echo "Usage: $0 [--repo <repository-url>] [--token <runner-token>] [--no-install]"
            echo "  --repo: GitHub repository URL (e.g., https://github.com/user/repo)"
            echo "  --token: GitHub runner token"
            echo "  --no-install: Skip auto-install to home directory"
            echo "  If repo not specified, will try to detect from git remote origin"
            exit 0
            ;;
        --no-install)
            NO_AUTO_INSTALL="true"
            shift
            ;;
        *)
            shift
            ;;
    esac
done

# If not set by argument, check environment variable
if [[ -z "$REPO_URL" && -n "$GITHUB_REPO_URL" ]]; then
    REPO_URL="$GITHUB_REPO_URL"
fi

# If still not set, try to get from git remote
if [[ -z "$REPO_URL" ]]; then
    if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        REPO_URL="$(git config --get remote.origin.url 2>/dev/null || echo '')"
        # Convert SSH to HTTPS format if needed
        if [[ "$REPO_URL" =~ ^git@github\.com:(.+)\.git$ ]]; then
            REPO_URL="https://github.com/${BASH_REMATCH[1]}"
        fi
    fi
fi

# If still not set, print error and exit
if [[ -z "$REPO_URL" ]]; then
    echo -e "${RED}❌ Repository URL not specified.${NC}"
    echo "Please either:"
    echo "  1. Run from within a git repository with origin remote"
    echo "  2. Use --repo <repository-url> argument"
    echo "  3. Set GITHUB_REPO_URL environment variable"
    echo ""
    echo "Example: $0 --repo https://github.com/user/repo"
    exit 1
fi

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}🚀 GitHub Runner Setup for Claude Bot Commands${NC}"
echo "=============================================================="
echo ""

# Function to print colored output
print_step() {
    echo -e "${BLUE}▶️  $1${NC}"
}

print_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

print_error() {
    echo -e "${RED}❌ $1${NC}"
}

# Check if running on supported OS
print_step "Checking operating system..."
OS="linux"
ARCH="x64"

case "$(uname -s)" in
    Linux*)     OS="linux" ;;
    Darwin*)    OS="osx" ;;
    MINGW*)     OS="win" ;;
    *)
        print_error "Unsupported operating system: $(uname -s)"
        echo "This script supports Linux, macOS, and Windows (Git Bash)"
        exit 1
        ;;
esac

case "$(uname -m)" in
    x86_64*)    ARCH="x64" ;;
    arm64*)     ARCH="arm64" ;;
    aarch64*)   ARCH="arm64" ;;
    *)
        print_warning "Architecture $(uname -m) detected. Using x64 as default."
        ARCH="x64"
        ;;
esac

print_success "Detected: $OS-$ARCH"

# Check if runner directory already exists
if [[ -d "$RUNNER_DIR" ]]; then
    print_warning "Runner directory already exists: $RUNNER_DIR"
    read -p "Remove existing directory and start fresh? (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        print_step "Removing existing runner directory..."
        rm -rf "$RUNNER_DIR"
        print_success "Removed existing directory"
    else
        print_error "Cannot proceed with existing directory. Please remove it manually or choose a different location."
        exit 1
    fi
fi

# Create runner directory
print_step "Creating runner directory: $RUNNER_DIR"
mkdir -p "$RUNNER_DIR"
cd "$RUNNER_DIR"
print_success "Created directory"

# Download runner package
PACKAGE_NAME="actions-runner-${OS}-${ARCH}-${RUNNER_VERSION}.tar.gz"
DOWNLOAD_URL="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${PACKAGE_NAME}"

print_step "Downloading GitHub Actions runner..."
echo "URL: $DOWNLOAD_URL"

if command -v curl &> /dev/null; then
    curl -o "$PACKAGE_NAME" -L "$DOWNLOAD_URL"
elif command -v wget &> /dev/null; then
    wget -O "$PACKAGE_NAME" "$DOWNLOAD_URL"
else
    print_error "Neither curl nor wget found. Please install one of them."
    exit 1
fi

print_success "Downloaded runner package"

# Extract package
print_step "Extracting runner package..."
tar xzf "$PACKAGE_NAME"
print_success "Extracted runner"

# Get runner token from user
echo ""
print_warning "IMPORTANT: You need to get a runner token from GitHub"
echo ""
echo "1. Go to: ${REPO_URL}/settings/actions/runners"
echo "2. Click 'New self-hosted runner'"
echo "3. Select your OS: $OS"
echo "4. Copy the token from the configuration command"
echo ""
print_step "Waiting for runner token..."

# Check for token from environment or command line arguments first
if [[ -n "${RUNNER_TOKEN:-}" ]]; then
    print_success "Using RUNNER_TOKEN (environment or command line)"
else
    # Only prompt interactively if no token provided
    while true; do
        read -s -p "Enter your GitHub runner token: " RUNNER_TOKEN
        echo  # Print newline since -s suppresses echo
        if [[ -n "$RUNNER_TOKEN" ]]; then
            break
        else
            print_error "Token cannot be empty. Please try again."
        fi
    done
fi

# Configure runner
print_step "Configuring GitHub runner..."
echo "Repository: $REPO_URL"
echo "Labels: $LABELS"

./config.sh --url "$REPO_URL" --token "$RUNNER_TOKEN" --labels "$LABELS" --unattended

if [[ $? -eq 0 ]]; then
    print_success "Runner configured successfully"
else
    print_error "Failed to configure runner"
    exit 1
fi

# Install and start service (Linux/macOS only)
if [[ "$OS" != "win" ]]; then
    print_step "Installing runner as system service..."

    if [[ $EUID -eq 0 ]]; then
        print_warning "Running as root. Installing service directly..."
        ./svc.sh install
        ./svc.sh start
    else
        print_step "Installing service (requires sudo)..."
        sudo ./svc.sh install

        print_step "Starting service..."
        sudo ./svc.sh start

        # Check service status
        print_step "Checking service status..."
        if sudo ./svc.sh status; then
            print_success "Service is running"
        else
            print_warning "Service may not be running properly"
        fi
    fi
else
    print_warning "Windows detected. You'll need to start the runner manually:"
    echo "Run: ./run.cmd"
fi

# Verify runner is online
print_step "Verifying runner registration..."
sleep 3

echo ""
print_success "Runner setup completed!"
echo ""
echo "📋 Next Steps:"
echo "1. Check runner status at: ${REPO_URL}/settings/actions/runners"
echo "2. Look for your runner with labels: $LABELS"
echo "3. Status should show: Online"
echo ""
echo "🧪 Test the system:"
echo "1. Start Claude bot server: ./start-claude-bot.sh"
echo "2. Go to any PR and comment: /claude Hello!"
echo "3. Watch for Claude's response"
echo ""

if [[ "$OS" != "win" ]]; then
    echo "🔧 Service Management:"
    echo "- Check status: sudo ./svc.sh status"
    echo "- Stop service: sudo ./svc.sh stop"
    echo "- Start service: sudo ./svc.sh start"
    echo "- View logs: sudo journalctl -u actions.runner.* -f"
    echo ""
fi

echo "📁 Runner installed in: $RUNNER_DIR"
echo ""
print_success "Setup complete! 🎉"
