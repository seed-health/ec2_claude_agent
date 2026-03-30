#!/bin/bash

# Setup script for Claude Slack Bot
# Run this as root or with sudo on your EC2 instance
# This script is idempotent - safe to run multiple times

set -e

CLAUDE_USER="claude-bot"
WORKSPACE="${WORKSPACE_DIR:-/home/claude-bot/workspace}"

echo "=== Claude Slack Bot Setup ==="
echo ""

# ============================================
# Step 1: Verify Required Environment Variables
# ============================================
echo "=== Checking Required Environment Variables ==="

MISSING_VARS=()

if [ -z "$SLACK_BOT_TOKEN" ]; then
    MISSING_VARS+=("SLACK_BOT_TOKEN")
fi

if [ -z "$SLACK_SIGNING_SECRET" ]; then
    MISSING_VARS+=("SLACK_SIGNING_SECRET")
fi

if [ -z "$ANTHROPIC_API_KEY" ]; then
    MISSING_VARS+=("ANTHROPIC_API_KEY")
fi

if [ ${#MISSING_VARS[@]} -ne 0 ]; then
    echo "ERROR: Missing required environment variables:"
    for var in "${MISSING_VARS[@]}"; do
        echo "  - $var"
    done
    echo ""
    echo "Please export these variables and run again:"
    echo "  export SLACK_BOT_TOKEN='xoxb-...'"
    echo "  export SLACK_SIGNING_SECRET='...'"
    echo "  export ANTHROPIC_API_KEY='sk-ant-...'"
    exit 1
fi

echo "  SLACK_BOT_TOKEN: set"
echo "  SLACK_SIGNING_SECRET: set"
echo "  ANTHROPIC_API_KEY: set"
echo ""

# ============================================
# Step 2: Install Python Dependencies
# ============================================
echo "=== Installing Python Dependencies ==="

apt-get install -y python3 python3-pip -qq > /dev/null 2>&1
echo "  python3 and pip3 installed"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
    pip3 install --break-system-packages --ignore-installed -r "$SCRIPT_DIR/requirements.txt" -q
    echo "  Python dependencies installed from requirements.txt"
else
    echo "  WARNING: requirements.txt not found in $SCRIPT_DIR"
fi
echo ""

# ============================================
# Step 3: Create Claude Bot User
# ============================================
echo "=== Creating Claude Bot User ==="

if id "$CLAUDE_USER" &>/dev/null; then
    echo "  User $CLAUDE_USER already exists, skipping creation"
else
    useradd -m -s /bin/bash "$CLAUDE_USER"
    echo "  Created user: $CLAUDE_USER"
fi

# ============================================
# Step 4: Setup Workspace
# ============================================
echo ""
echo "=== Setting Up Workspace ==="

if [ -d "$WORKSPACE" ]; then
    echo "  Workspace $WORKSPACE already exists"
else
    echo "  Creating workspace directory: $WORKSPACE"
    mkdir -p "$WORKSPACE"
fi

# Setup worktrees directory for per-thread isolation
WORKTREES="${WORKTREES_DIR:-/home/claude-bot/worktrees}"
if [ -d "$WORKTREES" ]; then
    echo "  Worktrees directory $WORKTREES already exists"
else
    echo "  Creating worktrees directory: $WORKTREES"
    mkdir -p "$WORKTREES"
fi

# Ensure correct ownership (ignore errors if already owned)
chown -R "$CLAUDE_USER":"$CLAUDE_USER" "/home/$CLAUDE_USER" 2>/dev/null || true
echo "  Verified ownership of /home/$CLAUDE_USER"

# ============================================
# Step 5: Git Configuration
# ============================================
echo ""
echo "=== Setting Up Git Config for Claude User ==="

sudo -u "$CLAUDE_USER" git config --global user.name "Claude Bot" 2>/dev/null || true
sudo -u "$CLAUDE_USER" git config --global user.email "claude-bot@localhost" 2>/dev/null || true
sudo -u "$CLAUDE_USER" git config --global --add safe.directory "$WORKSPACE" 2>/dev/null || true
sudo -u "$CLAUDE_USER" git config --global --add safe.directory "$WORKTREES/*" 2>/dev/null || true
echo "  Git configured for $CLAUDE_USER"

# ============================================
# Step 5a: Install CLAUDE.local.md for Claude User
# ============================================
echo ""
echo "=== Installing CLAUDE.local.md for $CLAUDE_USER ==="

CLAUDE_CONFIG_DIR="/home/$CLAUDE_USER/.claude"
mkdir -p "$CLAUDE_CONFIG_DIR"

if [ -f "$SCRIPT_DIR/CLAUDE.local.md" ]; then
    cp "$SCRIPT_DIR/CLAUDE.local.md" "$CLAUDE_CONFIG_DIR/CLAUDE.local.md"
    chown -R "$CLAUDE_USER":"$CLAUDE_USER" "$CLAUDE_CONFIG_DIR"
    echo "  Installed CLAUDE.local.md to $CLAUDE_CONFIG_DIR/"
else
    echo "  WARNING: CLAUDE.local.md not found in $SCRIPT_DIR, skipping"
fi

# ============================================
# Step 5b: Set Environment Variables for Claude User
# ============================================
echo ""
echo "=== Setting Environment Variables for $CLAUDE_USER ==="

BASHRC="/home/$CLAUDE_USER/.bashrc"

# List of env vars to propagate to claude-bot
ENV_VARS=(ANTHROPIC_API_KEY GH_TOKEN ATLASSIAN_API_TOKEN ATLASSIAN_USER)

for VAR_NAME in "${ENV_VARS[@]}"; do
    VAR_VALUE="${!VAR_NAME}"
    if [ -n "$VAR_VALUE" ]; then
        if grep -q "^export $VAR_NAME=" "$BASHRC" 2>/dev/null; then
            sed -i "s|^export $VAR_NAME=.*|export $VAR_NAME='$VAR_VALUE'|" "$BASHRC"
            echo "  Updated $VAR_NAME in $BASHRC"
        else
            echo "export $VAR_NAME='$VAR_VALUE'" >> "$BASHRC"
            echo "  Added $VAR_NAME to $BASHRC"
        fi
    else
        echo "  Skipping $VAR_NAME (not set)"
    fi
done

# ============================================
# Step 6: Check GitHub CLI Auth
# ============================================
echo ""
echo "=== Checking GitHub CLI Authentication ==="

if sudo -u "$CLAUDE_USER" gh auth status &>/dev/null; then
    echo "  GitHub CLI: authenticated"
else
    echo "  GitHub CLI: NOT authenticated"
    echo ""
    echo "  To authenticate, run:"
    echo "    sudo -u $CLAUDE_USER gh auth login"
    echo ""
fi

# ============================================
# Step 7: Check Atlassian CLI Auth
# ============================================
echo ""
echo "=== Checking Atlassian CLI Authentication ==="

if sudo -u "$CLAUDE_USER" acli jira auth status &>/dev/null; then
    echo "  Atlassian CLI: authenticated"
else
    echo "  Atlassian CLI: NOT authenticated (or acli not installed)"
    echo ""
    echo "  To authenticate, run:"
    echo "    sudo -u $CLAUDE_USER acli jira auth login"
    echo "  Then select 'API Token' and enter your Atlassian credentials"
    echo ""
fi

# ============================================
# Summary
# ============================================
echo ""
echo "=== Setup Complete ==="
echo ""
echo "Workspace: $WORKSPACE"
echo ""
echo "If you haven't cloned your repo yet:"
echo "  sudo -u $CLAUDE_USER git clone <repo-url> $WORKSPACE"
echo ""
echo "To start the bot:"
echo "  sudo -E python3 app.py"
echo ""
echo "The bot will run on port 80 and use these env vars:"
echo "  - SLACK_BOT_TOKEN"
echo "  - SLACK_SIGNING_SECRET"
echo "  - ANTHROPIC_API_KEY"
