# agent-sync

agent-sync keeps your agents in step on macOS and Linux. It supports **Claude Code**, **Codex**, **Cursor**,
**Gemini CLI**, and **Antigravity**. Choose which agents to keep in step and which one provides the settings they
share.

- **Skill packages** such as [pstack](https://github.com/backnotprop/pstack) are installed and updated for the
  enabled agents using the [`skills` CLI](https://www.npmjs.com/package/skills). When two packages ship a skill
  with the same name, the first package in `skill_packages` keeps it. Upkeep removes skills a package drops.
- **Package subagents** (`agents/*.md`) are written in each enabled agent's format. Cursor gets a local
  subagent plugin. You can set models for Claude Code, Codex, Cursor, and Gemini CLI and add notes to the
  subagent prompts. Antigravity chooses its own model.
- **Your own skills** (the ones you made in the source tool, not from a package) are copied to the other tools.
  A copy is marked as agent-sync's, so it follows the source and goes away with it; a folder you made by hand in
  the other tool is never touched. With Gemini CLI as the source, skills only in `~/.gemini/skills` reach
  Antigravity but not Codex or Cursor, which share Gemini CLI's other folder (`~/.agents/skills`): keep a skill
  there to share it with them.
- **Loaded once**: a shared copy is left out only when every tool that reads its folder loads an identical skill elsewhere. Every upkeep
  checks each tool for a skill name it loads from two places, a plugin installed twice from two catalogs, and an
  MCP server it loads twice (from its settings and from a plugin), and names both places. Codex online-catalog
  plugins, Antigravity plugins and Gemini CLI extensions count. Codex skills you switched off and plugins you
  disabled don't. Cursor reads Claude Code's skills and plugins too unless you turned off its third-party setting.
  Skills you keep twice on purpose go in `allow_duplicate_skills`.
- **Each tool's own version**: Codex uses a plugin made for Codex in place of a Claude Code-format copy when the
  Codex one is not older and keeps every skill, MCP server, command, agent and hook invocation. Otherwise both stay and the
  run says why.
- **pstack model roles** (`feature, refactoring: <model>` and the rest) are copied from the source tool's
  `pstack-models` file to the others, and each tool keeps its own notes on how to run each model.
- **MCP servers** at user level are added to the tools that don't have them yet. A server a tool's plugin already
  brings is not added again. An exact settings copy of a plugin's server can be removed after you approve it.
  Environment values that look like secrets are not copied: the run tells you which variable to set. Every copy
  follows the newest version of a server an app rewrites on its updates (Codex's `node_repl`). A pinned package
  (`npx -y name@1.2.3`) moves to the newest release in the same major version once it is at least 2 days old.
  A plugin suppresses a pin update only when its server settings match apart from the pin version.
- **Global instructions** (optional, `mirror_instructions`) are shared by every enabled agent. A line added in any
  agent's file reaches the others. A deletion in the source file pauses mirroring until you choose in
  `agent-sync pending`. If another tool drops or rewords a shared line, the other copies stay until you choose in
  `agent-sync review`. Gemini CLI and Antigravity share `~/.gemini/GEMINI.md`. Cursor receives an always-applied rule.
- **Memory**: you choose whether to share Claude Code's memory notes with the other agents. Notes about
  credentials, keys or personal details stay in Claude Code by default. See [Memory and privacy](#memory-and-privacy).
- **Plugins and extensions** are updated for enabled agents: Claude Code's plugins and marketplaces, Git plugin
  marketplaces in Codex, user marketplaces in Cursor, Antigravity plugins (from its catalog), and Gemini CLI
  extensions.
- **Skills** from every source are updated (`skills update`), not only the packages in `skill_packages`.
- **MCP server programs** are upgraded the way they were installed (Homebrew, npm, pipx or uv). A program with its
  own updater runs it when you list it in `program_updates`.
- **Upgrades stay in the installed major version.** MCP programs, global tools, git, Node and npm move to the
  newest release in the major version you have, once that release is at least 2 days old. A newer major version
  can include breaking changes, so upkeep asks you first (see [Approve removals](#approve-removals));
  `global_major_updates` installs it without asking, still after 2 days. Homebrew has no 2-day wait, because
  Homebrew reviews a release before publishing it. A pipx tool installed from a URL or a folder is left alone.
- **CLIs** are updated through Homebrew when it installed them or through their own update commands. Gemini CLI
  uses npm outside Homebrew. Antigravity uses `agy update`. Upkeep also upgrades git and Node when Homebrew
  installed them, and npm when it is installed globally. `update_global_tools` (off by default) also updates your
  other global npm, pipx and uv tools, and Homebrew packages when you list `"homebrew"`.
- **agent-sync itself** is updated the way you installed it: `brew upgrade` for the Homebrew formula,
  `pipx upgrade`, or the newest release from GitHub for an install from the repository.
- **A double-click launcher** (macOS, optional): "Agent Upkeep" on the Desktop runs the upkeep with every step on
  screen, then lets you press `r` to review differences or `s` to change the settings. Every run puts it back if
  it is missing and rewrites it when a new version of agent-sync changes it.
- **A status line** in Claude Code, Cursor CLI and Antigravity, and Codex's built-in status items. See
  [Status line](#status-line).
- **Backups and approvals**: upkeep saves your files before changing them. You approve removals of installed items.
  See [Approve removals](#approve-removals).
- **A small log** with a timestamp on every line keeps the last 7 days, and warns when a model you listed in
  `warn_models` (one you have no subscription for, say) shows up in any tool's settings.

It runs hourly and at login but only does the work once every 20 hours, so a day the Mac was off is caught up
soon after it starts.

> Status: 0.9.1. macOS (launchd) and Linux (systemd user timer). See the [roadmap](docs/ROADMAP.md).

## Install

Requires Python 3.11+, git, and Node (for `npx`). Without Homebrew or
[pipx](https://pipx.pypa.io), give it a virtual environment of its own:
`python3 -m venv ~/.local/share/agent-sync/venv && ~/.local/share/agent-sync/venv/bin/pip install
git+https://github.com/Kasakir-AI/agent-sync.git`, then link `~/.local/share/agent-sync/venv/bin/agent-sync` into a
folder on your PATH. 
```sh
brew install kasakir-ai/tap/agent-sync      # or: pipx install git+https://github.com/Kasakir-AI/agent-sync.git
agent-sync setup                  # discover agents and install missing prerequisites and enabled CLIs
agent-sync config                 # change the settings by answering questions (optional)
agent-sync review                 # compare settings across the tools and choose which versions to keep
agent-sync run --dry-run          # see what it would change first
agent-sync run --force            # first run, with every step on screen
agent-sync install --launcher     # schedule it (hourly + at login, works once a day); --launcher adds
                                  # a double-click "Agent Upkeep" to the Desktop (macOS)
agent-sync launcher               # just the Desktop launcher, if you want it without reinstalling
agent-sync inventory              # what each agent loads, and any duplicates or outdated pins
agent-sync pending                # removals and memory sharing waiting for a choice, plus warnings
```

In a terminal, `agent-sync setup` first offers to add installed agents you haven't enabled. It then offers to
install missing prerequisites and CLIs for all enabled agents. With `--dry-run` it lists the installation plan.
With `--yes` it installs from the current list without prompting.

On macOS, installing Homebrew requires an administrator. Once Homebrew is available, setup uses its
`claude-code`, `codex`, and `cursor-cli` casks. Gemini CLI uses the `gemini-cli` formula. Setup uses each CLI's
official installer when Homebrew is unavailable to your account. Antigravity always uses its official installer.

On Linux, `agent-sync install` sets up a systemd user timer. Run `loginctl enable-linger` if it should also run
while you are logged out.

`agent-sync run --dry-run` previews file changes, commands, cleanup, and questions.
`agent-sync status` shows the last run, log location, backup totals and limits, and enabled CLI versions.
`agent-sync uninstall` removes the schedule and the launcher and turns off the launcher setting.

## Check what each agent loads

`agent-sync inventory` lists, for each enabled agent, the plugins it loads (with the version and the tool each was
made for), its MCP servers (from its settings or from a plugin, with pinned package versions and newer ones), and
the number of skills it loads. It reports duplicates, outdated pins, and settings files it could not read. A
duplicate skill from two plugins awaiting a replacement approval appears as an informational message.

## Approve removals

Before a run changes a file in your setup, it saves the original in
`~/.local/state/agent-sync/backups/run-<date-time>/`. Backups use paths relative to your resolved home folder.
Files outside your home go under `_outside/`. On macOS, upkeep uses APFS clones for folders when it can.
Global skill updates back up the skills with reported updates. When the Skills CLI updates only in bulk or
upkeep cannot read the update list, upkeep backs up every locked skill before updating.
A standalone update from `program_updates` backs up the resolved executable and its dedicated install folder under `~/.local` or `/usr/local/opt`, if present.
Upkeep prunes unchanged copies at the end of the run. A run with no changed originals leaves no backup folder.

Every `agent-sync run` checks retention, even when upkeep is skipped, offline, or fails. Backups expire after
30 days (`backup_days`). If the backup folder exceeds 500 MiB (`backup_max_mb`), upkeep deletes the oldest
agent-sync backup folders until it reaches the limit or exhausts eligible backups. The current run and any
folders you add yourself can keep the total above the limit. Upkeep skips symlinked backup folders and entries
and asks you to acknowledge the warning. Run `agent-sync status` to see backup size, folder count, and both limits.

Cleanup also drops cached checkouts for packages you remove from `skill_packages`. It checks the checkout name
and Git origin before deleting a folder. Warning acknowledgements and Inbox notification records expire after
30 days. Log lines expire after `log_days`. A dry run reports the cleanup it would do.

A run never removes something you or another program installed until you approve it. This covers an MCP settings
entry that a plugin already brings, a Codex plugin replaced by Codex's own version, a skill a package dropped, an
edited skill or memory copy, a deletion from your source instructions, and replacing your own status line. A run
also never installs a new major version of a program it upgrades (`upgrade:<manager>:<package>@<major>`) until you
approve it. Keeping that key stays on your major version until an even newer one comes out; keeping
`upgrade:<manager>:<package>` stays on it for good.

- **Scheduled run (macOS):** a window lists what is waiting. Select what may go ahead and click **Approve selected**, or
  click **Keep all for now**. Anything you don't select is asked about again at the next run.
- **Terminal and the Desktop launcher:** the run asks yes or no for each item. The default is no.
- **Plannotator Inbox:** when Plannotator is installed, each run with something new posts to the Inbox thread
  "agent-sync upkeep" with a button to approve and a button to keep each item.
- **Any time:** `agent-sync pending` lists what is waiting. `agent-sync pending --approve <key>` lets the next run
  remove it. `agent-sync pending --keep <key>` adds it to `keep_installed`, which keeps it for good.

Approvals expire when upkeep confirms the item is gone. For MCP duplicates, upkeep checks each settings file
independently. A skipped step or an unreadable file preserves the approvals it could not check.

Each batch of deletions from the source instructions gets one `instructions:<digest>` key. Removing one of two
identical lines counts as one deletion. All instruction edits wait for that batch to be decided. Approve to apply
the deletion and share the other edits on the next run. Keep to restore the shared lines and resume sharing. Add
`"instructions"` to `keep_installed` to keep the lines in every deletion batch. Moving a shared line into a local,
private, or memory section that did not already contain it leaves the other tools' copies in place.

New warnings appear in a macOS alert that stays until you click **OK** (in a terminal, until you press Enter), and in
the Inbox. A warning you acknowledge stays quiet for 30 days unless its text changes. `agent-sync pending --ack`
acknowledges the recent warnings. `notify = false` turns off desktop windows and Inbox messages.

## Memory and privacy

Claude Code keeps long-term memory as notes in `~/.claude/projects/<your home folder>/memory/`. When it has notes
and another agent is enabled, upkeep asks if you want to share them. You can answer in the terminal or a desktop
window. If the choice is still open, the Plannotator Inbox offers **Share** and **Don't share**.
You can also run `agent-sync pending --approve memory-sharing` or `agent-sync pending --keep memory-sharing`.

Upkeep saves your choice as `mirror_memory = true` or `mirror_memory = false`. You can set either value yourself
to skip the prompt. While you have a choice waiting, upkeep keeps the notes in Claude Code and asks again on the
next upkeep. A dry run shows the question without asking you to answer.

If you share, upkeep copies eligible notes to `~/.agents/memory/` and adds the index to the other agents'
instructions. Upkeep manages only the copies it recorded in its manifest. It skips symlinked notes and asks before
overwriting an edited copy. If Claude Code's memory folder is removed, upkeep backs up the instruction files and
clears the index it added.

The other agents send their instructions to their own companies, so some notes stay in Claude Code:

- A note that contains a secret value, such as a GitHub or API token, an AWS key or a private key, never leaves
  Claude Code. The same goes for an instruction line with a secret value: it stays in its file.
- A note that mentions credentials, tokens, keys, a legal name, an address, a phone number or bank details stays in
  Claude Code by default. Each run lists these notes and why. Add a note's name to `share_memory` to share it.
- A note with `private: true` in its front matter, or listed in `private_memory`, stays in Claude Code.
- Lines between `<!-- agent-sync: private -->` and `<!-- agent-sync: end private -->` in an instruction file stay in
  that file. Lines between `<!-- agent-sync: local -->` and `<!-- agent-sync: end local -->` do too.

## Status line

`agent-sync-statusline <tool>` draws one status line from the data each CLI passes it: the model and mode, the
5-hour and weekly limits used with their reset times, the context left, the cost, and running agents and tasks. On a
wide terminal it uses one line. On narrower ones it shortens labels, then wraps between parts, so the whole line
stays visible.

With `status_line` (on by default), upkeep sets it up in Claude Code (`~/.claude/settings.json`), Cursor CLI
(`~/.cursor/cli-config.json`) and Antigravity (`~/.gemini/antigravity-cli/settings.json`). Cursor does not pass plan
limits or cost, so its line shows the model and the context. Codex has no custom status line command, so upkeep sets
its built-in items in `~/.codex/config.toml`: model, 5-hour limit, weekly limit, context, tokens and cost. Gemini CLI
has no custom status line. If a tool already has a status line of yours, upkeep asks before replacing it.

## Choose your agents

`agent-sync agents` shows which agents are installed, which are in step, and which is the source. In a terminal,
it offers to add installed agents that are outside sync. The **Agents** section in `agent-sync config` also lets
you change the list.

```sh
agent-sync agents add gemini antigravity
agent-sync agents remove cursor
```

Claude Code, Codex, and Cursor are enabled by default. You can add an agent before installing it and then run
`agent-sync setup`. Removing an agent stops upkeep of its own files and leaves them in place. A shared file
continues to receive updates while an enabled agent uses it.

Choose a new source under **Source tool** in `agent-sync config` before removing the current source. Any
enabled agent can be the source, and Cursor skips the optional global instructions mirror when it is the source.

## Review differences

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

## Configure

The easy way: run `agent-sync config` (or `agent-sync settings`). It shows every setting with its current value,
explains each one with examples, and asks what you want to change: the enabled agents, the source tool, skill packages, which parts to keep in step, agent models and notes, model warnings,
update and install commands, extra steps, and the pstack model roles. It also opens your global instructions in
your editor. Nothing is written until you save; saving keeps the comments in your file, leaves the old one next to
it as `config.toml.bak`, and then offers a dry run so you can see what the next upkeep would do.

Or edit the file yourself: copy [`config.example.toml`](config.example.toml) to `~/.config/agent-sync/config.toml`
and change what you need. Every key is optional:

| key | default | what it does |
| --- | --- | --- |
| `source` | `"claude"` | the enabled agent the others mirror |
| `agents` | `["claude", "codex", "cursor"]` | the agents to keep in step |
| `skill_packages` | `["backnotprop/pstack"]` | GitHub skill packages to install for the enabled agents |
| `skills_cli` | `"skills@latest"` | the `skills` CLI version (pin one to hold it) |
| `remove_dropped_skills` | `true` | uninstall skills a package no longer ships |
| `mirror_roles` / `mirror_mcp` / `mirror_skills` / `update_plugins` | `true` | switch parts off |
| `skip_skills` | `[]` | your own skills that stay in the source tool only |
| `allow_duplicate_skills` | `[]` | skill names a tool may load from two places without a warning, e.g. `["pdf"]` |
| `mirror_instructions` | `false` | share global instructions across the enabled agents |
| `mirror_memory` | unset | ask once before sharing Claude Code's memory notes |
| `private_memory` / `share_memory` | `[]` | memory notes to keep in Claude Code, or flagged notes to share anyway |
| `status_line` | `true` | set up one status line in every CLI that supports it |
| `keep_installed` | `[]` | items upkeep never removes or replaces, by full key, final key segment, or colon-delimited prefix, e.g. `["cloudflare", "instructions"]` |
| `keep_mcp_versions` | `[]` | MCP servers whose pinned package version stays as it is |
| `mcp_major_updates` | `false` | let a pinned MCP package move to a new major version |
| `update_mcp_programs` | `true` | upgrade the programs MCP servers run (Homebrew, npm, pipx, uv) |
| `[program_updates]` | none | a program's own update command, e.g. `plannotator = ["plannotator", "update"]` |
| `update_global_tools` | `false` | also update global npm, pipx and uv tools (`true`), or a list that may include `"homebrew"` |
| `global_major_updates` | `false` | install a new major version of an MCP program, a global tool, git, Node or npm without asking (still after 2 days) |
| `notify` | `true` | ask for approvals and show warnings in a macOS window and the Plannotator Inbox |
| `backup_days` | `30` | how long you keep backup folders |
| `backup_max_mb` | `500` | total backup size limit in MiB |
| `warn_models` | `[]` | warn when a model name containing one of these appears, e.g. `["preview-"]` |
| `update_clis` | `true` | update all enabled CLIs, disable updates with `false`, or choose a subset |
| `[cli_commands]` | none | your own update command for a CLI, e.g. `codex = ["npm", "install", "-g", "@openai/codex@latest"]` |
| `[install_commands]` | none | your own install command for `agent-sync setup`, by agent ID or for `homebrew`, `git`, and `node` |
| `[[extra_steps]]` | none | your own commands, run at the end of every upkeep |
| `update_self` | `true` | update agent-sync itself (Homebrew, pipx, or a pip install from GitHub, which updates to the newest release) |
| `launcher` | `false` | keep the double-click "Agent Upkeep" on the Desktop and up to date (macOS); `install --launcher` and `agent-sync launcher` switch it on |
| `log_days` | `7` | how long log lines are kept |
| `[agent_models]` | `opus` / `claude-opus-5-5-high-fast` / `gpt-6.2-sol` | models for package subagents. Gemini CLI inherits its model by default |
| `[agent_notes]` | none | text added to the end of an agent's prompt in every tool, e.g. `poteto-agent = "..."` |

Unknown settings are errors, including unknown keys in a table with fixed keys. Each `[[extra_steps]]` entry
accepts `name`, `command`, and `timeout`. Put top-level settings above the first table. On Linux, the config and
state follow `XDG_CONFIG_HOME` and `XDG_STATE_HOME` when set.

## Make it yours

- **Settings first.** Most changes need only the config file, and `agent-sync config` walks you through them:
  other skill packages (`skill_packages = ["you/your-skills", "backnotprop/pstack"]`), another source tool, your
  own models for each tool's agents, parts switched off, your own update
  commands, notes on agents, or extra steps at the end.
- **Try changes safely.** `agent-sync run --dry-run` after every config change shows exactly what would happen
  (`agent-sync config` offers it after you save).
- **Change the code.** The supported agents and their settings paths are in `agent_sync/registry.py`. See `agents.py` (agent
  formats per tool), `skills.py` (your own skills, package skills), `inventory.py` (the plugins each tool loads),
  `duplicates.py` (skills a tool loads twice), `plugins.py` (plugins loaded twice, each tool's own version),
  `roles.py` (pstack model roles), `mcp.py` (MCP servers, duplicates and pinned versions), `instructions.py`
  (shared instructions), `memory.py` and `privacy.py` (shared memory, private notes, secret values), `tools.py`
  (skill packages, plugins, CLI, program and global tool updates, extra steps), `backups.py` (the copy of every
  changed file), `pending.py` and `notify.py` (approvals and warnings), `statusline.py` and `statuslines.py`
  (the status line), `checks.py` (warnings), `schedule.py` (launchd, systemd, launcher), `setup.py` (installing
  what is missing), `configure.py` and `tomledit.py` (the settings editor), `review.py` (comparing settings and
  recommending one), `textmerge.py` (merging lines), `writes.py` (the checked writer), `reviewpage.py` and
  `review.html` (the review page), `cli.py` (the order of the steps). To add a step, write a function that takes
  `log` and `dry`, never writes when `dry` is true, writes through `writes.update`, and call it from `run()` in
  `cli.py`. `tests/` shows how to test a part against a throwaway home folder (`python -m pytest`).

## Moving from your own upkeep scripts

If you already keep the tools in step with scripts of your own, move what is personal to your machine into the
config file, which stays on your machine: notes you add to agents go in `[agent_notes]`, commands only you need
go in `[[extra_steps]]`, models you never want in `warn_models`. Then:

1. `agent-sync run --dry-run` until it lists only changes you expect (ideally none).
2. Stop and remove your old scheduled job, so two jobs never write the same files.
3. `agent-sync run --force`, then `agent-sync install --launcher`.

## What it changes

The review writes only the settings you pick and apply, and in those files only the lines that differ.

| tool | files |
| --- | --- |
| Claude Code | `~/.claude/skills/*` (links and copies), `~/.claude/agents/*.md`, `~/.claude/rules/pstack-models.md`, `~/.claude.json` (`mcpServers`), `~/.claude/settings.json` (`statusLine`), `~/.claude/CLAUDE.md` (opt-in) |
| Codex | `~/.agents/skills/*`, `~/.codex/agents/*.toml`, `~/.agents/pstack-models.md`, `~/.codex/config.toml` (`[mcp_servers.*]`, `[tui] status_line`), Codex plugins (`codex plugin add` and `remove`), `~/.codex/AGENTS.md` (opt-in, memory index) |
| Cursor | `~/.agents/skills/*`, `~/.cursor/plugins/local/<package>-agents/`, `~/.cursor/rules/pstack-models.mdc`, `~/.cursor/mcp.json`, `~/.cursor/cli-config.json` (`statusLine`), `~/.cursor/rules/agent-sync-instructions.mdc` (opt-in, memory index) |
| Gemini CLI | `~/.agents/skills/*`, `~/.gemini/agents/*.md`, `~/.agents/pstack-models.md`, `~/.gemini/settings.json` (`mcpServers`), `~/.gemini/GEMINI.md` (opt-in, memory index) |
| Antigravity | `~/.gemini/config/skills/*`, `~/.gemini/config/agents/*.md`, `~/.agents/pstack-models.md`, `~/.gemini/config/mcp_config.json`, `~/.gemini/antigravity-cli/settings.json` (`statusLine`), Antigravity plugins (`agy plugin install`), `~/.gemini/GEMINI.md` (opt-in, memory index) |
| All | `~/.agents/memory/*` (memory copies), MCP server programs and global tools through their package managers |

State, backups (`backups/`), pending removals and the package checkouts live in `~/.local/state/agent-sync/`. The log is `~/Library/Logs/agent-sync.log`
on macOS and `~/.local/state/agent-sync/agent-sync.log` on Linux. The schedule is
`~/Library/LaunchAgents/io.github.agent-sync.plist` or `~/.config/systemd/user/agent-sync.{service,timer}`.

## Safety

Read [SECURITY.md](SECURITY.md). Skill packages and the `skills` CLI run with your agents' full permissions.
Only add packages you trust. You can pin `skills_cli` to choose when it updates. Upkeep backs up your files
before it changes them, asks you before removing installed items, and keeps detected secrets in their original
tool.

## Support

agent-sync is free. If it helps you, you can [star the repo](https://github.com/Kasakir-AI/agent-sync), [buy me a coffee on Ko-fi](https://ko-fi.com/kasakir), or [sponsor on GitHub](https://github.com/sponsors/Kasakir-AI), once or monthly.

### Sponsors

<!-- sponsors:start -->
<!-- This section is updated automatically. Edits here are overwritten. -->

No sponsors featured here yet. [Become the first](https://github.com/sponsors/Kasakir-AI)!
All sponsors: [SPONSORS.md](https://github.com/Kasakir-AI/.github/blob/main/SPONSORS.md).
<!-- sponsors:end -->

## License

MIT, see [LICENSE](LICENSE). pstack and the `skills` CLI are separate projects with their own licenses.
