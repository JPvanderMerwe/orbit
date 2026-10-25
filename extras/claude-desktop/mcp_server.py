#!/usr/bin/env python3
"""MCP server for apps without hooks (e.g. Claude Desktop).

Claude Desktop has no hooks, so nothing can watch its actions directly.
Instead this server gives Claude a `show_step` tool and asks it (through the
server instructions) to report each step it takes. Those steps appear in the
same dashboard, marked as "reported by Claude".

Limitation: this is self-reported. Claude decides when to call it, so it is
a narration, not a guaranteed record like the Claude Code hooks.

Speaks MCP over stdio (newline-delimited JSON-RPC). Standard library only.
"""
import json
import os
import sys
import urllib.request

PORT = int(os.environ.get("ORBIT_PORT") or os.environ.get("CLAUDE_VIZ_PORT") or "7777")
SOURCE = os.environ.get("ORBIT_SOURCE", "claude-desktop")

INSTRUCTIONS = (
    "The user watches a live activity dashboard and wants to see what you are "
    "doing behind the scenes. Call show_step BEFORE each meaningful action "
    "(planning, searching, reading, writing code, using another tool) and once "
    "when you finish. Keep titles short. Put code you write or change in `code`."
)

TOOLS = [
    {
        "name": "show_step",
        "description": "Show the user, on their live activity dashboard, what you are doing right now.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short label, e.g. 'Reading the config'"},
                "kind": {
                    "type": "string",
                    "enum": ["plan", "search", "read", "write", "tool", "done", "error"],
                    "description": "Type of step",
                },
                "detail": {"type": "string", "description": "Plain-language explanation of the step"},
                "code": {"type": "string", "description": "Optional code being written or changed"},
            },
            "required": ["title", "kind"],
        },
    }
]


def access_key():
    data_dir = os.environ.get("ORBIT_DATA") or os.environ.get("CLAUDE_VIZ_DATA") or os.path.join(os.path.expanduser("~"), ".orbit")
    try:
        with open(os.path.join(data_dir, "token")) as f:
            return f.read().strip()
    except OSError:
        return ""


def post_event(payload):
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/event",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Orbit-Key": access_key()},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=1).read()
        return True
    except Exception:
        return False


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def handle(message):
    method = message.get("method")
    msg_id = message.get("id")
    if msg_id is None:
        return  # notification, no reply

    if method == "initialize":
        result = {
            "protocolVersion": message.get("params", {}).get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "orbit", "version": "3.2.0"},
            "instructions": INSTRUCTIONS,
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = message.get("params", {})
        if params.get("name") != "show_step":
            send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": "Unknown tool"}})
            return
        args = params.get("arguments", {})
        ok = post_event({
            "source": SOURCE,
            "hook_event_name": "ReportedStep",
            "step": {
                "title": args.get("title", ""),
                "kind": args.get("kind", "tool"),
                "detail": args.get("detail", ""),
                "code": args.get("code", ""),
            },
        })
        text = "Shown on dashboard." if ok else "Dashboard not running; step not shown. Carry on."
        result = {"content": [{"type": "text", "text": text}]}
    elif method == "ping":
        result = {}
    else:
        send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Method not found: {method}"}})
        return
    send({"jsonrpc": "2.0", "id": msg_id, "result": result})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except json.JSONDecodeError:
            continue


if __name__ == "__main__":
    main()
