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

# Track Claude session IDs, git branches, and worktree paths per Slack thread
# Key: thread_ts, Value: {"session_id": str, "branch": str, "worktree_path": str}
thread_sessions = {}

# Prevent concurrent Claude runs in the same thread (they'd share a worktree)
active_threads = set()
active_threads_lock = threading.Lock()

def get_current_branch(cwd):
    """Get the current git branch in the given directory."""
    result = subprocess.run(
        ["sudo", "-u", CLAUDE_USER, "git", "branch", "--show-current"],
        capture_output=True, text=True, cwd=cwd
    )
    branch = result.stdout.strip() if result.returncode == 0 else ""
    return branch or DEFAULT_BRANCH

def sanitize_thread_ts(thread_ts):
    """Convert a Slack thread_ts into a filesystem-safe directory name."""
    return thread_ts.replace(".", "_")

def ensure_worktree(thread_ts, branch=None):
    """Ensure a git worktree exists for the given Slack thread.

    Returns the absolute path to the worktree directory.
    Raises RuntimeError if worktree creation fails.
    """
    sanitized = sanitize_thread_ts(thread_ts)
    worktree_path = os.path.join(WORKTREES_DIR, sanitized)

    if os.path.isdir(worktree_path):
        return worktree_path

    branch = branch or DEFAULT_BRANCH

    # Create worktree with --detach to avoid "branch already checked out" errors.
    # Git doesn't allow the same branch in multiple worktrees, and main is
    # already checked out in WORKSPACE_DIR. Detached HEAD is safe — Claude
    # can create/switch branches within the worktree freely.
    result = subprocess.run(
        ["sudo", "-u", CLAUDE_USER, "git", "worktree", "add",
         "--detach", worktree_path, branch],
        capture_output=True, text=True, cwd=WORKSPACE_DIR
    )

    if result.returncode != 0:
        # Prune stale worktree metadata and retry
        subprocess.run(
            ["sudo", "-u", CLAUDE_USER, "git", "worktree", "prune"],
            capture_output=True, text=True, cwd=WORKSPACE_DIR
        )
        result = subprocess.run(
            ["sudo", "-u", CLAUDE_USER, "git", "worktree", "add",
             "--detach", worktree_path, branch],
            capture_output=True, text=True, cwd=WORKSPACE_DIR
        )

    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to create worktree for thread {thread_ts} "
            f"on branch {branch}: {result.stderr}"
        )

    print(f"Created worktree at {worktree_path} (branch: {branch})")
    return worktree_path

def remove_worktree(thread_ts):
    """Remove the worktree associated with a Slack thread."""
    sanitized = sanitize_thread_ts(thread_ts)
    worktree_path = os.path.join(WORKTREES_DIR, sanitized)

    if not os.path.isdir(worktree_path):
        return True

    result = subprocess.run(
        ["sudo", "-u", CLAUDE_USER, "git", "worktree", "remove", "--force",
         worktree_path],
        capture_output=True, text=True, cwd=WORKSPACE_DIR
    )

    if result.returncode != 0:
        print(f"Failed to remove worktree {worktree_path}: {result.stderr}")
        return False
    return True

def cleanup_stale_worktrees(max_age_hours=24):
    """Remove worktrees not in thread_sessions and older than max_age_hours."""
    if not os.path.isdir(WORKTREES_DIR):
        return

    cutoff = time.time() - (max_age_hours * 3600)

    for entry in os.listdir(WORKTREES_DIR):
        entry_path = os.path.join(WORKTREES_DIR, entry)
        if not os.path.isdir(entry_path):
            continue

        # Reconstruct thread_ts from directory name
        thread_ts = entry.replace("_", ".", 1)

        if thread_ts in thread_sessions:
            continue

        try:
            mtime = os.path.getmtime(entry_path)
            if mtime > cutoff:
                continue
        except OSError:
            continue

        print(f"Cleaning up stale worktree: {entry_path}")
        remove_worktree(thread_ts)

    subprocess.run(
        ["sudo", "-u", CLAUDE_USER, "git", "worktree", "prune"],
        capture_output=True, text=True, cwd=WORKSPACE_DIR
    )

def cleanup_all_worktrees():
    """Remove all worktrees. Called on startup since thread_sessions is in-memory."""
    if not os.path.isdir(WORKTREES_DIR):
        os.makedirs(WORKTREES_DIR, exist_ok=True)
        return

    for entry in os.listdir(WORKTREES_DIR):
        entry_path = os.path.join(WORKTREES_DIR, entry)
        if os.path.isdir(entry_path):
            subprocess.run(
                ["sudo", "-u", CLAUDE_USER, "git", "worktree", "remove",
                 "--force", entry_path],
                capture_output=True, text=True, cwd=WORKSPACE_DIR
            )

    subprocess.run(
        ["sudo", "-u", CLAUDE_USER, "git", "worktree", "prune"],
        capture_output=True, text=True, cwd=WORKSPACE_DIR
    )
    print("Cleaned up all worktrees from previous run")

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
WORKSPACE_DIR = os.environ.get("WORKSPACE_DIR", "/home/claude-bot/workspace")
WORKTREES_DIR = os.environ.get("WORKTREES_DIR", "/home/claude-bot/worktrees")
DEFAULT_BRANCH = "main"
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

    # Prevent concurrent runs in the same thread (they'd share a worktree)
    with active_threads_lock:
        if thread_ts in active_threads:
            post_to_slack(channel, thread_ts,
                "I'm still working on the previous request in this thread. "
                "Please wait for me to finish.")
            with claude_lock:
                claude_process_count -= 1
            return
        active_threads.add(thread_ts)

    add_reaction(channel, message_ts, "thumbsup")

    print(f"Running task: {task}")
    print(f"Current thread_sessions: {json.dumps(thread_sessions, indent=2)}")

    # Ensure a worktree exists for this thread
    thread_info = thread_sessions.get(thread_ts, {})
    branch = thread_info.get("branch")

    try:
        worktree_path = thread_info.get("worktree_path")
        if not worktree_path or not os.path.isdir(worktree_path):
            worktree_path = ensure_worktree(thread_ts, branch)
            print(f"Created/verified worktree at {worktree_path} for thread {thread_ts}")
        else:
            print(f"Reusing existing worktree at {worktree_path} for thread {thread_ts}")
    except RuntimeError as e:
        print(f"Worktree creation failed: {e}")
        post_to_slack(channel, thread_ts, f"Failed to set up workspace: {e}")
        with active_threads_lock:
            active_threads.discard(thread_ts)
        with claude_lock:
            claude_process_count -= 1
        return

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
            cwd=worktree_path,
            stdin=subprocess.DEVNULL
        )

        print(f"stdout: {result.stdout[:500]}")
        print(f"stderr: {result.stderr}")

        try:
            output = json.loads(result.stdout)
            message = output.get("result", "Done, but no output.")
            # Store session ID, branch, and worktree path for conversation continuity
            session_id = output.get("session_id")
            current_branch = get_current_branch(worktree_path)
            if session_id:
                thread_sessions[thread_ts] = {
                    "session_id": session_id,
                    "branch": current_branch,
                    "worktree_path": worktree_path
                }
                print(f"Stored session {session_id}, branch {current_branch}, "
                      f"worktree {worktree_path} for thread {thread_ts}")
        except Exception as e:
            print(f"Parse error: {e}")
            message = result.stdout or result.stderr or "Something went wrong."

        print(f"Sending to Slack: {message[:200]}")
        post_to_slack(channel, thread_ts, message)
    finally:
        with active_threads_lock:
            active_threads.discard(thread_ts)
        with claude_lock:
            claude_process_count -= 1

def update_main_branch():
    """Checkout and pull the main branch in the base workspace."""
    checkout = subprocess.run(
        ["sudo", "-u", CLAUDE_USER, "git", "checkout", DEFAULT_BRANCH],
        capture_output=True, text=True, cwd=WORKSPACE_DIR
    )
    if checkout.returncode != 0:
        return f"Failed to checkout `{DEFAULT_BRANCH}`: {checkout.stderr.strip()}"

    pull = subprocess.run(
        ["sudo", "-u", CLAUDE_USER, "git", "pull"],
        capture_output=True, text=True, cwd=WORKSPACE_DIR
    )
    if pull.returncode != 0:
        return f"Checked out `{DEFAULT_BRANCH}` but pull failed: {pull.stderr.strip()}"

    return f"Updated `{DEFAULT_BRANCH}`:\n```\n{pull.stdout.strip()}\n```"

def setup_branch(thread_ts, branch):
    """Set up a thread's worktree on the given branch.

    If the thread already has a worktree, removes it first.
    If the branch doesn't exist, creates it from the default branch.
    """
    # Remove existing worktree for this thread if any
    thread_info = thread_sessions.get(thread_ts, {})
    if thread_info.get("worktree_path") and os.path.isdir(thread_info["worktree_path"]):
        remove_worktree(thread_ts)

    # Check if the branch exists
    check = subprocess.run(
        ["sudo", "-u", CLAUDE_USER, "git", "rev-parse", "--verify", branch],
        capture_output=True, text=True, cwd=WORKSPACE_DIR
    )
    branch_exists = check.returncode == 0

    if not branch_exists:
        # Also check remote
        check_remote = subprocess.run(
            ["sudo", "-u", CLAUDE_USER, "git", "rev-parse", "--verify", f"origin/{branch}"],
            capture_output=True, text=True, cwd=WORKSPACE_DIR
        )
        branch_exists = check_remote.returncode == 0

    try:
        worktree_path = ensure_worktree(thread_ts, branch if branch_exists else DEFAULT_BRANCH)
    except RuntimeError as e:
        return f"Failed to create worktree: {e}"

    # If the branch didn't exist, create it inside the worktree
    if not branch_exists:
        create = subprocess.run(
            ["sudo", "-u", CLAUDE_USER, "git", "checkout", "-b", branch],
            capture_output=True, text=True, cwd=worktree_path
        )
        if create.returncode != 0:
            return f"Worktree created but failed to create branch `{branch}`: {create.stderr.strip()}"

    # Update thread_sessions
    thread_sessions[thread_ts] = {
        "session_id": thread_info.get("session_id", ""),
        "branch": branch,
        "worktree_path": worktree_path
    }

    if branch_exists:
        return f"Worktree ready on existing branch `{branch}`"
    else:
        return f"Created new branch `{branch}` (from `{DEFAULT_BRANCH}`)"

def format_status_message():
    """Format current status as a Slack-friendly message."""
    with claude_lock:
        running = claude_process_count

    with active_threads_lock:
        active = list(active_threads)

    lines = [f"*Claude Processes:* {running}/{MAX_CLAUDE_PROCESSES}"]

    if not thread_sessions:
        lines.append("No active threads.")
    else:
        lines.append(f"*Threads:* {len(thread_sessions)}")
        for thread_ts, info in thread_sessions.items():
            is_active = thread_ts in active_threads
            status_icon = ":large_green_circle:" if is_active else ":white_circle:"
            branch = info.get("branch", "unknown")
            lines.append(f"  {status_icon} `{thread_ts}` — branch: `{branch}`")

    return "\n".join(lines)

@app.route("/status")
def status():
    """Return status of all active threads, worktrees, and Claude processes."""
    with claude_lock:
        running = claude_process_count

    with active_threads_lock:
        active = list(active_threads)

    sessions = {}
    for thread_ts, info in thread_sessions.items():
        worktree_path = info.get("worktree_path", "")
        sessions[thread_ts] = {
            "session_id": info.get("session_id", ""),
            "branch": info.get("branch", ""),
            "worktree_path": worktree_path,
            "worktree_exists": os.path.isdir(worktree_path) if worktree_path else False,
            "active": thread_ts in active_threads,
        }

    return {
        "claude_processes_running": running,
        "max_claude_processes": MAX_CLAUDE_PROCESSES,
        "active_threads": active,
        "threads": sessions,
    }

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

        # Handle built-in commands before spawning Claude
        cmd = task.strip().lower()
        if cmd == "!status":
            post_to_slack(channel, thread_ts, format_status_message())
            return "ok"
        if cmd == "!update":
            post_to_slack(channel, thread_ts, update_main_branch())
            return "ok"
        if cmd.startswith("!branch "):
            branch_name = task.strip().split(None, 1)[1]
            post_to_slack(channel, thread_ts, setup_branch(thread_ts, branch_name))
            return "ok"

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

        # Handle built-in commands before spawning Claude
        cmd = task.strip().lower()
        if cmd == "!status":
            post_to_slack(channel, thread_ts, format_status_message())
            return "ok"
        if cmd == "!update":
            post_to_slack(channel, thread_ts, update_main_branch())
            return "ok"
        if cmd.startswith("!branch "):
            branch_name = task.strip().split(None, 1)[1]
            post_to_slack(channel, thread_ts, setup_branch(thread_ts, branch_name))
            return "ok"

        threading.Thread(
            target=run_claude,
            args=(task, channel, thread_ts, message_ts)
        ).start()
        return "ok"

    return "ok"

def start_cleanup_timer(interval_hours=6):
    """Start a repeating background timer for worktree cleanup."""
    def _cleanup():
        while True:
            time.sleep(interval_hours * 3600)
            try:
                cleanup_stale_worktrees(max_age_hours=24)
            except Exception as e:
                print(f"Cleanup error: {e}")

    t = threading.Thread(target=_cleanup, daemon=True)
    t.start()

if __name__ == "__main__":
    cleanup_all_worktrees()
    start_cleanup_timer()
    app.run(host="0.0.0.0", port=80)
