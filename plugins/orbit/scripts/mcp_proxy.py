#!/usr/bin/env python3
"""Records every tool Claude Desktop uses on this computer, for Orbit.

Claude Desktop has no hooks, but everything it does on this computer goes through MCP servers. `orbit
install-desktop` puts this script in front of each MCP server in Claude Desktop's settings:

    python mcp_proxy.py --orbit-desktop <server name> -- <original command> <original args...>

It starts the original server and passes every message both ways unchanged. Each tool call and its result
also go to Orbit, so they appear live in the "Claude Desktop" session like Claude Code's own steps. If
Orbit is not running it is started; if it cannot be reached the server simply works as before.
Standard library only.
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook import KEY_HEADER, URL, access_key, newest_install, server_up, start_server, trim  # noqa: E402

MARKER = "--orbit-desktop"
SOURCE = "claude-desktop"
# install-desktop writes where Orbit's own scripts are, so this copy (kept in ~/.orbit/bin) can start the newest Orbit
SCRIPTS_FILE = Path(os.path.dirname(os.path.abspath(__file__))) / "scripts.json"

outbox = queue.Queue(maxsize=1000)
pending = {}          # JSON-RPC id of a tools/call -> its PreToolUse event
pending_lock = threading.Lock()


def orbit_scripts():
    """Orbit's newest installed scripts folder, or None."""
    try:
        folder = Path(json.loads(SCRIPTS_FILE.read_text(encoding="utf-8"))["scripts"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    found = newest_install(folder)[1]
    return found if (found / "server.py").is_file() else None


def post(event):
    request = urllib.request.Request(f"{URL}/event", data=json.dumps(event).encode(), method="POST",
                                     headers={"Content-Type": "application/json", KEY_HEADER: access_key()})
    urllib.request.urlopen(request, timeout=3).read()


def wait_for_orbit():
    for _ in range(50):
        if server_up(0.2):
            return
        time.sleep(0.1)


def poster():
    """Sends events to Orbit in order, off the message path so a slow Orbit never slows Claude Desktop."""
    # Claude Desktop starts its MCP servers when it opens, so Orbit is started then, before any tool is used
    scripts = None if server_up(0.5) else orbit_scripts()
    if scripts:
        start_server(scripts=scripts)  # skipped if another relay started it moments ago; wait for it either way
        wait_for_orbit()
    while True:
        event = outbox.get()
        for attempt in range(2):
            try:
                post(event)
                break
            except urllib.error.URLError as e:
                refused = isinstance(getattr(e, "reason", None), ConnectionRefusedError)
                scripts = orbit_scripts() if refused and attempt == 0 else None
                if not scripts:
                    break
                start_server(scripts=scripts)
                wait_for_orbit()
            except Exception:
                break
        outbox.task_done()


def emit(event):
    try:
        outbox.put_nowait(trim({"source": SOURCE, **event}))
    except queue.Full:
        pass


def messages(line):
    try:
        msg = json.loads(line)
    except ValueError:
        return []
    return [m for m in (msg if isinstance(msg, list) else [msg]) if isinstance(m, dict)]


def from_desktop(line, server):
    for m in messages(line):
        if m.get("method") == "tools/call" and "id" in m:
            params = m.get("params") if isinstance(m.get("params"), dict) else {}
            event = {"hook_event_name": "PreToolUse", "tool_name": f"mcp__{server}__{params.get('name', '')}",
                     "tool_input": params.get("arguments") or {}, "tool_use_id": f"desktop-{os.getpid()}-{m['id']}",
                     "mcp_server": server}
            with pending_lock:
                pending[json.dumps(m["id"])] = event
            emit(event)


def from_server(line):
    for m in messages(line):
        if "method" in m or "id" not in m:
            continue  # a request or notification from the server, not an answer to a tool call
        with pending_lock:
            event = pending.pop(json.dumps(m["id"]), None)
        if not event:
            continue
        result = m.get("result") if isinstance(m.get("result"), dict) else {}
        if "error" in m or result.get("isError"):
            error = (m.get("error") or {}).get("message") if isinstance(m.get("error"), dict) else None
            text = error or " ".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
            emit({**event, "hook_event_name": "PostToolUseFailure", "error": text or "The tool reported an error"})
        else:
            emit({**event, "hook_event_name": "PostToolUse", "tool_response": result.get("structuredContent") or result.get("content", result)})


def pump(src, dst, watch, close_dst=False):
    for line in iter(src.readline, b""):
        try:
            dst.write(line)
            dst.flush()
        except OSError:
            break
        try:
            watch(line)
        except Exception:  # noqa: BLE001 - recording must never break the server
            pass
    if close_dst:
        try:
            dst.close()
        except OSError:
            pass


def main():
    args = sys.argv[1:]
    if len(args) < 4 or args[0] != MARKER or args[2] != "--":
        print("usage: mcp_proxy.py --orbit-desktop <server name> -- <command> [args...]", file=sys.stderr)
        sys.exit(2)
    server, command = args[1], args[3:]
    # Resolve the command the way a shell would (npx is npx.cmd on Windows)
    command[0] = shutil.which(command[0]) or command[0]
    try:
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    except OSError as e:
        print(f"Orbit could not start the {server} MCP server ({command[0]}): {e}", file=sys.stderr)
        sys.exit(1)
    threading.Thread(target=poster, daemon=True).start()
    threading.Thread(target=pump, args=(sys.stdin.buffer, child.stdin, lambda line: from_desktop(line, server), True),
                     daemon=True).start()
    pump(child.stdout, sys.stdout.buffer, from_server)
    code = child.wait()
    flushed = threading.Thread(target=outbox.join, daemon=True)
    flushed.start()
    flushed.join(5)  # let the last results reach Orbit
    sys.exit(code)


if __name__ == "__main__":
    main()
