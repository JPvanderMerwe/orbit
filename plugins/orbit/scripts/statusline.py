#!/usr/bin/env python3
"""Claude Code status line - forwards plan usage and cost to Orbit, then prints a short status line.

Claude Code pipes the status line data as JSON on stdin. This is the only place it shares
Pro and Max plan usage (the 5-hour and weekly limits), so Orbit reads it here.

If you already had a status line, `orbit install-statusline` saves it and this script runs it
with the same input and prints its output instead of Orbit's own line. Never fails loudly.
"""
import json
import os
import shutil
import subprocess
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook import DATA_DIR, KEY_HEADER, URL, access_key  # noqa: E402

NEXT_FILE = DATA_DIR / "statusline-next.json"
KEEP = ("session_id", "transcript_path", "cwd", "model", "cost", "context_window", "rate_limits", "fast_mode", "version")


def forward(data):
    payload = {k: data[k] for k in KEEP if k in data}
    request = urllib.request.Request(
        f"{URL}/statusline", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", KEY_HEADER: access_key()}, method="POST",
    )
    urllib.request.urlopen(request, timeout=0.5).read()


def own_line(data):
    parts = ["◉ Orbit"]
    limits = data.get("rate_limits") or {}
    for key, name in (("five_hour", "5h"), ("seven_day", "week")):
        pct = (limits.get(key) or {}).get("used_percentage")
        if isinstance(pct, (int, float)):
            parts.append(f"{name} {pct:.0f}%")
    cost = (data.get("cost") or {}).get("total_cost_usd")
    if isinstance(cost, (int, float)):
        parts.append(f"${cost:.2f}")
    return " · ".join(parts)


def previous_line(command, raw):
    """Run the status line the user had before, in the shell Claude Code would have used (Git Bash on Windows)."""
    run = {"shell": True}
    if os.name == "nt":
        git = shutil.which("git")
        bash = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH") or (git and os.path.join(os.path.dirname(os.path.dirname(git)), "bin", "bash.exe"))
        if bash and os.path.isfile(bash):
            run = {"args": [bash, "-c", command]}
    run.setdefault("args", command)
    return subprocess.run(input=raw, capture_output=True, timeout=5, **run).stdout


def main():
    raw = sys.stdin.buffer.read()  # bytes: Claude Code sends UTF-8 whatever the system's code page
    try:
        data = json.loads(raw)
    except ValueError:
        data = {}
    if isinstance(data, dict):
        try:
            forward(data)
        except Exception:
            pass
    try:
        nxt = json.loads(NEXT_FILE.read_text(encoding="utf-8")).get("command")
    except (OSError, ValueError, AttributeError):
        nxt = None
    if nxt:
        try:
            sys.stdout.buffer.write(previous_line(nxt, raw))
            return
        except Exception:
            pass
    sys.stdout.buffer.write((own_line(data if isinstance(data, dict) else {}) + "\n").encode("utf-8"))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
