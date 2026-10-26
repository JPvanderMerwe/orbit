#!/usr/bin/env python3
"""End-to-end check of Orbit on this computer (run on Windows, macOS and Linux by .github/workflows/test.yml).

Runs everything the way Claude Code would, against a throwaway Orbit (its own port, data folder and Claude
folder), with a fake `claude` command so no real Claude session starts:

- a hook started through scripts/run.sh starts the server, and events keep non-English text intact
- a workspace terminal runs `claude`, shows its output, takes typing, reports its exit code, and can be closed
- running Claude sessions are listed, other processes are not, and checking them does not disturb them
- the status line, `orbit install-hooks` and the commands it writes work
- `orbit install-desktop` puts Orbit in front of Claude Desktop's MCP servers: messages pass unchanged, every
  tool call and result reaches Orbit (starting Orbit if needed), and uninstall-desktop restores the settings

    python tests/smoke.py
"""
import base64
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "plugins" / "orbit" / "scripts"
WINDOWS = os.name == "nt"
PORT = 7799
URL = f"http://127.0.0.1:{PORT}"
TEXT = "héllo — ✓ 日本語"
SID = str(uuid.uuid4())

work = Path(tempfile.mkdtemp(prefix="orbit-smoke-"))
data_dir, claude_dir, bin_dir, project = work / "data", work / "claude", work / "bin", work / "project"
for d in (claude_dir / "sessions", bin_dir, project):
    d.mkdir(parents=True)

# A fake claude: prints its arguments and the terminal id, echoes one typed line, exits with code 3
if WINDOWS:
    (bin_dir / "claude.cmd").write_text(
        "@echo off\r\necho FAKE CLAUDE READY [%*] term=%ORBIT_TERM_ID%\r\nset /p LINE=\r\necho GOT:%LINE%\r\nexit /b 3\r\n")
else:
    fake = bin_dir / "claude"
    fake.write_text('#!/bin/sh\necho "FAKE CLAUDE READY [$*] term=$ORBIT_TERM_ID"\nread LINE\necho "GOT:$LINE"\nexit 3\n')
    fake.chmod(0o755)

env = dict(os.environ, ORBIT_PORT=str(PORT), ORBIT_DATA=str(data_dir), CLAUDE_CONFIG_DIR=str(claude_dir),
           PATH=str(bin_dir) + os.pathsep + os.environ["PATH"])
results = []

# A minimal MCP server with one tool, "shout"
ECHO_MCP = """import json, sys
for line in sys.stdin.buffer:
    m = json.loads(line)
    if "id" not in m:
        continue
    if m["method"] == "initialize":
        r = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "echo", "version": "1"}}
    elif m["method"] == "tools/call" and m["params"]["arguments"].get("fail"):
        r = {"content": [{"type": "text", "text": "it broke"}], "isError": True}
    elif m["method"] == "tools/call":
        r = {"content": [{"type": "text", "text": m["params"]["arguments"]["text"].upper()}]}
    else:
        r = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}) + "\\n")
    sys.stdout.flush()
"""


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""), flush=True)
    return ok


def wait(test, seconds=10):
    end = time.time() + seconds
    while time.time() < end:
        try:
            value = test()
            if value:
                return value
        except Exception:
            pass
        time.sleep(0.2)
    return None


def key():
    return (data_dir / "token").read_text(encoding="utf-8").strip()


def api(method, path, body=None):
    req = urllib.request.Request(URL + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Orbit-Key": key()})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def hook(event):
    """Run the hook exactly as Claude Code's hooks.json does: sh run.sh hook.py, event as UTF-8 JSON on stdin."""
    payload = {"session_id": SID, "cwd": str(project), "transcript_path": "", **event}
    return subprocess.run(["sh", str(SCRIPTS / "run.sh"), "hook.py"], input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          env=env, capture_output=True, timeout=20)


class Socket:
    """Just enough of a WebSocket client for a workspace terminal."""

    def __init__(self, path):
        self.s = socket.create_connection(("127.0.0.1", PORT), timeout=10)
        k = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {k}\r\nSec-WebSocket-Version: 13\r\nX-Orbit-Key: {key()}\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.s.recv(1)
        assert b" 101 " in head.split(b"\r\n")[0], head
        self.screen, self.exit = b"", None

    def send(self, obj):
        data, mask = json.dumps(obj).encode(), os.urandom(4)
        n = len(data)
        head = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n))
        self.s.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _exact(self, n):
        buf = b""
        while len(buf) < n:
            part = self.s.recv(n - len(buf))
            if not part:
                raise ConnectionError("closed")
            buf += part
        return buf

    def pump(self, until, seconds=15):
        end = time.time() + seconds
        while time.time() < end and not until():
            self.s.settimeout(max(0.1, end - time.time()))
            try:
                b1, b2 = self._exact(2)
            except (socket.timeout, ConnectionError):
                break
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._exact(8))[0]
            payload = self._exact(n)
            if b1 & 0x0F == 2:
                self.screen += payload
            elif b1 & 0x0F == 1:
                msg = json.loads(payload)
                if msg.get("t") == "exit":
                    self.exit = msg.get("code")
        return until()


def main():
    started = []
    try:
        # ---- the hook starts the server; text survives the trip
        r = hook({"hook_event_name": "SessionStart", "source": "startup"})
        check("hook exits cleanly", r.returncode == 0, r.stderr.decode("utf-8", "replace"))
        health = wait(lambda: json.loads(urllib.request.urlopen(URL + "/health", timeout=1).read()))
        check("hook started the Orbit server", bool(health), "no /health")
        if not health:
            print((data_dir / "server.log").read_text(encoding="utf-8", errors="replace") if (data_dir / "server.log").exists() else "no log")
            return
        check("server version is 3.4.1", health.get("version") == "3.4.1", health)
        check("built-in terminals supported", health.get("terminals") is True, health)
        hook({"hook_event_name": "UserPromptSubmit", "prompt": TEXT})
        prompts = wait(lambda: [e for e in api("GET", "/events") if e.get("hook_event_name") == "UserPromptSubmit"])
        check("non-English prompt text arrives intact", bool(prompts) and prompts[0].get("prompt") == TEXT, prompts)

        # ---- a workspace terminal runs claude
        terms = api("GET", "/api/terms")
        check("claude command found", terms.get("claude") is True, terms)
        t = api("POST", "/api/terms", {"cwd": str(project), "mode": "new", "cols": 100, "rows": 30})
        started.append(t["id"])
        ws = Socket(f"/term/{t['id']}/ws")
        ok = ws.pump(lambda: b"FAKE CLAUDE READY" in ws.screen)
        check("terminal shows claude's output", ok, ws.screen[-300:])
        check("claude sees its workspace id", f"term={t['id']}".encode() in ws.screen, ws.screen[-300:])
        ws.send({"t": "resize", "cols": 120, "rows": 40})
        ws.send({"t": "in", "d": "typed-in-orbit\r"})
        ok = ws.pump(lambda: b"GOT:typed-in-orbit" in ws.screen)
        check("typing reaches claude", ok, ws.screen[-300:])
        ws.pump(lambda: ws.exit is not None)
        check("exit code is reported", ws.exit == 3, ws.exit)
        gone = wait(lambda: not next(x for x in api("GET", "/api/terms")["terms"] if x["id"] == t["id"])["alive"])
        check("terminal marked closed", bool(gone))

        t2 = api("POST", "/api/terms", {"cwd": str(project), "mode": "resume", "session": SID})
        started.append(t2["id"])
        ws2 = Socket(f"/term/{t2['id']}/ws")
        check("resume passes --resume <id>", ws2.pump(lambda: f"--resume {SID}".encode() in ws2.screen), ws2.screen[-300:])
        api("POST", f"/api/terms/{t2['id']}/kill", {})
        gone = wait(lambda: not next(x for x in api("GET", "/api/terms")["terms"] if x["id"] == t2["id"])["alive"])
        check("closing a terminal ends claude", bool(gone))

        if WINDOWS:  # the native installer's claude.exe starts directly, not through cmd.exe as claude.cmd does
            sys.path.insert(0, str(SCRIPTS))
            import windows
            con = windows.PseudoConsole(windows.command_line(sys.executable, ["-c", "print('EXE OK ' + 'é✓')"]), str(project),
                                        dict(os.environ), 80, 24)
            out = b""
            while True:
                part = con.read()
                if not part:
                    break
                out += part
            con.exited.wait(10)
            con.release()
            check("a .exe runs in a Windows pseudo console", b"EXE OK" in out and con.exit_code == 0, (out[-200:], con.exit_code))

        # ---- running sessions: Claude processes are listed, others are not, neither is disturbed
        sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
        other = subprocess.Popen(sleeper)
        if WINDOWS:  # an npm-installed Claude runs as node.exe (a copied python.exe would not find its DLLs)
            claude_proc = subprocess.Popen([shutil.which("node"), "-e", "setTimeout(() => {}, 60000)"])
        else:
            fake_claude = bin_dir / "claude-session"
            shutil.copy2(sys.executable, fake_claude)
            claude_proc = subprocess.Popen([str(fake_claude), *sleeper[1:]])
        started_procs = [other, claude_proc]
        ids = {}
        for p in started_procs:
            ids[p.pid] = str(uuid.uuid4())
            (claude_dir / "sessions" / f"{p.pid}.json").write_text(json.dumps(
                {"pid": p.pid, "sessionId": ids[p.pid], "cwd": str(project), "status": "idle", "startedAt": int(time.time() * 1000)}))
        listed = wait(lambda: {s["session_id"] for s in api("GET", "/api/live")["sessions"]} & {ids[claude_proc.pid]})
        check("running Claude session is listed", bool(listed))
        if WINDOWS or sys.platform == "darwin":  # Linux trusts any live process id when Claude recorded no start time
            time.sleep(2)
            names = {s["session_id"] for s in api("GET", "/api/live")["sessions"]}
            check("other processes are not listed", ids[other.pid] not in names)
        time.sleep(2)
        check("checking sessions leaves them running", other.poll() is None and claude_proc.poll() is None,
              (other.poll(), claude_proc.poll()))
        r = api("POST", f"/api/live/{ids[claude_proc.pid]}/stop", {})
        check("Stop closes a running session", bool(wait(lambda: claude_proc.poll() is not None)), r)
        other.kill()

        # ---- status line and the standalone command
        status = subprocess.run(["sh", str(SCRIPTS / "run.sh"), "statusline.py"], env=env, capture_output=True, timeout=20,
                                input=json.dumps({"session_id": SID, "rate_limits": {"five_hour": {"used_percentage": 12}}}).encode())
        check("status line prints", status.stdout.decode("utf-8", "replace").strip() == "◉ Orbit · 5h 12%", status.stdout)
        version = subprocess.run(["sh", str(SCRIPTS / "run.sh"), "cli.py", "version"], env=env, capture_output=True, timeout=20)
        check("orbit version", version.stdout.decode().strip() == "Orbit 3.4.1", version.stdout + version.stderr)
        link = subprocess.run(["sh", str(SCRIPTS / "run.sh"), "cli.py", "url"], env=env, capture_output=True, timeout=20)
        check("orbit url gives the private link", link.stdout.decode().strip() == f"{URL}/?k={key()}", link.stdout + link.stderr)
        inst = subprocess.run([sys.executable, str(SCRIPTS / "cli.py"), "install-hooks", "--force"], env=env, capture_output=True, timeout=20)
        settings = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
        command = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
        check("install-hooks writes the hooks", inst.returncode == 0 and "--orbit-standalone" in command, inst.stdout + inst.stderr)
        before = len(api("GET", "/events"))
        subprocess.run(["sh", "-c", command], env=env, timeout=20,
                       input=json.dumps({"session_id": SID, "hook_event_name": "Stop", "cwd": str(project)}).encode())
        check("an installed hook command reaches Orbit", bool(wait(lambda: len(api("GET", "/events")) > before)))

        # ---- Claude Desktop: a stand-in MCP server, started through a launcher on PATH as npx would be
        (work / "echo_mcp.py").write_text(ECHO_MCP, encoding="utf-8")
        if WINDOWS:
            (bin_dir / "echo-mcp.cmd").write_text(f'@"{sys.executable}" "{work / "echo_mcp.py"}" %*\r\n')
        else:
            (bin_dir / "echo-mcp").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{work / "echo_mcp.py"}" "$@"\n')
            (bin_dir / "echo-mcp").chmod(0o755)
        config = work / "claude_desktop_config.json"
        original = {"mcpServers": {"echo": {"command": "echo-mcp", "args": ["--flag", "two words"], "env": {"A": "1"}},
                                   "remote": {"url": "https://example.com/mcp"}}, "preferences": {"keep": True}}
        config.write_text(json.dumps(original), encoding="utf-8")
        inst = subprocess.run(["sh", str(SCRIPTS / "run.sh"), "cli.py", "install-desktop", "--config", str(config)],
                              env=env, capture_output=True, timeout=20)
        wrapped = json.loads(config.read_text(encoding="utf-8"))
        entry = wrapped["mcpServers"]["echo"]
        check("install-desktop wraps the local server only", "--orbit-desktop" in entry["args"] and entry["env"] == {"A": "1"}
              and wrapped["mcpServers"]["remote"] == original["mcpServers"]["remote"] and wrapped["preferences"] == {"keep": True},
              inst.stdout + inst.stderr)

        def desktop_session(calls):
            """Start the server exactly as Claude Desktop would and make tool calls; returns the replies."""
            p = subprocess.Popen([entry["command"], *entry["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
            msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"}]
            msgs += [{"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call", "params": {"name": "shout", "arguments": a}}
                     for i, a in enumerate(calls)]
            out, _ = p.communicate("".join(json.dumps(m, ensure_ascii=False) + "\n" for m in msgs).encode("utf-8"), timeout=30)
            return [json.loads(line) for line in out.decode("utf-8").splitlines() if line.strip()], p.returncode

        replies, code = desktop_session([{"text": TEXT}, {"fail": True}])
        check("Desktop's messages pass through unchanged", code == 0 and len(replies) == 3
              and replies[0]["result"]["serverInfo"]["name"] == "echo" and replies[1]["result"]["content"][0]["text"] == TEXT.upper(),
              (code, replies))

        def desktop_events():
            return [e for e in api("GET", "/events") if e.get("source") == "claude-desktop"]
        got = wait(lambda: len(desktop_events()) >= 4 and desktop_events()) or []
        calls = {}  # tool use id -> its events, in the order Orbit received them
        for e in got:
            calls.setdefault(e.get("tool_use_id"), []).append(e)
        flows = sorted([(e["hook_event_name"], e["tool_name"]) for e in c] for c in calls.values())
        check("Desktop tool calls reach Orbit", flows == [[("PreToolUse", "mcp__echo__shout"), ("PostToolUse", "mcp__echo__shout")],
                                                          [("PreToolUse", "mcp__echo__shout"), ("PostToolUseFailure", "mcp__echo__shout")]], flows)
        done = next((c for c in calls.values() if c[0].get("tool_input") == {"text": TEXT}), None)
        failed = next((c for c in calls.values() if c[0].get("tool_input") == {"fail": True}), None)
        check("Desktop tool input and result are intact", bool(done and failed) and len(done) == 2 and len(failed) == 2
              and TEXT.upper() in json.dumps(done[1].get("tool_response"), ensure_ascii=False) and failed[1].get("error") == "it broke", got)

        os.kill(int((data_dir / "server.pid").read_text(encoding="utf-8")), signal.SIGTERM)
        wait(lambda: not urllib.request.urlopen(URL + "/health", timeout=0.3), 5)
        (data_dir / ".last-start").unlink(missing_ok=True)
        # Claude Desktop starts all its MCP servers together when it opens
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(3) as pool:
            codes = [r[1] for r in pool.map(lambda i: desktop_session([{"text": f"after restart {i}"}]), range(3))]
        wanted = {f"after restart {i}" for i in range(3)}
        def restarted():
            return {(e.get("tool_input") or {}).get("text") for e in desktop_events() if e.get("hook_event_name") == "PostToolUse"} & wanted
        arrived = wait(lambda: restarted() == wanted, 20)
        check("Desktop starts Orbit when it is not running (3 servers at once)", codes == [0, 0, 0] and bool(arrived),
              (codes, sorted(wait(restarted, 1) or [])))

        subprocess.run(["sh", str(SCRIPTS / "run.sh"), "cli.py", "uninstall-desktop", "--config", str(config)], env=env, capture_output=True, timeout=20)
        check("uninstall-desktop restores Claude Desktop's settings", json.loads(config.read_text(encoding="utf-8")) == original)
    finally:
        try:
            pid = int((data_dir / "server.pid").read_text(encoding="utf-8"))
            os.kill(pid, signal.SIGTERM)
        except (OSError, ValueError):
            pass
        time.sleep(1)
        log = data_dir / "server.log"
        if log.exists() and not all(results):
            print("---- server log\n" + log.read_text(encoding="utf-8", errors="replace"))
        shutil.rmtree(work, ignore_errors=True)
    passed = sum(results)
    print(f"\n{passed} of {len(results)} checks passed")
    sys.exit(0 if results and all(results) else 1)


if __name__ == "__main__":
    main()
