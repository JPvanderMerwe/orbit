# Orbit

**See everything Claude Code does.**

Orbit is a live, animated window into Claude Code. Every file Claude reads, every command it runs and every change it makes appears on a map in your browser the moment it happens. You can run Claude right inside Orbit, replay any session, read any past conversation, and see what each prompt cost.

Everything runs on your own computer. Nothing is sent anywhere.

## What you get

- **Live map.** Claude sits at the centre and your files, the terminal, web, search and MCP tools orbit it. Each action fires a beam of light. Edits spark, failures flash red, and sub-agents appear as their own satellites.
- **Every step in full detail.** Click any step to see it. Double-click, or press **Expand**, to open it full screen. Code changes type themselves in as a before-and-after diff with line numbers. Commands play out in a terminal window, the command typed first and then its output. A side list shows everything that happened to the same file. Turn on **Follow live** and each new step opens there as Claude works.
- **Files tab.** Every file read or changed, with lines added and removed. Click one to step through its full change history.
- **Search and filters.** Find any step by text, or show only edits, reads, commands, searches, web, agents, MCP, prompts or failures. Press `/` to search.
- **Replay.** Play any session back on the map, at 1× to 30× speed, with a scrub bar.
- **Cost and plan usage.** A rolling odometer shows the estimated cost of each session and each prompt, with a breakdown of where the tokens went. Rings show your Pro or Max plan's 5-hour and weekly limits, with reset times, and how full the context window is.
- **Chats.** Every conversation Claude Code has saved on your computer, including ones from before Orbit was installed. They're grouped by project, can be searched (inside the text too), and show as clean chat: your messages, Claude's formatted replies, and each run of tool steps folded into one expandable row. From any conversation you can carry it on in the workspace, show it on the map, replay it, or export it.
- **Workspace.** Start Claude sessions from the browser. The real, unmodified Claude Code runs in a built-in terminal, signed in with your own account, beside the live map. Run several in tabs.
- **"Claude needs you" alerts.** When Claude asks permission or waits for an answer, the workspace glows amber, shows exactly what it wants, and can send a desktop alert.
- **Shareable reports.** One click gives a self-contained HTML page or Markdown file with what you asked, the files changed with diffs, commands with output, failures and cost.

## Install

### As a Claude Code plugin (recommended)

Inside Claude Code:

```
/plugin marketplace add JPvanderMerwe/orbit
/plugin install orbit@orbit
```

Restart Claude Code, then type `/orbit`.

### As a standalone command

```
pipx install git+https://github.com/JPvanderMerwe/orbit
orbit install-hooks
orbit
```

`orbit install-hooks` connects Orbit to Claude Code by adding hooks to `~/.claude/settings.json`. It backs up that file first and keeps any hooks you already have. `orbit uninstall-hooks` removes only Orbit's hooks. Use either the plugin or `install-hooks`, not both, or every action is recorded twice.

## Commands

| In Claude Code | In a terminal | What it does |
|---|---|---|
| `/orbit` | `orbit session <id>` | Open Orbit on the session you are in |
| `/orbit all` | `orbit` | Open the start screen with every session |
| `/orbit chats` | `orbit chats` | Open every saved conversation |
| `/orbit new [folder]` | `orbit new [folder]` | Start a new Claude session in the workspace |
| `/orbit statusline` | `orbit install-statusline` | Show your Pro or Max plan limits in Orbit |
| `/orbit statusline off` | `orbit uninstall-statusline` | Put your previous status line back |
| `/orbit status` | `orbit status` | Say whether Orbit is running |
| `/orbit stop` | `orbit stop` | Stop Orbit (also closes workspace sessions) |
| | `orbit install-hooks` | Connect Orbit to Claude Code without the plugin |
| | `orbit url` | Print the private link instead of opening a browser |

Keyboard: `/` search · `E` expand the selected step · `←` `→` previous and next step in the full-screen view · `Space` play or pause a replay · `Esc` close or go back.

## Cost and plan usage: how the numbers work

- **Estimated cost** comes from the token counts Claude Code writes to its own session files, priced at Anthropic's API list prices (October 2026). It shows what the same work would cost on the API. If you're on a Pro or Max plan you aren't billed per token, so use the plan rings for your real limits.
- **Plan limits** (5-hour and weekly) are only shared by Claude Code through its status line. `/orbit statusline` sets that up. If you already have a status line, Orbit keeps it: it passes the same data to yours and shows your line's output. These limits only exist on Pro and Max plans.
- **Prices change.** To correct or add a model, create `~/.orbit/prices.json` like this (US$ per million tokens: input, output, 5-minute cache write, 1-hour cache write, cache read):

  ```json
  { "claude-opus-5-5": [4, 20, 5, 8, 0.2] }
  ```

- Claude Code's session file format is internal and can change between versions. If a future version changes it, cost and chat history may stop updating until Orbit is updated. The live map does not depend on it.

## Requirements

- Claude Code
- Python 3.9 or newer, available as `python3` (standard library only; nothing else to install)
- macOS or Linux. Watching sessions may work on Windows; the built-in workspace terminals do not.

## Privacy and security

- Everything stays on your computer. Orbit listens on `127.0.0.1` only and sends nothing anywhere.
- A workspace terminal can run commands on your computer, so Orbit is locked with a private access key stored in `~/.orbit/token`, readable only by you. `/orbit` opens it with that key. Any other request is refused, including from other user accounts on the same computer and from other websites open in your browser.
- Claude Code runs unmodified and signs in through its own login. Orbit never sees or handles your Claude credentials.
- Activity is saved to `~/.orbit/events.jsonl`, including file contents Claude read and command output, each value cut at 20,000 characters. The file is trimmed automatically at 50 MB. Press **Clear** in Orbit, or delete the folder, to wipe it.
- Chats reads Claude Code's own saved conversations in `~/.claude/projects` and never changes them.
- Reports include your prompts, commands and code changes. Check one before you share it.
- The hook adds about 50 milliseconds per action and can never block or break Claude.

## Settings

| Environment variable | Default | Purpose |
|---|---|---|
| `ORBIT_PORT` | `7777` | Port for Orbit |
| `ORBIT_DATA` | `~/.orbit` | Where activity, the access key and settings are stored |

## Upgrading from Activity Viewer

Orbit is the new name for Activity Viewer. Uninstall the old plugin first (`/plugin uninstall activity-viewer@activity-viewer`), then install Orbit. Your recorded activity and access key move from `~/.claude-activity-viewer` to `~/.orbit` automatically the first time Orbit runs.

## Claude Desktop (optional, limited)

Claude Desktop has no hooks, so its actions can't be captured directly. `extras/claude-desktop/mcp_server.py` gives Claude a `show_step` tool to report what it is doing, and those steps appear in Orbit. It is a narration chosen by Claude, not a guaranteed record. Add it to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "orbit": {
      "command": "python3",
      "args": ["/full/path/to/extras/claude-desktop/mcp_server.py"]
    }
  }
}
```

Orbit must be running on the same computer (type `/orbit` in Claude Code once, or run `orbit`).

## What's in this repository

| Path | What it is |
|---|---|
| `.claude-plugin/marketplace.json` | Claude Code plugin marketplace catalogue |
| `plugins/orbit/` | The plugin: hooks, local server, dashboard, `/orbit` command |
| `pyproject.toml` | Standalone `orbit` command (pip or pipx) |
| `extras/claude-desktop/` | Optional MCP server for Claude Desktop |

## Credits

The workspace terminal uses [xterm.js](https://xtermjs.org/) (MIT licence), bundled in `plugins/orbit/scripts/vendor/` with its licence file.

## Licence

MIT. See [LICENSE](LICENSE).

## Not affiliated

Orbit is an independent community project. It is not made or endorsed by Anthropic. Claude and Claude Code are trademarks of Anthropic.
