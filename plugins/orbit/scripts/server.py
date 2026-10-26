#!/usr/bin/env python3
"""Orbit - local server for the dashboard and the Claude workspace.

- Receives events from Claude Code hooks (hook.py) and streams them live to
  the browser over Server-Sent Events.
- Runs the user's own, unmodified `claude` command in pseudo-terminals and
  connects them to the browser over WebSockets (the workspace terminals).
- Reads token usage from Claude Code's own session transcripts and
  estimates what it would cost at API list prices.

Security: a terminal is a shell on this computer, so every request needs the
secret access key stored in ~/.orbit/token (readable only by this user).
The browser receives it once via the link /orbit opens and keeps it
in a strict same-site cookie. The server binds to 127.0.0.1 only and also
refuses requests whose Host, Origin or Sec-Fetch-Site show another website.
Standard library only. Terminals need macOS, Linux or Windows 10 (1809) or later.
"""
import base64
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VERSION = "3.3.0"
HOST = "127.0.0.1"
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
WINDOWS = os.name == "nt"

if WINDOWS:
    import windows  # noqa: E402 - ConPTY terminals and process checks
    HAS_PTY = windows.conpty_available()
else:
    import fcntl
    import pty
    import termios
    HAS_PTY = True
from hook import APP, DATA_DIR, KEY_HEADER, PORT, clean_env  # noqa: E402

DASHBOARD = SCRIPT_DIR / "dashboard.html"
VENDOR_DIR = SCRIPT_DIR / "vendor"
LOG_FILE = DATA_DIR / "events.jsonl"
PID_FILE = DATA_DIR / "server.pid"
TOKEN_FILE = DATA_DIR / "token"
MAX_EVENTS = 5000
MAX_FILE_BYTES = 50 * 1024 * 1024
SCROLLBACK_BYTES = 512 * 1024
PRICES_FILE = DATA_DIR / "prices.json"
COOKIE = "orbit_key"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
VENDOR_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".txt": "text/plain; charset=utf-8"}

ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://{h}" for h in ALLOWED_HOSTS}

events = []
events_lock = threading.Lock()
subscribers = []
subscribers_lock = threading.Lock()
next_id = 1
TOKEN = ""
BOOT = uuid.uuid4().hex[:12]  # changes every time the server starts
HEARTBEAT = 5                 # seconds between heartbeats on the live stream


# ---------------------------------------------------------------- access key
def load_token():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            return token
    token = secrets.token_urlsafe(32)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token)
    return token


# ---------------------------------------------------------------- event log
def rewrite_log():
    """Keep the on-disk log to the events held in memory."""
    tmp = LOG_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    tmp.replace(LOG_FILE)


def load_history():
    global next_id
    if not LOG_FILE.exists():
        return
    with LOG_FILE.open(encoding="utf-8") as f:
        for line in f:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(event)
            if isinstance(event, dict):
                note_transcript(event)
                note_hooked(event)
            if len(events) > MAX_EVENTS * 2:
                del events[:-MAX_EVENTS]
    del events[:-MAX_EVENTS]
    if events:
        next_id = max(e.get("id", 0) for e in events) + 1
    if LOG_FILE.stat().st_size > MAX_FILE_BYTES:
        rewrite_log()


def add_event(payload):
    global next_id
    with events_lock:
        event = {"id": next_id, "received_at": time.time(), **payload}
        note_transcript(event)
        note_hooked(event)
        next_id += 1
        events.append(event)
        del events[:-MAX_EVENTS]
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
        if LOG_FILE.stat().st_size > MAX_FILE_BYTES:
            rewrite_log()
    broadcast(event)
    return event


def broadcast(event):
    with subscribers_lock:
        for q in subscribers:
            q.put(event)


# ---------------------------------------------------------------- token usage and cost
# US$ per million tokens: input, output, 5-minute cache write, 1-hour cache write, cache read.
# Anthropic API list prices as of October 2026. Add a ~/.orbit/prices.json with the same shape
# ({"model-id-prefix": [in, out, write5m, write1h, read]}) to correct or extend them.
PRICES_AS_OF = "October 2026"
PRICES = {
    "claude-fable-5-1": (10, 50, 12.5, 20, 0.25), "claude-mythos-5-1": (10, 50, 12.5, 20, 0.25),
    "claude-fable-5": (10, 50, 12.5, 20, 1), "claude-mythos-5": (10, 50, 12.5, 20, 1),
    "claude-opus-5-5": (4, 20, 5, 8, 0.2), "claude-opus-5": (5, 25, 6.25, 10, 0.5),
    "claude-opus-4-8": (5, 25, 6.25, 10, 0.5), "claude-opus-4-7": (5, 25, 6.25, 10, 0.5), "claude-opus-4-6": (5, 25, 6.25, 10, 0.5),
    "claude-sonnet-5-5": (2, 10, 2.5, 4, 0.2), "claude-sonnet-5": (2, 10, 2.5, 4, 0.2), "claude-sonnet-4-6": (3, 15, 3.75, 6, 0.3),
    "claude-haiku-5-5": (0.1, 0.5, 0.125, 0.2, 0.01), "claude-haiku-4-5": (1, 5, 1.25, 2, 0.1),
}
# Claude Haiku 5.5 has a second rate card for prompts over 100K tokens
LONG_PROMPT = {"claude-haiku-5-5": (100_000, (0.5, 2.5, 0.625, 1, 0.05))}
FAST_MULTIPLIER = 2  # fast mode list prices are twice the standard rate


def load_prices():
    try:
        extra = json.loads(PRICES_FILE.read_text(encoding="utf-8"))
        for model, rates in extra.items():
            if isinstance(rates, list) and len(rates) == 5:
                PRICES[str(model)] = tuple(float(r) for r in rates)
    except (OSError, ValueError, TypeError):
        pass


def price_key(model):
    m = str(model or "")
    m = m[m.find("claude-"):] if "claude-" in m else m
    for key in sorted(PRICES, key=len, reverse=True):
        if m.startswith(key):
            return key
    return None


def request_cost(model, inp, out, w5, w1, read, fast):
    key = price_key(model)
    if not key:
        return None
    rates = PRICES[key]
    if key in LONG_PROMPT and inp + w5 + w1 + read > LONG_PROMPT[key][0]:
        rates = LONG_PROMPT[key][1]
    cost = (inp * rates[0] + out * rates[1] + w5 * rates[2] + w1 * rates[3] + read * rates[4]) / 1e6
    return cost * (FAST_MULTIPLIER if fast else 1)


def iso_time(text):
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class TranscriptFile:
    """Reads one transcript JSONL incrementally and keeps one usage record per API request."""

    def __init__(self, path, agent=False):
        self.path, self.agent, self.offset, self.records = path, agent, 0, {}

    def update(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self.offset:  # rewritten from the start
            self.offset, self.records = 0, {}
        if size == self.offset:
            return
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            data = f.read(size - self.offset)
        end = data.rfind(b"\n")
        if end < 0:
            return
        self.offset += end + 1
        for line in data[:end].splitlines():
            if b'"usage"' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            msg = row.get("message") if isinstance(row, dict) else None
            usage = msg.get("usage") if isinstance(msg, dict) else None
            model = msg.get("model") if isinstance(msg, dict) else None
            if not isinstance(usage, dict) or not model or model.startswith("<"):
                continue
            created = usage.get("cache_creation") or {}
            w1 = int(created.get("ephemeral_1h_input_tokens") or 0)
            w5 = int(created.get("ephemeral_5m_input_tokens") or 0) if created else int(usage.get("cache_creation_input_tokens") or 0)
            inp, out, read = (int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens"))
            fast = usage.get("speed") == "fast"
            # The same request is written once per content block; the last copy has the final counts
            key = (msg.get("id"), row.get("requestId"))
            self.records[key] = {"t": iso_time(row.get("timestamp")), "model": model, "in": inp, "out": out, "w5": w5, "w1": w1,
                                 "read": read, "cost": request_cost(model, inp, out, w5, w1, read, fast), "agent": self.agent}


class SessionUsage:
    def __init__(self, transcript):
        self.main = TranscriptFile(transcript)
        self.agents = {}
        self.agent_dir = Path(transcript).with_suffix("") / "subagents"

    def records(self):
        self.main.update()
        try:
            for f in self.agent_dir.glob("*.jsonl"):
                if str(f) not in self.agents:
                    self.agents[str(f)] = TranscriptFile(str(f), agent=True)
        except OSError:
            pass
        for a in self.agents.values():
            a.update()
        out = list(self.main.records.values())
        for a in self.agents.values():
            out.extend(a.records.values())
        return sorted(out, key=lambda r: r["t"])


transcripts = {}      # Claude session id -> transcript path (from hook events)
plan_status = {}      # Claude session id -> latest status line data (cost, context, plan limits)
STATUS_FILE = DATA_DIR / "status.json"
usage_cache = {}      # transcript path -> SessionUsage
usage_lock = threading.Lock()


def note_transcript(event):
    path, sid = event.get("transcript_path"), event.get("session_id")
    if isinstance(path, str) and isinstance(sid, str) and path.endswith(".jsonl") and os.path.isabs(path):
        transcripts[sid] = path


def summarise_usage(records):
    totals = {"in": 0, "out": 0, "w5": 0, "w1": 0, "read": 0, "cost": 0.0, "requests": len(records), "unpriced": 0}
    models = {}
    for r in records:
        for k in ("in", "out", "w5", "w1", "read"):
            totals[k] += r[k]
        tokens = r["in"] + r["out"] + r["w5"] + r["w1"] + r["read"]
        m = models.setdefault(r["model"], {"tokens": 0, "cost": 0.0, "requests": 0, "priced": r["cost"] is not None})
        m["tokens"] += tokens
        m["requests"] += 1
        if r["cost"] is None:
            totals["unpriced"] += 1
        else:
            totals["cost"] += r["cost"]
            m["cost"] += r["cost"]
    totals["tokens"] = totals["in"] + totals["out"] + totals["w5"] + totals["w1"] + totals["read"]
    return totals, models


def session_records(sid):
    path = transcripts.get(sid)
    if not path:
        # A conversation opened from the history has no hook events yet; find its transcript on disk
        found = find_transcript(sid)
        if not found:
            return None
        path = transcripts[sid] = str(found)
    su = usage_cache.get(path)
    if not su:
        su = usage_cache[path] = SessionUsage(path)
    return su.records()


def usage_report(sid):
    with usage_lock:
        if sid != "*":
            records = session_records(sid)
            if records is None:
                return {"session": sid, "known": False, "prices_as_of": PRICES_AS_OF}
            totals, models = summarise_usage(records)
            # compact per-request list [time, tokens, cost, output tokens] so the page can attribute cost to each prompt
            reqs = [[round(r["t"], 3), r["in"] + r["out"] + r["w5"] + r["w1"] + r["read"], r["cost"], r["out"]] for r in records]
            return {"session": sid, "known": True, "totals": totals, "models": models, "requests": reqs, "prices_as_of": PRICES_AS_OF}
        per, every = {}, []
        for s in list(transcripts):
            records = session_records(s) or []
            every.extend(records)
            per[s] = summarise_usage(records)[0]
        totals, models = summarise_usage(every)
        return {"session": "*", "known": True, "totals": totals, "models": models, "sessions": per, "prices_as_of": PRICES_AS_OF}


# ---------------------------------------------------------------- chat history
# Every conversation Claude Code has saved on this computer, including ones that ran before
# Orbit was installed or in a plain terminal. Read-only; the files stay where Claude Code keeps them.
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
SESSION_ID_OK = re.compile(r"^[A-Za-z0-9_-]{6,100}$")
history_cache = {}    # path -> (mtime, size, summary)
history_lock = threading.Lock()
EDGE = 256 * 1024
CHAT_TEXT = 4000


def _lines(chunk, drop_first):
    lines = chunk.split(b"\n")
    if drop_first:
        lines = lines[1:]
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            yield row


def human_text(row):
    """The text of a prompt the person typed, or None for tool results, notices and system turns."""
    if row.get("type") != "user" or row.get("isMeta") or row.get("isSidechain"):
        return None
    origin = (row.get("origin") or {}).get("kind")
    if origin and origin != "human":
        return None
    content = (row.get("message") or {}).get("content")
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(content, str) or not content.strip():
        return None
    if not origin and content.lstrip().startswith("<") and "<command-name>" not in content:
        return None
    return content


def summarise_transcript(path):
    st = path.stat()
    with open(path, "rb") as f:
        head = f.read(EDGE)
        tail = b""
        if st.st_size > EDGE:
            f.seek(max(EDGE, st.st_size - EDGE))
            tail = f.read()
    info = {"id": path.stem, "project": path.parent.name, "cwd": "", "title": "", "first_prompt": "", "last_prompt": "",
            "start": 0.0, "end": st.st_mtime, "size": st.st_size, "version": "", "entrypoint": ""}
    for row in _lines(head, False):
        if not info["cwd"] and row.get("cwd"):
            info["cwd"] = row["cwd"]
        if not info["start"] and row.get("timestamp"):
            info["start"] = iso_time(row["timestamp"])
        info["version"] = info["version"] or row.get("version", "")
        info["entrypoint"] = info["entrypoint"] or row.get("entrypoint", "")
        if not info["first_prompt"]:
            text = human_text(row)
            if text:
                info["first_prompt"] = text[:300]
        if row.get("type") == "ai-title":
            info["title"] = row.get("aiTitle", "")
        if row.get("type") == "last-prompt":
            info["last_prompt"] = str(row.get("lastPrompt", ""))[:300]
    for row in _lines(tail, True):
        if row.get("type") == "ai-title":
            info["title"] = row.get("aiTitle", "")
        if row.get("type") == "last-prompt":
            info["last_prompt"] = str(row.get("lastPrompt", ""))[:300]
        if row.get("timestamp"):
            info["end"] = max(info["end"] if info["end"] != st.st_mtime else 0, iso_time(row["timestamp"]))
    if not info["start"]:
        info["start"] = info["end"]
    return info


def list_history():
    out = []
    with history_lock:
        for path in CLAUDE_DIR.glob("projects/*/*.jsonl"):
            try:
                st = path.stat()
                cached = history_cache.get(str(path))
                if not cached or cached[0] != st.st_mtime or cached[1] != st.st_size:
                    cached = history_cache[str(path)] = (st.st_mtime, st.st_size, summarise_transcript(path))
            except OSError:
                continue
            if cached[2]["first_prompt"] or cached[2]["title"]:
                out.append(cached[2])
    return sorted(out, key=lambda x: x["end"], reverse=True)


def search_history(q):
    """Ids of conversations whose saved text contains q (case-insensitive)."""
    q = (q or "").strip().lower()
    if len(q) < 3:
        return []
    needles = {q.encode(), json.dumps(q)[1:-1].encode()}
    ids = []
    for path in CLAUDE_DIR.glob("projects/*/*.jsonl"):
        try:
            data = path.read_bytes().lower()
        except OSError:
            continue
        if any(n in data for n in needles):
            ids.append(path.stem)
    return ids


def find_transcript(sid):
    if not SESSION_ID_OK.match(sid or ""):
        return None
    known = transcripts.get(sid)
    if known and os.path.isfile(known):
        return Path(known)
    for path in CLAUDE_DIR.glob(f"projects/*/{sid}.jsonl"):
        return path
    return None


def clip(value, limit=CHAT_TEXT):
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"\n… [{len(value) - limit} more characters]"
    if isinstance(value, dict):
        return {k: clip(v, limit) for k, v in value.items() if k not in ("signature",)}
    if isinstance(value, list):
        return [clip(v, limit) for v in value[:200]]
    return value


def result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") if isinstance(b, dict) and b.get("type") == "text" else "[image]" if isinstance(b, dict) and b.get("type") == "image" else "" for b in content)
    return ""


def read_chat(sid):
    path = find_transcript(sid)
    if not path:
        return None
    items, tools, title, cwd = [], {}, "", ""
    with open(path, "rb") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get("isSidechain"):
                continue
            kind, t = row.get("type"), iso_time(row.get("timestamp")) if row.get("timestamp") else 0
            cwd = cwd or row.get("cwd", "")
            if kind == "ai-title":
                title = row.get("aiTitle", "")
                continue
            if kind == "system" and row.get("subtype") == "turn_duration":
                items.append({"k": "turn", "t": t, "ms": row.get("durationMs", 0)})
                continue
            msg = row.get("message") if isinstance(row.get("message"), dict) else None
            if not msg:
                continue
            if kind == "user":
                text = human_text(row)
                if text is not None:
                    items.append({"k": "cmd" if "<command-name>" in text else "user", "t": t, "text": clip(text, 20000)})
                    continue
                content = msg.get("content")
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in tools:
                            item = tools[b["tool_use_id"]]
                            item["ok"] = not b.get("is_error")
                            item["end"] = t
                            res = row.get("toolUseResult")
                            # Commands and file writes keep their structured result (stdout/stderr, the file before);
                            # everything else keeps the plain text Claude saw
                            if item["name"] in ("Bash", "Write") and isinstance(res, dict):
                                item["res"] = clip({k: res.get(k) for k in ("stdout", "stderr", "interrupted", "type", "originalFile") if k in res})
                            else:
                                item["result"] = clip(result_text(b.get("content")))
                elif (row.get("origin") or {}).get("kind") in ("task-notification", "peer") and isinstance(content, str):
                    items.append({"k": "note", "t": t, "text": clip(content, 600)})
                continue
            if kind == "assistant":
                for b in msg.get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and b.get("text", "").strip():
                        items.append({"k": "claude", "t": t, "text": clip(b["text"], 40000), "model": msg.get("model", "")})
                    elif b.get("type") == "tool_use":
                        item = {"k": "tool", "t": t, "id": b.get("id"), "name": b.get("name", ""), "input": clip(b.get("input"), 8000)}
                        tools[b.get("id")] = item
                        items.append(item)
    usage = usage_report_for_path(str(path))
    return {"id": sid, "title": title, "cwd": cwd, "items": items, "usage": usage}


def usage_report_for_path(path):
    with usage_lock:
        su = usage_cache.get(path)
        if not su:
            su = usage_cache[path] = SessionUsage(path)
        records = su.records()
    totals, models = summarise_usage(records)
    return {"totals": totals, "models": models, "prices_as_of": PRICES_AS_OF,
            "requests": [[round(r["t"], 3), r["in"] + r["out"] + r["w5"] + r["w1"] + r["read"], r["cost"], r["out"]] for r in records]}


# ---------------------------------------------------------------- terminals
def find_claude():
    if WINDOWS:  # the native installer makes claude.exe, npm makes claude.cmd (next to a "claude" file only Git Bash can run)
        found = shutil.which("claude.exe") or shutil.which("claude.cmd")
        places = (Path.home() / ".local/bin/claude.exe", Path(os.environ.get("APPDATA") or Path.home()) / "npm/claude.cmd")
    else:
        found = shutil.which("claude")
        places = (Path.home() / ".local/bin/claude", Path.home() / ".claude/local/claude", Path("/usr/local/bin/claude"), Path("/opt/homebrew/bin/claude"))
    if found:
        return found
    for p in places:
        if p.exists():
            return str(p)
    return None


def child_env(term_id):
    # The server may have been started from inside a Claude Code session; never pass that session's markers on
    env = clean_env()
    env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor", "ORBIT_TERM_ID": term_id})
    return env


class Terminal:
    def __init__(self, cwd, cols, rows, args):
        self.id = uuid.uuid4().hex[:12]
        self.cwd = cwd
        self.args = args
        self.created = time.time()
        self.buf = bytearray()
        self.clients = set()
        self.lock = threading.Lock()
        self.alive = True
        self.exit_code = None
        claude = find_claude()
        if not claude:
            raise RuntimeError("Could not find the claude command. Install Claude Code first.")
        env = child_env(self.id)
        self.con = None
        if WINDOWS:
            cols, rows = self._size(cols, rows)
            self.con = windows.PseudoConsole(windows.command_line(claude, args), cwd, env, cols, rows)
            self.pid = self.con.pid
        else:
            pid, fd = pty.fork()
            if pid == 0:  # child: become the user's own claude command
                try:
                    os.chdir(cwd)
                    os.execvpe(claude, [claude, *args], env)
                finally:
                    os._exit(127)
            self.pid, self.fd = pid, fd
            self.resize(cols, rows)
        threading.Thread(target=self._read_loop, daemon=True).start()

    def info(self):
        return {"id": self.id, "cwd": self.cwd, "args": self.args, "created": self.created, "alive": self.alive, "exit_code": self.exit_code}

    @staticmethod
    def _size(cols, rows):
        return max(20, min(int(cols), 500)), max(5, min(int(rows), 200))

    def resize(self, cols, rows):
        cols, rows = self._size(cols, rows)
        if self.con:
            self.con.resize(cols, rows)
            return
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def write(self, data):
        if not self.alive:
            return
        data = data.encode() if isinstance(data, str) else data
        try:
            self.con.write(data) if self.con else os.write(self.fd, data)
        except OSError:
            pass

    def _read_loop(self):
        while True:
            try:
                data = self.con.read() if self.con else os.read(self.fd, 65536)
            except OSError:
                data = b""
            if not data:
                break
            with self.lock:
                self.buf += data
                if len(self.buf) > SCROLLBACK_BYTES:
                    del self.buf[: len(self.buf) - SCROLLBACK_BYTES]
                clients = list(self.clients)
            for c in clients:
                c.send_binary(data)
        if self.con:
            self.con.exited.wait()
            self.exit_code = self.con.exit_code
            self.con.release()
        else:
            try:
                _, status = os.waitpid(self.pid, 0)
                self.exit_code = os.waitstatus_to_exitcode(status)
            except (ChildProcessError, OSError):
                pass
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.alive = False
        msg = json.dumps({"t": "exit", "code": self.exit_code})
        with self.lock:
            clients = list(self.clients)
        for c in clients:
            c.send_text(msg)
        broadcast({"id": 0, "kind": "terminal", "terminal": self.info()})

    def kill(self):
        if self.alive and self.con:
            self.con.kill()
        elif self.alive:
            try:
                os.killpg(os.getpgid(self.pid), signal.SIGHUP)
            except OSError:
                pass


terminals = {}
terminals_lock = threading.Lock()


# ---------------------------------------------------------------- running sessions
# Claude Code keeps one small file per running session in ~/.claude/sessions/<pid>.json (process id,
# session id, folder, name, busy/idle). Orbit reads them to list every running session on this computer.
# A session that sends no hook events (it started before Orbit was switched on) is followed through
# its saved conversation instead, so it still shows live.
SESSIONS_DIR = CLAUDE_DIR / "sessions"
FOLLOW_FILE = DATA_DIR / "followed.json"
LIVE_EVERY = 1.0
live = {}             # Claude session id -> running session info
live_lock = threading.Lock()
hooked = set()        # session ids that send hook events
last_hook = {}        # session id -> time of its latest hook event
followed = {}         # session id -> {"offset", "path", "tools", "cwd"} for sessions read from their transcript
follow_lock = threading.Lock()
saved_offsets = {}    # session id -> transcript offset already turned into events (survives restarts)


def note_hooked(event):
    if event.get("source") == "claude-code" and isinstance(event.get("session_id"), str):
        hooked.add(event["session_id"])
        last_hook[event["session_id"]] = event.get("received_at") or time.time()


def proc_is_claude(pid, proc_start):
    """True if pid is still the Claude process its session file describes, not a reused process id."""
    if WINDOWS:  # never os.kill(pid, 0) here: on Windows that sends Ctrl+C to the process
        name = windows.process_name(pid)
        return name is not None and any(k in name for k in ("claude", "node", "bun"))
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():  # Linux: the process start time must match the one Claude recorded
        try:
            return not proc_start or stat.read_text(encoding="utf-8").rsplit(")", 1)[1].split()[19] == str(proc_start)
        except (OSError, IndexError):
            return False
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "claude" in out


def scan_live():
    with terminals_lock:
        term_by_pid = {t.pid: t.id for t in terminals.values() if t.alive}
    found = {}
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            info = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(info, dict):
            continue
        sid, pid = info.get("sessionId"), info.get("pid")
        if not isinstance(sid, str) or not SESSION_ID_OK.match(sid) or not isinstance(pid, int) or isinstance(pid, bool):
            continue
        if not proc_is_claude(pid, info.get("procStart")):
            continue
        found[sid] = {"session_id": sid, "pid": pid, "cwd": str(info.get("cwd") or ""), "name": str(info.get("name") or ""),
                      "status": str(info.get("status") or ""), "started": (info.get("startedAt") or 0) / 1000,
                      "term_id": term_by_pid.get(pid), "hooks": sid in hooked}
    return found


def background_done(row):
    """The details of a "background command finished" notice Claude Code adds to the conversation, or None."""
    if row.get("type") != "user" or (row.get("origin") or {}).get("kind") != "task-notification":
        return None
    content = (row.get("message") or {}).get("content")
    if isinstance(content, list):
        content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    if not isinstance(content, str):
        return None
    tag = lambda name: (re.search(rf"<{name}>(.*?)</{name}>", content, re.S) or [None, ""])[1].strip()
    if not tag("tool-use-id"):
        return None
    return {"tool_use_id": tag("tool-use-id"), "task_id": tag("task-id"), "status": tag("status") or "completed",
            "summary": clip(tag("summary"), 600), "output_file": tag("output-file")}


def transcript_events(sid, cwd, path, rows, tools, hooked=False):
    """Hook-style events for transcript rows, so the dashboard treats a followed session like any other.

    For a session that sends hook events, only what hooks never report is taken from the transcript:
    the text Claude writes between steps."""
    base = {"session_id": sid, "cwd": cwd, "transcript_path": path, "source": "transcript"}
    out = []
    for row in rows:
        if row.get("isSidechain"):
            continue
        t = iso_time(row["timestamp"]) if row.get("timestamp") else time.time()
        if row.get("type") == "system" and row.get("subtype") == "turn_duration":
            if not hooked:
                out.append({**base, "hook_event_name": "Stop", "received_at": t})
            continue
        msg = row.get("message") if isinstance(row.get("message"), dict) else None
        if not msg:
            continue
        if row.get("type") == "assistant":
            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                    out.append({**base, "hook_event_name": "AssistantText", "text": clip(b["text"], 20000), "received_at": t})
        done = background_done(row)
        if done:
            out.append({**base, "hook_event_name": "BackgroundDone", **done, "received_at": t})
            continue
        if hooked:
            continue
        if row.get("type") == "user":
            text = human_text(row)
            if text is not None:
                out.append({**base, "hook_event_name": "UserPromptSubmit", "prompt": clip(text, 20000), "received_at": t})
                continue
            content = msg.get("content")
            for b in content if isinstance(content, list) else []:
                if not (isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in tools):
                    continue
                name, tool_input = tools.pop(b["tool_use_id"])
                res = row.get("toolUseResult")
                response = clip(res) if isinstance(res, dict) else clip(result_text(b.get("content")))
                out.append({**base, "hook_event_name": "PostToolUseFailure" if b.get("is_error") else "PostToolUse",
                            "tool_name": name, "tool_input": tool_input, "tool_use_id": b["tool_use_id"],
                            "tool_response": response, "received_at": t})
        elif row.get("type") == "assistant":
            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tools[b.get("id")] = (b.get("name", ""), clip(b.get("input"), 8000))
                    out.append({**base, "hook_event_name": "PreToolUse", "tool_name": b.get("name", ""),
                                "tool_input": clip(b.get("input"), 8000), "tool_use_id": b.get("id"), "received_at": t})
    return out


def last_turn_offset(path, size):
    """Where the latest prompt starts in a transcript, so following a session begins with its current turn.

    Looks back further and further (a long turn can fill megabytes); starts at the end if none is found."""
    window = EDGE
    while True:
        start = max(0, size - window)
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read(size - start)
        pos, found = 0, None
        for i, line in enumerate(data.split(b"\n")):
            if not (i == 0 and start) and b'"user"' in line:  # the first line of a chunk read mid-file is only partial
                try:
                    row = json.loads(line)
                except ValueError:
                    row = None
                if isinstance(row, dict) and human_text(row) is not None:
                    found = start + pos
            pos += len(line) + 1
        if found is not None:
            return found
        if start == 0 or window >= 32 * 1024 * 1024:
            return size
        window *= 4


def read_new(path, offset):
    """The complete rows added to a JSONL file since offset, and the new offset."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], offset
    if size < offset:  # rewritten from the start
        offset = 0
    if size == offset:
        return [], offset
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read(min(size - offset, 8 * 1024 * 1024))
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    return list(_lines(data[:end], False)), offset + end + 1


def agent_events(sid, cwd, f, register_only=False):
    """What sub-agents write between their steps, from <transcript>/subagents/agent-<id>.jsonl.

    Files already there when following starts are old, so they are read from their end."""
    out = []
    try:
        files = list((Path(f["path"]).with_suffix("") / "subagents").glob("agent-*.jsonl"))
    except OSError:
        return out
    for p in files:
        a = f["agents"].get(str(p))
        if a is None:
            try:
                start = p.stat().st_size if register_only else 0
            except OSError:
                continue
            a = f["agents"][str(p)] = {"offset": start, "id": p.stem[len("agent-"):], "type": ""}
        if not a["type"]:
            try:
                meta = json.loads(p.with_name(p.stem + ".meta.json").read_text(encoding="utf-8"))
                a["type"] = str(meta.get("agentType") or "") if isinstance(meta, dict) else ""
            except (OSError, ValueError):
                pass
        if register_only:
            continue
        rows, a["offset"] = read_new(str(p), a["offset"])
        for row in rows:
            msg = row.get("message") if isinstance(row.get("message"), dict) else None
            if row.get("type") != "assistant" or not msg:
                continue
            t = iso_time(row["timestamp"]) if row.get("timestamp") else time.time()
            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                    out.append({"session_id": sid, "cwd": cwd, "transcript_path": f["path"], "source": "transcript",
                                "hook_event_name": "AssistantText", "agent_id": a["id"], "agent_type": a["type"] or "sub-agent",
                                "text": clip(b["text"], 20000), "received_at": t})
    return out


def follow(sid, cwd, path=None):
    """Turn whatever is new in a session's transcript (and its sub-agents' transcripts) into events.

    Called by the session watcher and, for sessions that send hook events, just before each hook event
    is added, so the text Claude writes lands in the right order between steps."""
    with follow_lock:
        hooked_now = sid in hooked
        f = followed.get(sid)
        if f is None:
            found = Path(path) if path and os.path.isabs(path) and path.endswith(".jsonl") else find_transcript(sid)
            if not found:
                return
            offset = saved_offsets.get(sid)
            if offset is None:
                try:
                    size = found.stat().st_size
                except OSError:
                    size = 0  # a brand-new session: its transcript does not exist yet, so read it from the start
                if hooked_now:
                    offset = size  # hooks already reported everything before now
                else:
                    offset = last_turn_offset(found, size) if size else 0
                    add_event({"session_id": sid, "cwd": cwd, "transcript_path": str(found), "source": "transcript",
                               "hook_event_name": "SessionStart", "source_type": "follow"})
            f = followed[sid] = {"offset": offset, "path": str(found), "tools": {}, "cwd": cwd, "agents": {}}
            agent_events(sid, cwd, f, register_only=True)
        cwd = cwd or f["cwd"]
        rows, f["offset"] = read_new(f["path"], f["offset"])
        for event in transcript_events(sid, cwd, f["path"], rows, f["tools"], hooked_now) + agent_events(sid, cwd, f):
            if event["hook_event_name"] == "BackgroundDone":
                finish_background(event)
            add_event(event)
        saved_offsets[sid] = f["offset"]


def save_offsets():
    keep = {sid: off for sid, off in saved_offsets.items() if sid in live}
    try:
        FOLLOW_FILE.write_text(json.dumps(keep), encoding="utf-8")
    except OSError:
        pass


def watch_live():
    while True:
        try:
            now = scan_live()
            with live_lock:
                gone = [sid for sid in live if sid not in now]
                changed = now != live
                live.clear()
                live.update(now)
            for sid in gone:
                with follow_lock:
                    f = followed.pop(sid, None)
                # Sessions that send hook events report their own end
                if f is not None and sid not in hooked:
                    add_event({"session_id": sid, "source": "transcript", "hook_event_name": "SessionEnd"})
            before = dict(saved_offsets)
            for sid, info in now.items():
                # Give a new session a moment to send its first hook event before following its transcript
                if sid in hooked or time.time() - info["started"] > 5:
                    follow(sid, info["cwd"])
            # A session that sends hook events but has no session file (older Claude Code): keep reading its
            # transcript while it is active, so text written after its latest hook event still shows
            with follow_lock:
                extra = [(sid, f["cwd"]) for sid, f in followed.items()
                         if sid not in now and sid in hooked and time.time() - last_hook.get(sid, 0) < 1800]
            for sid, cwd in extra:
                follow(sid, cwd)
            if changed:
                broadcast({"id": 0, "kind": "live", "sessions": list(now.values())})
            if saved_offsets != before or gone:
                save_offsets()
        except Exception as e:  # noqa: BLE001 - keep watching whatever one bad file does
            print(f"Orbit: session watcher: {e}", file=sys.stderr, flush=True)
        time.sleep(LIVE_EVERY)


# ---------------------------------------------------------------- live command output
# While a command runs, Claude Code writes its output to a file in the session's "tasks" folder, next to
# the scratchpad folder that hook events name. Orbit tails that file so a long command shows its output
# as it prints, not only when it finishes. If the folder is not there, commands simply show at the end.
OUTPUT_TAIL = 16 * 1024
OUTPUT_EVERY = 0.5
running_cmds = {}     # tool use id -> {"sid", "tasks", "before", "start", "file", "size"}
outputs = {}          # tool use id -> {"text", "size"}: the latest output of a running command
output_lock = threading.Lock()


def note_command(event):
    ev, sid, tid = event.get("hook_event_name"), event.get("session_id"), event.get("tool_use_id")
    with output_lock:
        if ev == "PreToolUse" and event.get("tool_name") == "Bash" and isinstance(tid, str):
            inp = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
            sp = event.get("scratchpad_dir")
            if inp.get("run_in_background") or not isinstance(sp, str) or not os.path.isabs(sp):
                return
            tasks = Path(sp).parent / "tasks"
            try:
                before = {p.name for p in tasks.glob("*.output")}
            except OSError:
                before = set()
            running_cmds[tid] = {"sid": sid, "tasks": tasks, "before": before, "start": time.time(), "file": None, "size": -1}
        elif ev in ("PostToolUse", "PostToolUseFailure") and isinstance(tid, str):
            running_cmds.pop(tid, None)
            outputs.pop(tid, None)
            # A command sent to the background keeps writing to tasks/<id>.output until Claude Code reports it done
            res = event.get("tool_response") if isinstance(event.get("tool_response"), dict) else {}
            task, sp = res.get("backgroundTaskId"), event.get("scratchpad_dir")
            if isinstance(task, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task) and isinstance(sp, str) and os.path.isabs(sp):
                running_cmds[tid] = {"sid": sid, "tasks": None, "before": set(), "start": time.time(),
                                     "file": str(Path(sp).parent / "tasks" / f"{task}.output"), "size": -1, "bg": task}
        elif ev in ("Stop", "SessionEnd"):
            # Background commands outlive the turn that started them, not the session
            for t in [t for t, c in running_cmds.items() if c["sid"] == sid and (ev == "SessionEnd" or not c.get("bg"))]:
                running_cmds.pop(t, None)
                outputs.pop(t, None)


def output_tail(path):
    """The last part of a command's output file, starting at a whole line."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - OUTPUT_TAIL))
            data = f.read(OUTPUT_TAIL)
    except OSError:
        return None, 0
    text = data.decode("utf-8", "replace")
    if size > OUTPUT_TAIL:
        text = "…\n" + text.split("\n", 1)[-1]  # drop the cut-off first line
    return text, size


def finish_background(event):
    """A background command finished: stop following it and keep its final output with the event."""
    tid, path = event.get("tool_use_id"), event.get("output_file")
    with output_lock:
        c = running_cmds.pop(tid, None)
        outputs.pop(tid, None)
    if c and not path:
        path = c["file"]
    if isinstance(path, str) and os.path.isabs(path) and path.endswith(".output"):
        event["output"] = output_tail(path)[0]
    event.pop("output_file", None)


def watch_output():
    while True:
        try:
            with output_lock:
                items = sorted(running_cmds.items(), key=lambda x: x[1]["start"])
                claimed = {c["file"] for _, c in items if c["file"]}
            for tid, c in items:
                if time.time() - c["start"] > 6 * 3600:  # a command whose end was never reported
                    with output_lock:
                        running_cmds.pop(tid, None)
                        outputs.pop(tid, None)
                    continue
                if not c["file"]:
                    # The command's file is the oldest one that appeared in the folder after it started
                    try:
                        fresh = [p for p in c["tasks"].glob("*.output") if p.name not in c["before"] and str(p) not in claimed]
                        fresh.sort(key=lambda p: p.stat().st_mtime)
                    except OSError:
                        fresh = []
                    if not fresh:
                        continue
                    c["file"] = str(fresh[0])
                    claimed.add(c["file"])
                try:
                    size = os.path.getsize(c["file"])
                except OSError:
                    continue
                if size == c["size"]:
                    continue
                c["size"] = size
                text, size = output_tail(c["file"])
                if text is None:
                    continue
                with output_lock:
                    if tid not in running_cmds:
                        continue
                    outputs[tid] = {"text": text, "size": size}
                broadcast({"id": 0, "kind": "output", "session_id": c["sid"], "tool_use_id": tid, "text": text, "size": size})
        except Exception as e:  # noqa: BLE001 - keep watching whatever one file does
            print(f"Orbit: output watcher: {e}", file=sys.stderr, flush=True)
        time.sleep(OUTPUT_EVERY)


def wait_until_closed(info, timeout=8):
    for _ in range(int(timeout / 0.2)):
        if not proc_is_claude(info["pid"], None):
            return True
        time.sleep(0.2)
    return False


def close_session(info):
    """Close a running Claude session the way closing its window would; False if it could not be signalled."""
    if info.get("term_id"):
        with terminals_lock:
            t = terminals.get(info["term_id"])
        if t:
            t.kill()
            return True
    try:
        os.kill(info["pid"], signal.SIGTERM)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- websocket
class WebSocket:
    """Minimal RFC 6455 server side: text/binary frames, ping/pong, close."""

    def __init__(self, handler):
        self.r, self.w = handler.rfile, handler.wfile
        self.send_lock = threading.Lock()
        self.open = True

    def _send(self, opcode, payload):
        if not self.open:
            return
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", 0x80 | opcode, n)
        elif n < 65536:
            header = struct.pack("!BBH", 0x80 | opcode, 126, n)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 127, n)
        try:
            with self.send_lock:
                self.w.write(header + payload)
                self.w.flush()
        except OSError:
            self.open = False

    def send_text(self, text):
        self._send(0x1, text.encode())

    def send_binary(self, data):
        self._send(0x2, bytes(data))

    def _read_exact(self, n):
        data = self.r.read(n)
        if data is None or len(data) < n:
            raise ConnectionError
        return data

    def recv(self):
        """Return (opcode, payload) for the next complete message; None when closed."""
        message, first_op = b"", None
        while True:
            b1, b2 = self._read_exact(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            if length > 4 * 1024 * 1024:
                raise ConnectionError
            mask = self._read_exact(4) if b2 & 0x80 else b"\0\0\0\0"
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(self._read_exact(length)))
            if opcode == 0x8:
                self._send(0x8, b"")
                self.open = False
                return None
            if opcode == 0x9:
                self._send(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode != 0x0:
                first_op = opcode
            message += payload
            if fin:
                return first_op, message


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _same_site(self):
        if self.headers.get("Host", "") not in ALLOWED_HOSTS:
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in ALLOWED_ORIGINS:
            return False
        if self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
            return False
        return True

    def _has_key(self):
        supplied = self.headers.get(KEY_HEADER, "")
        if not supplied:
            for part in self.headers.get("Cookie", "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == COOKIE:
                    supplied = value
        return bool(supplied) and hmac.compare_digest(supplied, TOKEN)

    def _send(self, code, body, content_type="application/json", extra=None):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj))

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > 8 * 1024 * 1024:
            raise ValueError("too large")
        payload = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("expected an object")
        return payload

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        if path == "/health":
            self._json(200, {"ok": True, "app": APP, "version": VERSION, "terminals": HAS_PTY})
            return
        if not self._same_site():
            self._json(403, {"error": "forbidden"})
            return
        if path == "/" and "k" in parse_qs(url.query):
            key = parse_qs(url.query)["k"][0]
            if hmac.compare_digest(key, TOKEN):
                self.send_response(303)
                self.send_header("Location", "/" + (f"#{url.fragment}" if url.fragment else ""))
                self.send_header("Set-Cookie", f"{COOKIE}={TOKEN}; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        if not self._has_key():
            if path in ("/", "/index.html"):
                self._send(401, LOCKED_PAGE, "text/html; charset=utf-8")
            else:
                self._json(401, {"error": "missing access key"})
            return
        if path in ("/", "/index.html"):
            self._send(200, DASHBOARD.read_bytes(), "text/html; charset=utf-8")
        elif path.startswith("/vendor/"):
            f = (VENDOR_DIR / path[len("/vendor/"):]).resolve()
            if f.parent == VENDOR_DIR.resolve() and f.is_file() and f.suffix in VENDOR_TYPES:
                self._send(200, f.read_bytes(), VENDOR_TYPES[f.suffix], {"Cache-Control": "max-age=86400"})
            else:
                self._json(404, {"error": "not found"})
        elif path == "/events":
            try:
                after = int(parse_qs(url.query).get("after", ["0"])[0])
            except ValueError:
                after = 0
            with events_lock:
                self._json(200, [e for e in events if e.get("id", 0) > after] if after else events)
        elif path == "/stream":
            self._stream()
        elif path == "/api/terms":
            with terminals_lock:
                self._json(200, {"supported": HAS_PTY, "claude": bool(find_claude()), "terms": [t.info() for t in terminals.values()]})
        elif path == "/api/history":
            self._json(200, {"sessions": list_history()})
        elif path == "/api/search":
            self._json(200, {"ids": search_history(parse_qs(url.query).get("q", [""])[0])})
        elif path == "/api/chat":
            chat = read_chat(parse_qs(url.query).get("session", [""])[0])
            self._json(200 if chat else 404, chat or {"error": "conversation not found"})
        elif path == "/api/status":
            self._json(200, plan_status)
        elif path == "/api/output":
            with output_lock:
                self._json(200, outputs.get(parse_qs(url.query).get("tool", [""])[0]) or {})
        elif path == "/api/live":
            with live_lock:
                self._json(200, {"sessions": list(live.values())})
        elif path == "/api/usage":
            self._json(200, usage_report(parse_qs(url.query).get("session", ["*"])[0]))
        elif path == "/api/ls":
            self._list_dir(parse_qs(url.query).get("path", [str(Path.home())])[0])
        elif path.startswith("/term/") and path.endswith("/ws"):
            self._terminal_socket(path.split("/")[2])
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._same_site() or not self._has_key():
            self._json(403, {"error": "forbidden"})
            return
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self._json(415, {"error": "json only"})
            return
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "bad json"})
            return
        if path == "/event":
            note_hooked(body)
            if body.get("source") == "claude-code":
                note_command(body)
            sid = body.get("session_id")
            if body.get("source") == "claude-code" and isinstance(sid, str) and SESSION_ID_OK.match(sid):
                try:
                    follow(sid, str(body.get("cwd") or ""), body.get("transcript_path"))
                except Exception as e:  # noqa: BLE001 - the hook event itself must always get through
                    print(f"Orbit: transcript read: {e}", file=sys.stderr, flush=True)
            event = add_event(body)
            self._json(200, {"id": event["id"]})
        elif path == "/clear":
            with events_lock:
                events.clear()
                LOG_FILE.write_text("", encoding="utf-8")
            with usage_lock:
                transcripts.clear()
                usage_cache.clear()
            self._json(200, {"ok": True})
        elif path == "/statusline":
            self._statusline(body)
        elif path == "/api/terms":
            self._create_terminal(body)
        elif path.startswith("/api/terms/") and path.endswith("/kill"):
            with terminals_lock:
                t = terminals.get(path.split("/")[3])
            if t:
                t.kill()
            self._json(200, {"ok": bool(t)})
        elif path.startswith("/api/live/") and path.count("/") == 4 and path.rsplit("/", 1)[1] in ("stop", "move"):
            self._live_action(path.split("/")[3], path.rsplit("/", 1)[1], body)
        elif path.startswith("/api/terms/") and path.endswith("/forget"):
            with terminals_lock:
                t = terminals.get(path.split("/")[3])
                if t and not t.alive:
                    del terminals[t.id]
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    # -- helpers
    def _statusline(self, body):
        sid = body.get("session_id")
        if not isinstance(sid, str):
            self._json(400, {"error": "no session"})
            return
        note_transcript(body)
        entry = {"t": time.time(), **{k: body.get(k) for k in ("cost", "context_window", "rate_limits", "model", "fast_mode")}}
        changed = {k: v for k, v in entry.items() if k != "t"} != {k: v for k, v in plan_status.get(sid, {}).items() if k != "t"}
        plan_status[sid] = entry
        if changed:
            broadcast({"id": 0, "kind": "status", "session_id": sid, "status": entry})
            try:
                STATUS_FILE.write_text(json.dumps(plan_status), encoding="utf-8")
            except OSError:
                pass
        self._json(200, {"ok": True})

    def _live_action(self, sid, action, body):
        with live_lock:
            info = dict(live.get(sid) or {})
        if not info:
            self._json(404, {"error": "That session is no longer running."})
            return
        if action == "move" and info.get("term_id"):
            with terminals_lock:
                t = terminals.get(info["term_id"])
            if t:
                self._json(200, t.info())
                return
        if action == "move" and not HAS_PTY:
            self._json(501, {"error": "Built-in terminals need macOS, Linux or Windows 10 (1809) or later."})
            return
        if action == "move" and info["status"] == "busy" and not body.get("force"):
            self._json(409, {"error": "Claude is working in that session right now.", "busy": True})
            return
        if not close_session(info):
            self._json(500, {"error": "Could not close that session."})
            return
        if action == "stop":
            self._json(200, {"ok": True})
            return
        # Only one copy of a conversation may run at a time: wait for the old one to close, then resume it here
        if not wait_until_closed(info):
            self._json(500, {"error": "The session did not close, so it was not moved. Close it in its own terminal and try again."})
            return
        cwd = info["cwd"] if os.path.isdir(info["cwd"]) else str(Path.home())
        # A session closed before anything was said has no saved conversation to resume: start fresh in its folder
        args = ["--resume", sid] if find_transcript(sid) else []
        try:
            t = Terminal(cwd, body.get("cols", 110), body.get("rows", 32), args)
        except Exception as e:  # noqa: BLE001 - reported to the user
            self._json(500, {"error": str(e)})
            return
        with terminals_lock:
            terminals[t.id] = t
        broadcast({"id": 0, "kind": "terminal", "terminal": t.info()})
        self._json(200, t.info())

    def _list_dir(self, raw):
        p = Path(os.path.expanduser(raw or "~")).resolve()
        if not p.is_dir():
            self._json(404, {"error": "not a folder", "path": str(p)})
            return
        try:
            dirs = sorted((c.name for c in p.iterdir() if c.is_dir() and not c.name.startswith(".")), key=str.lower)[:300]
        except PermissionError:
            dirs = []
        self._json(200, {"path": str(p), "parent": str(p.parent) if p.parent != p else None, "dirs": dirs})

    def _create_terminal(self, body):
        if not HAS_PTY:
            self._json(501, {"error": "Built-in terminals need macOS, Linux or Windows 10 (1809) or later."})
            return
        cwd = Path(os.path.expanduser(str(body.get("cwd") or "~"))).resolve()
        if not cwd.is_dir():
            self._json(400, {"error": f"Folder not found: {cwd}"})
            return
        args = []
        if body.get("mode") == "continue":
            args = ["--continue"]
        elif body.get("mode") == "resume" and isinstance(body.get("session"), str) and body["session"].replace("-", "").isalnum():
            args = ["--resume", body["session"]]
        try:
            t = Terminal(str(cwd), body.get("cols", 100), body.get("rows", 30), args)
        except Exception as e:  # noqa: BLE001 - reported to the user
            self._json(500, {"error": str(e)})
            return
        with terminals_lock:
            terminals[t.id] = t
        broadcast({"id": 0, "kind": "terminal", "terminal": t.info()})
        self._json(200, t.info())

    def _terminal_socket(self, term_id):
        with terminals_lock:
            t = terminals.get(term_id)
        key = self.headers.get("Sec-WebSocket-Key")
        if not t or not key or self.headers.get("Upgrade", "").lower() != "websocket":
            self._json(404, {"error": "no such terminal"})
            return
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        ws = WebSocket(self)
        with t.lock:
            if t.buf:
                ws.send_binary(bytes(t.buf))
            t.clients.add(ws)
        if not t.alive:
            ws.send_text(json.dumps({"t": "exit", "code": t.exit_code}))
        try:
            while ws.open:
                msg = ws.recv()
                if msg is None:
                    break
                try:
                    m = json.loads(msg[1])
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if m.get("t") == "in" and isinstance(m.get("d"), str):
                    t.write(m["d"])
                elif m.get("t") == "resize":
                    t.resize(m.get("cols", 100), m.get("rows", 30))
        except (ConnectionError, OSError):
            pass
        finally:
            ws.open = False
            with t.lock:
                t.clients.discard(ws)

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.close_connection = True
        q = queue.Queue()
        with subscribers_lock:
            subscribers.append(q)
        # A heartbeat the page can see (comments are invisible to it): it carries this server's start id,
        # so the page notices a restart, and the newest event id, so it can fetch anything it missed
        beat = lambda kind: f"data: {json.dumps({'id': 0, 'kind': kind, 'boot': BOOT, 'last': next_id - 1})}\n\n".encode()
        try:
            self.wfile.write(beat("hello"))
            self.wfile.flush()
            while True:
                try:
                    event = q.get(timeout=HEARTBEAT)
                    self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(beat("ping"))
                self.wfile.flush()
        except (OSError, ValueError):
            pass
        finally:
            with subscribers_lock:
                subscribers.remove(q)


LOCKED_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Orbit</title><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#05070e;color:#e8ecf8;font:15px/1.6 system-ui,sans-serif;padding:16px;box-sizing:border-box}
.box{max-width:460px;text-align:center}.orb{width:72px;height:72px;margin:0 auto 22px;border-radius:50%;background:radial-gradient(circle at 35% 30%,#fff,#8ab4ff 40%,#7b5cff 78%);box-shadow:0 0 40px #8ab4ff}
code{background:#141a2e;padding:2px 8px;border-radius:6px}p{color:#8c96b3}</style></head>
<body><div class="box"><div class="orb"></div><h1>Open it from Claude Code</h1>
<p>For your safety Orbit only opens through its private link. In Claude Code, type <code>/orbit</code>, or run <code>orbit</code> in a terminal, and it will open here, unlocked.</p></div></body></html>"""


def main():
    global TOKEN
    for stream in (sys.stdout, sys.stderr):  # the log is UTF-8 everywhere (Windows would use the local code page)
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    TOKEN = load_token()
    load_prices()
    try:
        plan_status.update(json.loads(STATUS_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        print(f"Orbit: port {PORT} is busy ({e}). Set ORBIT_PORT to use another port.", file=sys.stderr)
        sys.exit(1)
    server.daemon_threads = True
    load_history()
    try:
        saved_offsets.update({k: int(v) for k, v in json.loads(FOLLOW_FILE.read_text(encoding="utf-8")).items()})
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    threading.Thread(target=watch_live, daemon=True).start()
    threading.Thread(target=watch_output, daemon=True).start()
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    print(f"Orbit {VERSION} running at http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for t in list(terminals.values()):
            t.kill()
        try:
            if PID_FILE.read_text(encoding="utf-8") == str(os.getpid()):
                PID_FILE.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
