# Changelog

## 3.2.0 — 2026-10-25

### New
- **Background commands live.** A command Claude sends to the background shows its output as it prints, with "running in background" and a clock, even after Claude's turn ends. When it finishes, the timeline says so, with Claude Code's summary and the final output.

### Fixed
- After an update, a Claude session that was already open could start the old Orbit again. Orbit now always starts the newest installed version, and a new Claude session (or `/orbit`) replaces an older Orbit that is still running, unless workspace terminals are open in it.
- "1 actions" and "1 files changed" now read "1 action" and "1 file changed".
- The "CLAUDE · WORKING" label no longer sits on top of the Terminal node on a short map.

## 3.1.0 — 2026-10-25

Orbit now keeps up with everything Claude does, not only its tool steps.

### New
- **What Claude says.** The text Claude writes between steps appears in the timeline and banner as it is written, in the right order. Sub-agents' text shows too, labelled with the sub-agent's type.
- **Live command output.** A long command shows its output as it prints, in the details panel and the full-screen view, with the latest line in the banner.
- **Never looks frozen.** The banner shows a running clock while a command runs ("Running for 1:23") or while Claude thinks ("Thinking for 0:45").
- **Running now.** The start screen lists every Claude session open on the computer, with Move into Orbit (continue it in Orbit's workspace) and Stop. Sessions started before Orbit was on are followed from their saved conversation.

### Fixed
- The live connection notices when it has silently died (after sleep or a network drop), reconnects by itself and fetches anything it missed, without reloading the page.
- One bad event can no longer stop the live feed.
- Events are no longer lost when the Orbit server is briefly busy (the hook now waits up to 3 seconds instead of 1).
- A session counts as working whenever Claude Code reports it busy, even during a long think.

## 3.0.0 — 2026-10-24

Activity Viewer is now **Orbit**.

### New
- **Full-screen step view.** Double-click any step, or press Expand (`E`). Code changes type themselves in as a numbered before-and-after diff. Commands play out in a terminal window, the command first and then its output. A side list shows every change to the same file, every command, or every prompt. Follow live opens each new step as Claude works.
- **Streaming details.** The details panel animates the same way while you watch live.
- **Files tab.** Every file read or changed, with lines added and removed, and its full change history.
- **Search and filters** for the timeline (`/` to search).
- **Replay** any session on the map at 1× to 30×, with play, pause and scrub.
- **Cost tracking.** Estimated cost for each session and each prompt, shown on a rolling odometer with a breakdown of where the tokens went. Read from Claude Code's own session files and priced at Anthropic API list prices. Override prices in `~/.orbit/prices.json`.
- **Plan usage rings.** Pro and Max 5-hour and weekly limits with reset times, plus context-window fill. Enabled with `/orbit statusline` or `orbit install-statusline`, which keeps any status line you already have.
- **Chats.** Every Claude Code conversation saved on the computer, including ones from before Orbit was installed, grouped by project and searchable (inside the text too). Each opens as a formatted chat with the cost of each prompt, and can be carried on in the workspace, shown or replayed on the map, or exported as Markdown.
- **Shareable reports** as self-contained HTML or Markdown.
- **Standalone `orbit` command** (`pipx install git+https://github.com/JPvanderMerwe/orbit`) with `orbit install-hooks` to connect it to Claude Code without the plugin.

### Changed
- Command `/viz` is now `/orbit`. Plugin `activity-viewer` is now `orbit`.
- Data folder `~/.claude-activity-viewer` is now `~/.orbit` and moves across automatically.
- Settings `CLAUDE_VIZ_PORT` and `CLAUDE_VIZ_DATA` are now `ORBIT_PORT` and `ORBIT_DATA`. The old names still work.
- Diffs now handle large rewrites line by line, and long unchanged stretches fold away.

## 2.0.0

Workspace (run Claude in the browser), "Claude needs you" alerts, session picker, private access key.
