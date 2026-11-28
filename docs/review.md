# Review differences

Run `agent-sync review` to open a local page. Compare global instructions, rule files, pstack model roles, your
skills, and MCP servers side by side. You can also see which plugins are installed and see what packages keep up to date automatically. The upkeep log points you to the page when settings
differ or a tool is missing a copy.

Each version shows its tools, path, date, length, and origin. The origin label identifies your edits, copies
agent-sync made, copies you edited, package updates, plugin skills, and entries from a tool's settings file.
A copy can look newer because agent-sync rewrote it. Recommendations consider origin first and use the date to
compare edits.

Each card says in one sentence what differs, for example "Codex has 85 lines Claude Code doesn't; Claude Code has
1 line Codex doesn't." Choose a version to keep across the tools, or leave the setting as it is.

Keeping a version writes only the differences. A line you edited takes the chosen text, and lines the chosen
version adds are added. Lines, notes and files that only one tool has stay.

Rules that apply only in certain files or contexts stay in the tool that defines them. Review lists each
scoped rule separately under **Only in one tool**.

Lines only one tool has may be that tool's own notes, or old rules you removed elsewhere before agent-sync could
see it happen. So the first time a version only adds lines to another, the card is listed under **Check once**
("Codex has 12 lines Claude Code doesn't that you haven't checked yet") with those lines open. Switch any outdated
line to **Remove** and apply; applying with every line kept writes nothing and only records that you checked them.
From then on the setting counts as the same ("Codex keeps 12 lines of its own") until a line nobody checked shows
up in a tool.

A line you delete from the version you keep can go from the other tools too. Each time you apply a choice, the
review remembers the kept version in `~/.local/state/agent-sync/review/baselines.json`. Until it has one, it uses
agent-sync's own copy of the source tool, such as the Cursor `agent-sync-instructions.mdc`. A line that another
tool still has, that the last sync had, and that the kept version no longer has was deleted, so the card marks
the setting as differing, for example "Claude Code no longer has 1 line that Codex still has". Every other line
only one tool has is that tool's own. Under each version, **Lines only Codex has** lists those lines with a
**Keep** / **Remove** switch for each. Deleted lines start on **Remove**, and the tool's own lines start on
**Keep**. Review changes shows the lines that will be removed in red. In a skill, the rule applies to each text
file, and files only one tool has stay.
If a reviewed skill keeps local notes or files, upkeep leaves that copy in place.

Some plugins and installers write a separate version of a skill for each tool on purpose. For example, a Claude Code
copy uses `allowed-tools` while the Codex copy has `agents/openai.yaml`. The review lists these under **The same**
as "Made separately for each tool", leaves them alone by default, and does not count them as differences. MCP servers
a Claude Code plugin brings count as kept in step, not as choices. A copy of an MCP server that points at files
that no longer exist is marked stale and never recommended.

Select **Review changes** to see the exact lines each file gains or loses before you apply them. The review saves
a copy of each file it changes under `~/.local/state/agent-sync/backups/review-YYYYmmdd-HHMMSS/`. On Linux, the
backups follow `XDG_STATE_HOME` when it is set.

- `agent-sync review --list` prints the comparison and recommendations in your terminal, the settings that
  need a choice first.
- `agent-sync review --no-open` prints a local URL you can open yourself.
