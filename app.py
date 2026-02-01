from flask import Flask, request, abort
import subprocess
import requests
import json
import os
import threading
import hmac
import hashlib
import time

app = Flask(__name__)

@app.before_request
def require_https():
    """Reject requests that didn't come through HTTPS via Cloudflare."""
    if request.headers.get('X-Forwarded-Proto', 'http') != 'https':
        abort(403, "HTTPS required")

# Track number of running Claude processes
claude_lock = threading.Lock()
claude_process_count = 0
MAX_CLAUDE_PROCESSES = 5

# Track Claude session IDs and git branches per Slack thread
# Key: thread_ts, Value: {"session_id": str, "branch": str}
thread_sessions = {}

def get_current_branch():
    """Get the current git branch in the workspace."""
    result = subprocess.run(
        ["sudo", "-u", CLAUDE_USER, "git", "branch", "--show-current"],
        capture_output=True, text=True, cwd=WORKSPACE_DIR
    )
    return result.stdout.strip() if result.returncode == 0 else "main"

def checkout_branch(branch):
    """Checkout a git branch in the workspace."""
    result = subprocess.run(
        ["sudo", "-u", CLAUDE_USER, "git", "checkout", branch],
        capture_output=True, text=True, cwd=WORKSPACE_DIR
    )
    if result.returncode != 0:
        print(f"Failed to checkout {branch}: {result.stderr}")
    return result.returncode == 0

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
WORKSPACE_DIR = os.environ.get("WORKSPACE_DIR", "/home/claude-bot/workspace")
CLAUDE_USER = "claude-bot"

def verify_slack_request():
    """Verify the request came from Slack using the signing secret."""
    if not SLACK_SIGNING_SECRET:
        print("ERROR: SLACK_SIGNING_SECRET not set - rejecting request")
        return False

    # Get the timestamp and signature from headers
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    slack_signature = request.headers.get("X-Slack-Signature", "")

    if not timestamp or not slack_signature:
        return False

    # Prevent replay attacks - reject requests older than 5 minutes
    if abs(time.time() - int(timestamp)) > 60 * 5:
        return False

    # Compute the signature
    sig_basestring = f"v0:{timestamp}:{request.get_data(as_text=True)}"
    computed_signature = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(),
        sig_basestring.encode(),
        hashlib.sha256
    ).hexdigest()

    # Compare signatures using constant-time comparison to prevent timing attacks
    return hmac.compare_digest(computed_signature, slack_signature)

def post_to_slack(channel, thread_ts, text):
    requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json={
            "channel": channel,
            "thread_ts": thread_ts,
            "text": text
        }
    )

def add_reaction(channel, timestamp, emoji):
    """Add an emoji reaction to a message."""
    requests.post(
        "https://slack.com/api/reactions.add",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json={
            "channel": channel,
            "timestamp": timestamp,
            "name": emoji
        }
    )

def run_claude(task, channel, thread_ts, message_ts):
    global claude_process_count

    add_reaction(channel, message_ts, "thumbsup")

    print(f"Running task: {task}")
    print(f"Current thread_sessions: {json.dumps(thread_sessions, indent=2)}")

    # Checkout the branch for this thread (if any)
    thread_info = thread_sessions.get(thread_ts, {})
    if thread_info.get("branch"):
        print(f"Checking out branch {thread_info['branch']} for thread {thread_ts}")
        checkout_branch(thread_info["branch"])

    try:
        # Build environment for Claude subprocess
        claude_env = os.environ.copy()
        if ANTHROPIC_API_KEY:
            claude_env["ANTHROPIC_API_KEY"] = ANTHROPIC_API_KEY

        cmd = [
                "sudo",
            ]
        if ANTHROPIC_API_KEY:
            cmd.append(f"ANTHROPIC_API_KEY={ANTHROPIC_API_KEY}")
        cmd.extend(["-u", CLAUDE_USER, "claude"])

        # Resume existing session if this thread has one (must come before -p)
        if thread_info.get("session_id"):
            cmd.extend(["--resume", thread_info["session_id"]])

        cmd.extend([
                "-p", task,
                "--allowedTools", "Bash,Read,Write,Edit",
                "--output-format", "json",
                "--dangerously-skip-permissions"
        ])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=WORKSPACE_DIR,
            stdin=subprocess.DEVNULL
        )

        print(f"stdout: {result.stdout[:500]}")  # Add this
        print(f"stderr: {result.stderr}")  # Add this

        try:
            output = json.loads(result.stdout)
            message = output.get("result", "Done, but no output.")
            # Store session ID and current branch for conversation continuity
            session_id = output.get("session_id")
            current_branch = get_current_branch()
            if session_id:
                thread_sessions[thread_ts] = {
                    "session_id": session_id,
                    "branch": current_branch
                }
                print(f"Stored session {session_id}, branch {current_branch} for thread {thread_ts}")
        except Exception as e:
            print(f"Parse error: {e}")  # Add this
            message = result.stdout or result.stderr or "Something went wrong."

        print(f"Sending to Slack: {message[:200]}")  # Add this
        post_to_slack(channel, thread_ts, message)
    finally:
        with claude_lock:
            claude_process_count -= 1

@app.route("/slack/events", methods=["POST"])
def slack_events():
    # Verify the request is from Slack
    if not verify_slack_request():
        abort(401, "Invalid request signature")

    data = request.json

    # Slack URL verification challenge
    if data.get("type") == "url_verification":
        return data["challenge"]

    event = data.get("event", {})

    # Ignore bot messages (avoid loops)
    if event.get("bot_id"):
        return "ok"

    global claude_process_count
    if event.get("type") == "app_mention":

        text = event.get("text", "")
        channel = event["channel"]
        message_ts = event["ts"]
        thread_ts = event.get("thread_ts", message_ts)

        # Check if we've hit the max concurrent processes
        with claude_lock:
            if claude_process_count >= MAX_CLAUDE_PROCESSES:
                post_to_slack(channel, thread_ts, "Busy right now boss")
                return "ok"
            claude_process_count += 1

        # Strip the @mention
        task = " ".join(text.split()[1:])

        # Run in background so we respond to Slack quickly
        threading.Thread(
            target=run_claude,
            args=(task, channel, thread_ts, message_ts)
        ).start()

        return "ok"

    # Handle direct messages
    if event.get("type") == "message" and event.get("channel_type") == "im":
        text = event.get("text", "")
        channel = event["channel"]
        message_ts = event["ts"]
        thread_ts = event.get("thread_ts", message_ts)

        with claude_lock:
            if claude_process_count >= MAX_CLAUDE_PROCESSES:
                post_to_slack(channel, thread_ts, "Busy right now boss")
                return "ok"
            claude_process_count += 1

        # No need to strip @mention in DMs
        task = text

        threading.Thread(
            target=run_claude,
            args=(task, channel, thread_ts, message_ts)
        ).start()
        return "ok"

    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80)
