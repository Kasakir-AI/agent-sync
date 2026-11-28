# Status line

`agent-sync-statusline <tool>` draws one status line from the data each CLI passes it: the model and mode, the
5-hour and weekly limits used with their reset times, the context left, the cost, and running agents and tasks. On a
wide terminal it uses one line. On narrower ones it shortens labels, then wraps between parts, so the whole line
stays visible.

With `status_line` (on by default), upkeep sets it up in Claude Code (`~/.claude/settings.json`), Cursor CLI
(`~/.cursor/cli-config.json`) and Antigravity (`~/.gemini/antigravity-cli/settings.json`). Cursor does not pass plan
limits or cost, so its line shows the model and the context. Codex has no custom status line command, so upkeep sets
its built-in items in `~/.codex/config.toml`: model, 5-hour limit, weekly limit, context, tokens and cost. Gemini CLI
has no custom status line. If a tool already has a status line of yours, upkeep asks before replacing it.
