---
name: orbit
description: Open Orbit, a live animated view of Claude Code - see every file Claude reads, command it runs and change it makes, replay past sessions, browse every saved conversation, track tokens, cost and plan limits, export reports, and start new Claude sessions in a visual workspace. Use when the user asks to open, show, watch, replay or visualise Claude's activity, cost or past conversations, to show plan usage, to start a visual Claude workspace, or to check or stop Orbit.
argument-hint: "[all | chats | new [folder] | statusline [off] | desktop [off] | status | stop]"
allowed-tools: Bash(sh:*)
---

Run exactly one command with the Bash tool, chosen from the user's argument:

| Argument | Command |
|---|---|
| (none) | `sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" cli.py session ${CLAUDE_SESSION_ID}` |
| `all` | `sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" cli.py` |
| `chats` | `sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" cli.py chats` |
| `new` | `sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" cli.py new` |
| `new <folder>` | `sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" cli.py new "<folder>"` |
| `statusline` | `sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" cli.py install-statusline` |
| `statusline off` | `sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" cli.py uninstall-statusline` |
| `desktop` | `sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" cli.py install-desktop` |
| `desktop off` | `sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" cli.py uninstall-desktop` |
| `status` | `sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" cli.py status` |
| `stop` | `sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" cli.py stop` |

The user's argument: $ARGUMENTS

Then reply in one or two short lines:
- (none): say Orbit opened in their browser on this session's workspace. If the command said the conversation now continues in Orbit's workspace, tell them to type /exit here to close this terminal, so the conversation is only open in one place.
- `all`: say Orbit opened in their browser on all sessions.
- `chats`: say Orbit opened on their saved conversations.
- `new`: say the launcher opened in their browser, where they pick a folder and press Launch Claude.
- `statusline`, `statusline off`, `desktop`, `desktop off`, `status` or `stop`: repeat what the command printed.
- If the command printed a private link instead of opening the browser, give them that link and say it is private to them.

Do nothing else.
