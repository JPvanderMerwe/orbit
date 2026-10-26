# Orbit for Claude Code

**See everything Claude Code does.** A live, animated map of every file Claude reads, command it runs and change it makes, with a built-in workspace, full-screen step details, session replay, every saved conversation, cost and plan-usage tracking, and shareable reports.

## Install

```
/plugin marketplace add JPvanderMerwe/orbit
/plugin install orbit@orbit
```

Restart Claude Code, then type `/orbit`.

## Commands

| Command | What it does |
|---|---|
| `/orbit` | Open Orbit on the session you are in |
| `/orbit all` | Open the start screen with every session |
| `/orbit chats` | Open every saved conversation |
| `/orbit new [folder]` | Start a new Claude session in the workspace |
| `/orbit statusline` | Show your Pro or Max plan limits in Orbit (keeps any status line you have) |
| `/orbit statusline off` | Put your previous status line back |
| `/orbit desktop` | Record every tool Claude Desktop chats use on this computer (type it here in Claude Code, then restart Claude Desktop) |
| `/orbit desktop off` | Undo it |
| `/orbit status` | Say whether Orbit is running |
| `/orbit stop` | Stop Orbit (also closes workspace sessions) |

## Requirements

Claude Code, and Python 3.9 or newer (standard library only), on macOS, Linux, or Windows 10 (version 1809) or later. On Windows, Orbit runs through Git Bash, which Claude Code uses there too, and finds Python as `py`, `python` or `python3`.

## Privacy

Everything stays on your computer: Orbit listens on `127.0.0.1` only, is locked with a private access key in `~/.orbit/token`, and sends nothing anywhere. Costs are estimates at Anthropic API list prices; Pro and Max plans are not billed per token.

Full documentation, the standalone `orbit` command and settings: see the [main README](../../README.md).

## Uninstall

```
/plugin uninstall orbit@orbit
```

Then delete `~/.orbit` to remove recorded activity and the access key. If you used `/orbit statusline`, run `/orbit statusline off` first.

Orbit is an independent community project, not made or endorsed by Anthropic.
