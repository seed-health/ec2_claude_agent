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
# Step 2: Create Claude Bot User
# ============================================
echo "=== Creating Claude Bot User ==="

if id "$CLAUDE_USER" &>/dev/null; then
    echo "  User $CLAUDE_USER already exists, skipping creation"
else
    useradd -m -s /bin/bash "$CLAUDE_USER"
    echo "  Created user: $CLAUDE_USER"
fi

# ============================================
# Step 3: Setup Workspace
# ============================================
echo ""
echo "=== Setting Up Workspace ==="

if [ -d "$WORKSPACE" ]; then
    echo "  Workspace $WORKSPACE already exists"
else
    echo "  Creating workspace directory: $WORKSPACE"
    mkdir -p "$WORKSPACE"
fi

# Ensure correct ownership (ignore errors if already owned)
chown -R "$CLAUDE_USER":"$CLAUDE_USER" "/home/$CLAUDE_USER" 2>/dev/null || true
echo "  Verified ownership of /home/$CLAUDE_USER"

# ============================================
# Step 4: Git Configuration
# ============================================
echo ""
echo "=== Setting Up Git Config for Claude User ==="

sudo -u "$CLAUDE_USER" git config --global user.name "Claude Bot" 2>/dev/null || true
sudo -u "$CLAUDE_USER" git config --global user.email "claude-bot@localhost" 2>/dev/null || true
sudo -u "$CLAUDE_USER" git config --global --add safe.directory "$WORKSPACE" 2>/dev/null || true
echo "  Git configured for $CLAUDE_USER"

# ============================================
# Step 5: Check GitHub CLI Auth
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
# Step 6: Check Atlassian CLI Auth
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
