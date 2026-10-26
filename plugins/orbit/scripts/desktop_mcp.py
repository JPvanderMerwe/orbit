#!/usr/bin/env python3
"""Orbit inside Claude Desktop: an MCP server whose one tool opens Orbit.

`orbit install-desktop` adds it to Claude Desktop's settings. In any chat the user says "open Orbit",
Claude calls open_orbit, and Orbit opens in the browser (started first if needed). Nothing opens
unless the user asks. The private link goes only to the browser; Claude never sees the access key.

Speaks MCP over stdio (newline-delimited JSON-RPC). Standard library only.
"""
import json
import os
import sys
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook import URL, access_key, server_up, start_server  # noqa: E402
from mcp_proxy import orbit_scripts, wait_for_orbit  # noqa: E402

VERSION = "3.5.0"
TOOLS = [{
    "name": "open_orbit",
    "description": "Open Orbit, the user's live view of what Claude does on this computer, in their web browser. "
                   "Use only when the user asks to open, show or start Orbit.",
    "inputSchema": {"type": "object", "properties": {}},
}]


def open_orbit():
    if not server_up(0.5):
        scripts = orbit_scripts()
        if not scripts:
            return "Orbit isn't installed properly. Run `orbit install-desktop` again in a terminal."
        start_server(force=True, scripts=scripts)
        wait_for_orbit()
    key = access_key()
    if not server_up(1) or not key:
        return "Orbit didn't start. Its log is in the .orbit folder in your home folder (server.log)."
    if webbrowser.open(f"{URL}/?k={key}"):
        return "Orbit opened in your browser."
    return "Orbit is running, but the browser didn't open. In a terminal, run `orbit` to open it."


def reply(msg_id, result=None, error=None):
    body = {"jsonrpc": "2.0", "id": msg_id}
    body.update({"error": error} if error else {"result": result})
    sys.stdout.buffer.write((json.dumps(body) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def handle(m):
    method, msg_id = m.get("method"), m.get("id")
    if msg_id is None:
        return  # a notification
    if method == "initialize":
        reply(msg_id, {"protocolVersion": (m.get("params") or {}).get("protocolVersion", "2025-06-18"),
                       "capabilities": {"tools": {}}, "serverInfo": {"name": "orbit", "version": VERSION}})
    elif method == "tools/list":
        reply(msg_id, {"tools": TOOLS})
    elif method == "tools/call" and (m.get("params") or {}).get("name") == "open_orbit":
        reply(msg_id, {"content": [{"type": "text", "text": open_orbit()}]})
    elif method == "ping":
        reply(msg_id, {})
    else:
        reply(msg_id, error={"code": -32601, "message": f"Not supported: {method}"})


def main():
    for line in sys.stdin.buffer:  # bytes: MCP messages are UTF-8 whatever the system's code page
        try:
            m = json.loads(line)
        except ValueError:
            continue
        for one in m if isinstance(m, list) else [m]:
            if isinstance(one, dict):
                handle(one)


if __name__ == "__main__":
    main()
