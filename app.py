from flask import Flask, request, abort
import subprocess
import requests
import json
import os
import threading
import hmac
import hashlib
import time
import re

app = Flask(__name__)


def markdown_to_slack(text):
    """Convert Markdown formatting to Slack mrkdwn."""
    # Links: [text](url) -> <url|text>
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<\2|\1>', text)

    # Bold+italic: ***text*** or ___text___ -> *_text_*
    text = re.sub(r'\*{3}(.+?)\*{3}', r'*_\1_*', text)
    text = re.sub(r'_{3}(.+?)_{3}', r'*_\1_*', text)

    # Bold: **text** -> *text*
    text = re.sub(r'\*{2}(.+?)\*{2}', r'*\1*', text)

    # Strikethrough: ~~text~~ -> ~text~
    text = re.sub(r'~~(.+?)~~', r'~\1~', text)

    # Headers: strip # prefix, make bold
    text = re.sub(r'^#{1,6}\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)

    # Code blocks: remove language hints after ```
    text = re.sub(r'```\w+\n', '```\n', text)

    return text

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
GH_TOKEN = os.environ.get("GH_TOKEN")
ATLASSIAN_API_TOKEN = os.environ.get("ATLASSIAN_API_TOKEN")
ATLASSIAN_USER = os.environ.get("ATLASSIAN_USER")
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
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json={
            "channel": channel,
            "thread_ts": thread_ts,
            "text": text
        }
    )
    data = resp.json()
    return data.get("ts")  # message timestamp, used for chat.update

def update_slack_message(channel, message_ts, text):
    """Update an existing Slack message in-place."""
    requests.post(
        "https://slack.com/api/chat.update",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json={
            "channel": channel,
            "ts": message_ts,
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

    if not task or not task.strip():
        with claude_lock:
            claude_process_count -= 1
        return

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

    # Reuse existing worktree if the thread already has one, otherwise
    # just run in the main workspace in read-only mode.  A worktree is
    # only created when the user explicitly requests a branch via !branch.
    thread_info = thread_sessions.get(thread_ts, {})
    worktree_path = thread_info.get("worktree_path")
    has_worktree = bool(worktree_path and os.path.isdir(worktree_path))
    if has_worktree:
        print(f"Reusing existing worktree at {worktree_path} for thread {thread_ts}")
    else:
        worktree_path = WORKSPACE_DIR
        print(f"Using main workspace {worktree_path} (read-only, no worktree) for thread {thread_ts}")

    try:
        cmd = ["sudo"]
        # Pass environment variables through sudo to claude-bot
        for var_name, var_value in [
            ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
            ("GH_TOKEN", GH_TOKEN),
            ("ATLASSIAN_API_TOKEN", ATLASSIAN_API_TOKEN),
            ("ATLASSIAN_USER", ATLASSIAN_USER),
        ]:
            if var_value:
                cmd.append(f"{var_name}={var_value}")
        cmd.extend(["-u", CLAUDE_USER, "claude"])

        # Resume existing session if this thread has one (must come before -p)
        if thread_info.get("session_id"):
            cmd.extend(["--resume", thread_info["session_id"]])

        # Full tools on a worktree branch; read-only on main workspace
        allowed_tools = "Bash,Read,Write,Edit" if has_worktree else "Bash,Read"

        cmd.extend([
                "-p", task,
                "--allowedTools", allowed_tools,
                "--output-format", "stream-json",
                "--verbose",
                "--dangerously-skip-permissions"
        ])

        # Post initial thinking message and get its ts for live updates
        bot_msg_ts = post_to_slack(channel, thread_ts,
                                   ":hourglass_flowing_sand: Thinking...")

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=worktree_path,
            stdin=subprocess.DEVNULL
        )

        session_id = None
        final_result = None
        response_text = ""
        tool_history = []       # list of tool names used so far
        current_status = ""     # what's currently happening
        last_update = 0
        last_display = ""
        UPDATE_INTERVAL = 20.0  # seconds between Slack updates (only sends if changed)

        def build_status_display():
            """Build the current Slack message showing live progress."""
            parts = []
            # Show tool history as a compact trail
            if tool_history:
                trail = " → ".join(tool_history[-5:])  # last 5 tools
                parts.append(f":hammer_and_wrench: {trail}")
            if current_status:
                parts.append(current_status)
            if response_text:
                # Show tail of response so far (Slack max is 40k chars)
                preview = markdown_to_slack(response_text[-3000:])
                if len(response_text) > 3000:
                    preview = "…" + preview
                parts.append(preview)
            if not parts:
                parts.append(":hourglass_flowing_sand: Thinking...")
            return "\n\n".join(parts)

        def maybe_update_slack(force=False):
            """Send a Slack update if content changed and enough time has passed."""
            nonlocal last_update, last_display
            display = build_status_display()
            if display == last_display:
                return
            now = time.time()
            if force or (now - last_update >= UPDATE_INTERVAL):
                update_slack_message(channel, bot_msg_ts, display)
                last_display = display
                last_update = now

        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type")

            if event_type == "system":
                session_id = event.get("session_id", session_id)

            elif event_type == "assistant":
                msg = event.get("message", {})
                for block in msg.get("content", []):
                    block_type = block.get("type")
                    if block_type == "tool_use":
                        tool_name = block.get("name", "tool")
                        tool_input = block.get("input", {})
                        tool_history.append(f"`{tool_name}`")
                        # Show what file/command is being used
                        detail = ""
                        if tool_name == "Read":
                            fp = tool_input.get("file_path", "")
                            detail = f" `{fp.split('/')[-1]}`" if fp else ""
                        elif tool_name == "Bash":
                            cmd_str = tool_input.get("command", "")
                            detail = f" `{cmd_str[:50]}`" if cmd_str else ""
                        elif tool_name in ("Edit", "Write"):
                            fp = tool_input.get("file_path", "")
                            detail = f" `{fp.split('/')[-1]}`" if fp else ""
                        current_status = f":gear: Running `{tool_name}`{detail}..."
                        maybe_update_slack()

                    elif block_type == "thinking":
                        current_status = ":brain: Thinking..."
                        maybe_update_slack()

                    elif block_type == "text":
                        text = block.get("text", "")
                        if text:
                            response_text = text  # full text from this block
                            current_status = ""
                            maybe_update_slack()

            elif event_type == "result":
                session_id = event.get("session_id", session_id)
                final_result = event.get("result", "")

        process.wait()
        stderr_output = process.stderr.read() if process.stderr else ""

        if process.returncode != 0 and not final_result:
            print(f"Claude process failed (rc={process.returncode}): {stderr_output}")
            message = final_result or response_text or stderr_output or "Something went wrong."
        else:
            message = final_result or response_text or "Done, but no output."

        # Store session for conversation continuity
        if session_id:
            current_branch = get_current_branch(worktree_path)
            thread_sessions[thread_ts] = {
                "session_id": session_id,
                "branch": current_branch,
                "worktree_path": worktree_path
            }
            print(f"Stored session {session_id}, branch {current_branch}, "
                  f"worktree {worktree_path} for thread {thread_ts}")

        # Final update with complete response
        message = markdown_to_slack(message)
        print(f"Sending to Slack: {message[:200]}")
        update_slack_message(channel, bot_msg_ts, message)
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

def cleanup_branches():
    """Delete local branches that aren't the default and aren't tied to active sessions."""
    # Get all local branches
    result = subprocess.run(
        ["sudo", "-u", CLAUDE_USER, "git", "branch", "--format=%(refname:short)"],
        capture_output=True, text=True, cwd=WORKSPACE_DIR
    )
    if result.returncode != 0:
        return f"Failed to list branches: {result.stderr.strip()}"

    all_branches = [b.strip() for b in result.stdout.strip().splitlines() if b.strip()]

    # Branches in use by active sessions
    active_branches = {info.get("branch") for info in thread_sessions.values() if info.get("branch")}
    active_branches.add(DEFAULT_BRANCH)

    to_delete = [b for b in all_branches if b not in active_branches]

    if not to_delete:
        return "No stale branches to clean up."

    deleted = []
    failed = []
    for branch in to_delete:
        res = subprocess.run(
            ["sudo", "-u", CLAUDE_USER, "git", "branch", "-D", branch],
            capture_output=True, text=True, cwd=WORKSPACE_DIR
        )
        if res.returncode == 0:
            deleted.append(branch)
        else:
            failed.append(f"`{branch}`: {res.stderr.strip()}")

    lines = []
    if deleted:
        lines.append(f"Deleted {len(deleted)} branch(es): {', '.join(f'`{b}`' for b in deleted)}")
    if failed:
        lines.append(f"Failed to delete {len(failed)}:\n" + "\n".join(failed))
    return "\n".join(lines)

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

    # Ignore bot messages and message edits (avoid loops from chat.update)
    if event.get("bot_id"):
        return "ok"
    if event.get("subtype") in ("message_changed", "message_deleted", "bot_message"):
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
        task = " ".join(text.split()[1:]).strip()

        # Ignore empty mentions (just "@bot" with no text)
        if not task:
            with claude_lock:
                claude_process_count -= 1
            return "ok"

        # Handle built-in commands before spawning Claude
        cmd = task.lower()
        if cmd == "!status":
            with claude_lock:
                claude_process_count -= 1
            post_to_slack(channel, thread_ts, format_status_message())
            return "ok"
        if cmd == "!update":
            with claude_lock:
                claude_process_count -= 1
            post_to_slack(channel, thread_ts, update_main_branch())
            return "ok"
        if cmd.startswith("!branch "):
            with claude_lock:
                claude_process_count -= 1
            branch_name = task.split(None, 1)[1]
            post_to_slack(channel, thread_ts, setup_branch(thread_ts, branch_name))
            return "ok"
        if cmd == "!cleanup-branches":
            with claude_lock:
                claude_process_count -= 1
            post_to_slack(channel, thread_ts, cleanup_branches())
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
        task = text.strip()

        # Ignore empty messages
        if not task:
            with claude_lock:
                claude_process_count -= 1
            return "ok"

        # Handle built-in commands before spawning Claude
        cmd = task.lower()
        if cmd == "!status":
            with claude_lock:
                claude_process_count -= 1
            post_to_slack(channel, thread_ts, format_status_message())
            return "ok"
        if cmd == "!update":
            with claude_lock:
                claude_process_count -= 1
            post_to_slack(channel, thread_ts, update_main_branch())
            return "ok"
        if cmd.startswith("!branch "):
            with claude_lock:
                claude_process_count -= 1
            branch_name = task.split(None, 1)[1]
            post_to_slack(channel, thread_ts, setup_branch(thread_ts, branch_name))
            return "ok"
        if cmd == "!cleanup-branches":
            with claude_lock:
                claude_process_count -= 1
            post_to_slack(channel, thread_ts, cleanup_branches())
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
