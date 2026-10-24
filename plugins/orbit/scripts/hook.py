#!/usr/bin/env python3
"""Claude Code hook - forwards every hook event to the local Orbit server.

Claude Code pipes the hook event as JSON on stdin. This script posts it to
the Orbit server and exits. If the server is not running it starts it in
the background (at most once every 10 seconds) and retries once.

It never blocks or breaks Claude: every failure is silent and it always
exits with code 0, normally within milliseconds and at most a few seconds.
"""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

APP = "orbit"
PORT = int(os.environ.get("ORBIT_PORT") or os.environ.get("CLAUDE_VIZ_PORT") or "7777")
SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("ORBIT_DATA") or os.environ.get("CLAUDE_VIZ_DATA") or Path.home() / ".orbit")
# Where Orbit kept its data when it was called Activity Viewer (moved across once, automatically)
OLD_DATA_DIR = Path.home() / ".claude-activity-viewer"
SPAWN_STAMP = DATA_DIR / ".last-start"
TOKEN_FILE = DATA_DIR / "token"
MAX_TEXT = 20000
URL = f"http://127.0.0.1:{PORT}"
KEY_HEADER = "X-Orbit-Key"
# Markers Claude Code adds to processes it starts. The Orbit server (and the Claude sessions it
# launches) must not inherit them: they would make a launched Claude act as a child session
# (no transcript saving), leak the parent session's messaging token, and set GIT_EDITOR=true.
SESSION_MARKERS = (
    "AI_AGENT", "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_EFFORT", "CLAUDE_PID", "GIT_EDITOR", "COREPACK_ENABLE_AUTO_PIN",
    "CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA", "CLAUDE_ENV_FILE", "ORBIT_TERM_ID", "VIZ_TERM_ID",
)


def clean_env():
    env = dict(os.environ)
    for key in SESSION_MARKERS:
        env.pop(key, None)
    return env


def migrate_old_data():
    """Move ~/.claude-activity-viewer to ~/.orbit once, stopping the old server first."""
    if DATA_DIR.exists() or not OLD_DATA_DIR.is_dir() or os.environ.get("ORBIT_DATA") or os.environ.get("CLAUDE_VIZ_DATA"):
        return
    try:
        os.kill(int((OLD_DATA_DIR / "server.pid").read_text()), signal.SIGTERM)
        time.sleep(0.3)
    except (OSError, ValueError):
        pass
    try:
        OLD_DATA_DIR.rename(DATA_DIR)
    except OSError:
        pass


def trim(value):
    """Shorten very large values (e.g. huge file reads) so the viewer stays fast."""
    if isinstance(value, str) and len(value) > MAX_TEXT:
        return value[:MAX_TEXT] + f"\n... [trimmed {len(value) - MAX_TEXT} characters]"
    if isinstance(value, dict):
        return {k: trim(v) for k, v in value.items()}
    if isinstance(value, list):
        return [trim(v) for v in value]
    return value


def server_up(timeout=0.3):
    try:
        with urllib.request.urlopen(f"{URL}/health", timeout=timeout) as r:
            return json.loads(r.read()).get("app") == APP
    except Exception:
        return False


def start_server(force=False):
    """Launch server.py detached from this process. Throttled unless force=True."""
    migrate_old_data()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not force and SPAWN_STAMP.exists() and time.time() - SPAWN_STAMP.stat().st_mtime < 10:
        return False
    SPAWN_STAMP.touch()
    log = open(DATA_DIR / "server.log", "ab")
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log, "close_fds": True, "env": clean_env()}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, str(SCRIPT_DIR / "server.py")], **kwargs)
    return True


def access_key():
    try:
        return TOKEN_FILE.read_text().strip()
    except OSError:
        return ""


def post(payload):
    request = urllib.request.Request(
        f"{URL}/event",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", KEY_HEADER: access_key()},
        method="POST",
    )
    # Generous: a briefly busy server must not cost an event (the hook itself is allowed 5 seconds)
    urllib.request.urlopen(request, timeout=3).read()


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    migrate_old_data()
    # SessionStart carries its own "source" (startup/resume/clear); keep it separately
    if "source" in data:
        data["source_type"] = data.pop("source")
    payload = {**trim(data), "source": "claude-code"}
    # Set when this Claude session runs inside a workspace terminal; links the two
    if os.environ.get("ORBIT_TERM_ID"):
        payload["orbit_term_id"] = os.environ["ORBIT_TERM_ID"]
    try:
        post(payload)
        return
    except urllib.error.URLError as e:
        if not isinstance(getattr(e, "reason", None), ConnectionRefusedError):
            return
    except Exception:
        return
    # Server not running: start it and retry once
    if start_server():
        for _ in range(8):
            time.sleep(0.1)
            if server_up(0.1):
                try:
                    post(payload)
                except Exception:
                    pass
                return


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
