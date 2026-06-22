---
description: Set up (or adjust) the autonomous cadence — scheduled watch turns, daily brief, weekly retro
argument-hint: "[daily|market-hours|off]"
allowed-tools: ["Read", "Bash", "AskUserQuestion", "ToolSearch"]
---

Configure the scheduled-task cadence (hermes-quant playbook-cadence ADR-0035
port). Scheduled tasks are created via the Cowork scheduled-tasks tools
(load via ToolSearch: `mcp__scheduled-tasks__*`) — each creation is shown to
the user for approval; never create one silently.

1. Show current state: `mcp__scheduled-tasks__list_scheduled_tasks` filtered
   to prompts mentioning cowork-quant; plus the watchlist from config.json
   (empty watchlist -> tell the user to set one first; a schedule with
   nothing to watch is noise).
2. Recommend the cadence based on `$ARGUMENTS` (default "daily"):
   - **daily**: one /watch turn at 14:30 UTC (pre-US-open prep on closed
     daily bars) + /brief at 13:00 UTC + /retro Sundays 18:00 UTC.
   - **market-hours**: /watch at 14:30 and 19:30 UTC weekdays (open prep +
     midday check) + /brief 13:00 UTC + /retro Sundays. Warn: still
     interday — more turns is more noise, not more edge (ADR-0083).
   - **off**: cancel/disable the cowork-quant scheduled tasks.
3. Confirm with AskUserQuestion before creating/updating each task. Task
   prompts must be self-contained, e.g.:
   "Run the cowork-quant /watch command on the workspace at E:\\...\\hermes-quant
   (state dir quant-state/). Unattended mode: queue proposals, never approve
   or fill. End with the 5-line digest."
4. Remind the user of the contract: scheduled turns QUEUE proposals;
   nothing enters the book until they approve interactively (/status shows
   the queue; pending proposals expire after 24h).

Rails: never schedule anything that approves, fills, resumes halts, or edits
config. The only self-acting writes a scheduled turn may do are marks,
settles, expiries, and gate-queued proposals.
