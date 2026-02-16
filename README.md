# AWS Coding Agent

A Slack bot that runs Claude Code on an EC2 instance. Mention the bot in Slack or send it a DM, and it will execute coding tasks in your workspace and respond with results.

## Architecture

```
Slack → Cloudflare (HTTPS) → EC2 (Flask) → Claude CLI → Workspace
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SLACK_BOT_TOKEN` | Yes | Bot token from Slack App (starts with `xoxb-`) |
| `SLACK_SIGNING_SECRET` | Yes | Signing secret from Slack App settings |
| `ANTHROPIC_API_KEY` | Yes | API key from Anthropic (starts with `sk-ant-`) |
| `WORKSPACE_DIR` | No | Path to workspace directory (default: `/home/claude-bot/workspace`) |

Set these when running the app:
```bash
export SLACK_BOT_TOKEN="xoxb-your-token"
export SLACK_SIGNING_SECRET="your-signing-secret"
export ANTHROPIC_API_KEY="sk-ant-your-key"
export WORKSPACE_DIR="/home/claude-bot/your-repo"  # optional
```

## Setup

### 1. EC2 Instance

Launch an Ubuntu EC2 instance and SSH in:
```bash
ssh -i your-key.pem ubuntu@<EC2-IP>
```

### 2. Install Dependencies

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install essentials
sudo apt install -y vim

# Install Python and pip
sudo apt install -y python3 python3-pip

# Install Flask
pip3 install flask requests

# Install Docker
sudo apt install -y docker.io docker-compose
sudo usermod -aG docker ubuntu
newgrp docker

# Install Node.js
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs

# Install Claude Code
sudo npm install -g @anthropic-ai/claude-code

# Install GitHub CLI
sudo apt install -y gh
```

### 3. Install Git

```bash
sudo apt install -y git

# Configure git
git config --global user.name "Your Name"
git config --global user.email "your@email.com"

# Generate SSH key for GitHub
ssh-keygen -t ed25519 -C "your@email.com"
cat ~/.ssh/id_ed25519.pub
# Add this key to GitHub: Settings → SSH and GPG keys → New SSH key

# Test connection
ssh -T git@github.com
```

### 4. Install Atlassian CLI (acli)

Install the Go-based Atlassian CLI:

```bash
# Download latest release (check https://github.com/atlassian/acli/releases for current version)
curl -LO https://github.com/atlassian/acli/releases/latest/download/acli_linux_amd64.tar.gz
tar -xzf acli_linux_amd64.tar.gz
sudo mv acli /usr/local/bin/
rm acli_linux_amd64.tar.gz

# Authenticate (interactive - will prompt for server, email, token)
acli jira auth

# You'll need an API token from: https://id.atlassian.com/manage-profile/security/api-tokens

# Test connection
acli jira project list
```

Common commands:
```bash
acli jira workitem get TICKET-123           # Get issue
acli jira workitem list --project CORE      # List issues
acli jira workitem create --project CORE --type Task --summary "Title"
acli jira workitem comment TICKET-123 --body "Comment"
acli jira board list                        # List boards
acli jira sprint list --board 123           # List sprints
```

### 5. Clone Your Workspace

```bash
# Set your workspace path (or use the default)
export WORKSPACE_DIR="/home/claude-bot/workspace"

mkdir -p $WORKSPACE_DIR
cd $WORKSPACE_DIR
sudo -u claude-bot git clone git@github.com:your-org/your-repo.git .
```

### 6. Set Up Restricted User (Recommended)

Run the setup script for better security:
```bash
sudo bash setup.sh
```

### 7. Configure Claude

Copy the local instructions file to your workspace:
```bash
cp CLAUDE.local.md $WORKSPACE_DIR/

# Add to .gitignore so it's not committed
echo "CLAUDE.local.md" >> $WORKSPACE_DIR/.gitignore
```

Edit it to update the placeholder URLs for your GitHub org and Atlassian domain.

### 8. Configure Environment Variables

Add these to `~/.bashrc`:
```bash
export SLACK_SIGNING_SECRET="your-signing-secret"
export SLACK_BOT_TOKEN="xoxb-your-bot-token"
export ANTHROPIC_API_KEY="your-anthropic-key"
export GH_TOKEN="github_pat_your-token"
export ATLASSIAN_API_TOKEN="your-atlassian-token"
export ATLASSIAN_USER="your@email.com"
```

Then reload:
```bash
source ~/.bashrc
```

### 9. Authenticate GitHub CLI

```bash
gh auth login
# Follow prompts, or use token:
gh auth login --with-token <<< "$GH_TOKEN"
```

### 10. Run the App with tmux

Use tmux to keep the app running after disconnecting:

```bash
# Start a new tmux session
tmux new -s claude-bot

# Run the app (uses env vars from .bashrc)
sudo -E python3 app.py

# Detach from session: Ctrl+B, then D
```

Reconnect later:
```bash
# List sessions
tmux ls

# Attach to session
tmux attach -t claude-bot

# Kill session when done
tmux kill-session -t claude-bot
```

## Slack App Setup

1. Create app at [api.slack.com/apps](https://api.slack.com/apps)
2. Enable **Event Subscriptions**:
   - Request URL: `https://yourdomain.com/slack/events`
   - Subscribe to: `app_mention`, `message.im`
3. Add **Bot Token Scopes**:
   - `app_mentions:read`
   - `chat:write`
   - `im:history`
   - `im:read`
   - `reactions:write`
4. Install to workspace and copy the Bot Token

## Cloudflare Setup

1. Add A record pointing to EC2 public IP (or Elastic IP)
2. Enable proxy (orange cloud)
3. SSL/TLS → Edge Certificates → Enable "Always Use HTTPS"

## How Conversations Work

Claude conversations are linked to Slack threads using a session tracking system:

```
Slack Thread (thread_ts) → Claude Session ID + Git Branch
```

**How it works:**

1. **First message in a thread**: When you mention the bot, it starts a new Claude session. After Claude responds, the app stores:
   - The Claude `session_id` (for conversation memory)
   - The current git branch (for workspace continuity)

2. **Follow-up messages in the same thread**: When you reply in the same Slack thread, the app:
   - Looks up the stored session using `thread_ts` as the key
   - Checks out the git branch that was active during the last message
   - Resumes the Claude session with `--resume <session_id>`, preserving full conversation context

3. **New threads = new sessions**: Starting a new thread (or mentioning the bot outside a thread) creates a fresh Claude session with no memory of previous conversations.

**Practical implications:**
- Keep related requests in the same thread for continuity (e.g., "create a branch" → "make changes" → "commit")
- Start a new thread when you want a clean slate
- The bot tracks up to 5 concurrent conversations

## Security Notes

- The app verifies Slack request signatures
- HTTPS is enforced via Cloudflare header check
- Use the restricted `claude-bot` user for OS-level isolation
- Claude instructions in `CLAUDE.local.md` provide soft guardrails

## Files

| File | Description |
|------|-------------|
| `app.py` | Flask app handling Slack events |
| `CLAUDE.local.md` | Instructions and restrictions for Claude |
| `setup.sh` | Script to create restricted Linux user and configure workspace |
