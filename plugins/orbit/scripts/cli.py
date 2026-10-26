#!/usr/bin/env python3
"""Orbit - see everything Claude Code does.

    orbit                     start Orbit if needed and open it in the browser
    orbit session <id>        open one Claude session in Orbit's workspace (resumes it there if it runs elsewhere)
    orbit new [folder]        open it and start a new Claude session in a workspace
    orbit chats               open every saved Claude Code conversation
    orbit url                 print the private link without opening a browser
    orbit status              say whether it is running
    orbit stop                stop Orbit (closes workspace terminals)
    orbit install-hooks       connect Orbit to Claude Code without the plugin
    orbit uninstall-hooks     remove the hooks that install-hooks added
    orbit install-statusline  show your Pro/Max plan usage in Orbit (keeps any status line you have)
    orbit uninstall-statusline  put your previous status line back
    orbit install-desktop     add Orbit to Claude Desktop: "open Orbit" in a chat opens it, and every tool
                              Claude Desktop uses on this computer shows in it [--config <file>]
    orbit uninstall-desktop   undo install-desktop [--config <file>]
    orbit version             print the version

Inside Claude Code the plugin runs the same commands through /orbit.
"""
import json
import os
import shutil
import signal
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook import DATA_DIR, KEY_HEADER, URL, access_key, migrate_old_data, server_up, start_server, upgrade_server  # noqa: E402

VERSION = "3.5.0"
HOOK_SCRIPT = Path(__file__).resolve().parent / "hook.py"
# Claude Code runs hook and status line commands in a shell (Git Bash on Windows), which reads C:/x/y fine
PYTHON = Path(sys.executable).as_posix()
STATUS_SCRIPT = Path(__file__).resolve().parent / "statusline.py"
NEXT_FILE = DATA_DIR / "statusline-next.json"
HOOK_EVENTS = (
    "SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PermissionRequest", "PostToolUse",
    "PostToolUseFailure", "Notification", "SubagentStart", "SubagentStop", "Stop",
)
WITH_MATCHER = {"PreToolUse", "PermissionRequest", "PostToolUse", "PostToolUseFailure"}
# Added to every hook command install-hooks writes, so uninstall-hooks removes only Orbit's own
MARKER = "--orbit-standalone"
MARKER_SL = "--orbit-statusline"
MARKER_DESKTOP = "--orbit-desktop"
MARKER_OPEN = "--orbit-open"  # Orbit's own entry in Claude Desktop, whose tool opens Orbit
PROXY_SCRIPT = Path(__file__).resolve().parent / "mcp_proxy.py"


def ensure_running():
    if server_up():
        upgrade_server()  # an older Orbit still running after an update is replaced (unless terminals are open)
        if server_up():
            return True
    start_server(force=True)
    for _ in range(40):
        time.sleep(0.1)
        if server_up(0.1) and access_key():
            return True
    print(f"Could not start Orbit. See {DATA_DIR / 'server.log'}")
    return False


def private_link(fragment=""):
    return f"{URL}/?k={access_key()}" + (f"#{fragment}" if fragment else "")


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"{URL}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json", KEY_HEADER: access_key()})
    with urllib.request.urlopen(request, timeout=5) as r:
        return json.loads(r.read())


def session_cwd(sid):
    """The folder a Claude session runs in, read from its saved transcript."""
    for path in (claude_dir() / "projects").glob(f"*/{sid}.jsonl"):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and row.get("cwd"):
                        return row["cwd"]
        except OSError:
            pass
    return os.getcwd()


def workspace_for(sid):
    """Orbit's workspace terminal for a session, as (terminal id, newly resumed), or None if there can't be one.

    Reuses the terminal the session already runs in (or one an earlier /orbit resumed it in);
    otherwise starts a new one that resumes the conversation."""
    info = api("GET", "/api/terms")
    if not info.get("supported") or not info.get("claude"):
        return None
    alive = {t["id"]: t for t in info.get("terms", []) if t.get("alive")}
    here = os.environ.get("ORBIT_TERM_ID")
    if here in alive:
        return here, False
    for t in alive.values():
        if t.get("args") == ["--resume", sid]:
            return t["id"], False
    t = api("POST", "/api/terms", {"cwd": session_cwd(sid), "mode": "resume", "session": sid, "cols": 110, "rows": 32})
    return t["id"], True


# ---------------------------------------------------------------- standalone hooks
def claude_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def plugin_installed():
    try:
        plugins = json.loads((claude_dir() / "plugins" / "installed_plugins.json").read_text(encoding="utf-8")).get("plugins", {})
    except (OSError, ValueError):
        return False
    return any(name.split("@")[0] == "orbit" for name in plugins)


def load_settings(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        print(f"{path} is not valid JSON, so it was left untouched. Fix it and try again.")
        sys.exit(1)
    if not isinstance(data, dict):
        print(f"{path} does not hold a settings object, so it was left untouched.")
        sys.exit(1)
    return data


def save_settings(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(f"{path.name}.orbit-backup-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
        print(f"Backed up your settings to {backup}")
    tmp = path.with_suffix(".orbit-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def strip_orbit(hooks):
    """Remove every hook entry install-hooks added; returns how many were removed."""
    removed = 0
    for event in list(hooks):
        groups = hooks[event] if isinstance(hooks[event], list) else []
        kept_groups = []
        for group in groups:
            inner = group.get("hooks", []) if isinstance(group, dict) else []
            kept = [h for h in inner if MARKER not in str(h.get("command", ""))]
            removed += len(inner) - len(kept)
            if kept or not inner:
                kept_groups.append({**group, "hooks": kept} if inner else group)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    return removed


def install_hooks(force):
    if plugin_installed() and not force:
        print("The Orbit plugin is already installed in Claude Code, so its hooks are already active.\n"
              "Adding them again would record every action twice. Run with --force to do it anyway.")
        return
    path = claude_dir() / "settings.json"
    settings = load_settings(path)
    hooks = settings.setdefault("hooks", {})
    strip_orbit(hooks)
    command = f'"{PYTHON}" "{HOOK_SCRIPT.as_posix()}" {MARKER}'
    for event in HOOK_EVENTS:
        entry = {"hooks": [{"type": "command", "command": command, "timeout": 5}]}
        if event in WITH_MATCHER:
            entry = {"matcher": "*", **entry}
        hooks.setdefault(event, []).append(entry)
    save_settings(path, settings)
    print(f"Orbit is connected to Claude Code ({path}).\nRestart any open Claude Code sessions, then run: orbit")


def uninstall_hooks():
    path = claude_dir() / "settings.json"
    settings = load_settings(path)
    removed = strip_orbit(settings.get("hooks", {}))
    if not settings.get("hooks"):
        settings.pop("hooks", None)
    if not removed:
        print("No Orbit hooks found in your Claude Code settings. Nothing changed.")
        return
    save_settings(path, settings)
    print(f"Removed {removed} Orbit hooks from {path}.")


def install_statusline():
    path = claude_dir() / "settings.json"
    settings = load_settings(path)
    current = settings.get("statusLine") or {}
    # Copied to a fixed place so plugin updates (which move the plugin folder) never break it
    bin_dir = DATA_DIR / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("statusline.py", "hook.py"):
        shutil.copy2(STATUS_SCRIPT.parent / name, bin_dir / name)
    command = f'"{PYTHON}" "{(bin_dir / "statusline.py").as_posix()}"'
    if MARKER_SL in str(current.get("command", "")):
        print("Orbit's status line is already installed (its script was refreshed).")
        return
    if current.get("command"):
        NEXT_FILE.write_text(json.dumps({"command": current["command"], "original": current}), encoding="utf-8")
        print("Your existing status line is kept: Orbit passes the same data to it and shows its output.")
    else:
        NEXT_FILE.unlink(missing_ok=True)
    settings["statusLine"] = {"type": "command", "command": f"{command} {MARKER_SL}"}
    save_settings(path, settings)
    print("Done. Plan usage (5-hour and weekly limits) appears in Orbit after Claude's next reply.\n"
          "It is only available on Claude Pro and Max plans.")


def uninstall_statusline():
    path = claude_dir() / "settings.json"
    settings = load_settings(path)
    if MARKER_SL not in str((settings.get("statusLine") or {}).get("command", "")):
        print("Orbit's status line is not installed. Nothing changed.")
        return
    try:
        original = json.loads(NEXT_FILE.read_text(encoding="utf-8")).get("original")
    except (OSError, ValueError):
        original = None
    if original:
        settings["statusLine"] = original
    else:
        settings.pop("statusLine", None)
    save_settings(path, settings)
    NEXT_FILE.unlink(missing_ok=True)
    print("Removed Orbit's status line" + (" and put your previous one back." if original else "."))


# ---------------------------------------------------------------- Claude Desktop
def desktop_config(args):
    """Claude Desktop's settings file: the one given with --config, else where the app keeps it on this system."""
    if "--config" in args and args.index("--config") + 1 < len(args):
        return Path(os.path.expanduser(args[args.index("--config") + 1]))
    if sys.platform == "darwin":
        places = [Path.home() / "Library/Application Support/Claude"]
    elif os.name == "nt":
        places = [Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming") / "Claude"]
    else:
        places = [Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / name for name in ("Claude", "claude-desktop")]
    for folder in places:
        if folder.is_dir():
            return folder / "claude_desktop_config.json"
    return None


def install_desktop(args):
    path = desktop_config(args)
    if not path:
        print("Claude Desktop's settings folder was not found. If Claude Desktop is installed, open it once, or\n"
              "give its settings file: orbit install-desktop --config <path to claude_desktop_config.json>")
        return
    settings = load_settings(path)
    servers = settings.get("mcpServers") if isinstance(settings.get("mcpServers"), dict) else {}
    # A fixed copy, so plugin updates (which move the plugin folder) never break Claude Desktop's settings
    bin_dir = DATA_DIR / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("mcp_proxy.py", "desktop_mcp.py", "hook.py"):
        shutil.copy2(PROXY_SCRIPT.parent / name, bin_dir / name)
    (bin_dir / "scripts.json").write_text(json.dumps({"scripts": str(PROXY_SCRIPT.parent)}), encoding="utf-8")
    added, already, skipped = [], [], []
    for name, entry in servers.items():
        if isinstance(entry, dict) and MARKER_OPEN in (entry.get("args") or []):
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("command"), str):
            skipped.append(name)  # a remote server (a web address), which runs elsewhere
        elif MARKER_DESKTOP in (entry.get("args") or []):
            already.append(name)
        elif any(str(a).replace("\\", "/").endswith("claude-desktop/mcp_server.py") for a in entry.get("args") or []):
            continue  # Orbit's own show_step server already reports to Orbit
        else:
            original = [entry["command"], *[str(a) for a in entry.get("args") or []]]
            entry["command"] = PYTHON
            entry["args"] = [(bin_dir / "mcp_proxy.py").as_posix(), MARKER_DESKTOP, name, "--", *original]
            added.append(name)
    # Orbit's own entry, whose open_orbit tool opens Orbit when the user asks in a chat
    own = next((n for n, e in servers.items() if isinstance(e, dict) and MARKER_OPEN in (e.get("args") or [])), None)
    own_entry = {"command": PYTHON, "args": [(bin_dir / "desktop_mcp.py").as_posix(), MARKER_OPEN]}
    own_changed = not own or servers[own] != own_entry
    if own:
        servers[own] = own_entry
    else:
        settings["mcpServers"] = servers = {("orbit" if "orbit" not in servers else "orbit-viewer"): own_entry, **servers}
    if added or own_changed:
        save_settings(path, settings)
    print("In Claude Desktop, say \"open Orbit\" in any chat to open it.")
    if added:
        print(f"Orbit records every tool Claude Desktop uses through: {', '.join(added)}.")
    if already:
        print(f"Already recorded: {', '.join(already)} (Orbit's copy was refreshed).")
    if skipped:
        print(f"Not recorded (remote servers, they run elsewhere): {', '.join(skipped)}.")
    if not added and not already:
        print("Claude Desktop has no tools on this computer yet, so there is nothing to record. Run this again after\n"
              "adding one (Claude Desktop: Settings > Developer > Edit Config).")
    if added or own_changed:
        print("Quit Claude Desktop completely and open it again for this to take effect.")
    print("To undo: orbit uninstall-desktop")


def uninstall_desktop(args):
    path = desktop_config(args)
    settings = load_settings(path) if path else {}
    servers = settings.get("mcpServers") if isinstance(settings.get("mcpServers"), dict) else {}
    restored = []
    for name in [n for n, e in servers.items() if isinstance(e, dict) and MARKER_OPEN in (e.get("args") or [])]:
        del servers[name]
        restored.append(name)
    for name, entry in servers.items():
        a = entry.get("args") if isinstance(entry, dict) else None
        if isinstance(a, list) and MARKER_DESKTOP in a and "--" in a:
            original = a[a.index("--") + 1:]
            if original:
                entry["command"], entry["args"] = original[0], original[1:]
                restored.append(name)
    if not restored:
        print("Orbit is not set up in Claude Desktop. Nothing changed.")
        return
    save_settings(path, settings)
    print("Orbit is removed from Claude Desktop. Quit and reopen Claude Desktop to apply it.")


# ---------------------------------------------------------------- main
def main():
    args = sys.argv[1:]
    action = args[0] if args else "open"
    if action in ("-h", "--help", "help"):
        print(__doc__.strip())
        return
    if action in ("-V", "--version", "version"):
        print(f"Orbit {VERSION}")
        return
    if action == "install-hooks":
        install_hooks("--force" in args)
        return
    if action == "uninstall-hooks":
        uninstall_hooks()
        return
    if action == "install-statusline":
        install_statusline()
        return
    if action == "uninstall-statusline":
        uninstall_statusline()
        return
    if action == "install-desktop":
        install_desktop(args)
        return
    if action == "uninstall-desktop":
        uninstall_desktop(args)
        return
    migrate_old_data()
    if action == "status":
        print(f"Orbit is running at {URL}" if server_up() else "Orbit is not running.")
        return
    if action == "stop":
        try:
            os.kill(int((DATA_DIR / "server.pid").read_text(encoding="utf-8")), signal.SIGTERM)
            print("Orbit stopped.")
        except (OSError, ValueError):
            print("Orbit is not running.")
        return
    if action not in ("open", "session", "new", "url", "chats"):
        print(f"Unknown command: {action}\n\n{__doc__.strip()}")
        sys.exit(2)
    if not ensure_running():
        sys.exit(1)
    fragment = ""
    moved = False
    if action == "session" and len(args) > 1:
        try:
            ws = workspace_for(args[1])
        except (OSError, ValueError, KeyError):
            ws = None
        if ws:
            fragment, moved = "/term/" + ws[0], ws[1]
        else:  # no built-in terminals here (e.g. Windows): show the session's map instead
            fragment = "/s/" + quote(args[1], safe="")
    elif action == "new":
        folder = os.path.abspath(os.path.expanduser(args[1])) if len(args) > 1 else os.getcwd()
        fragment = "/new/" + quote(folder, safe="")
    elif action == "chats":
        fragment = "/chats"
    link = private_link(fragment)
    if action == "url":
        print(link)
        return
    opened = webbrowser.open(link)
    print(f"Orbit opened at {URL}" if opened else f"Open this private link in your browser: {link}")
    if moved:
        print("This conversation now continues in Orbit's workspace. Close this terminal (type /exit) "
              "so the conversation is only open in one place.")


if __name__ == "__main__":
    main()
