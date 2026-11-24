# agent-sync

Keep **Claude Code**, **Codex** and **Cursor** in step on one Mac (or Linux machine). Pick one of them as the source of truth, and
agent-sync makes the other two match it, once a day:

- **Skill packages** (for example [pstack](https://github.com/backnotprop/pstack)) are installed and updated for
  all three tools from one shared copy, with the [`skills`](https://www.npmjs.com/package/skills) CLI. When two
  packages ship a skill with the same name, the package listed first in `skill_packages` keeps it.
- **Agents** that ship with a package (`agents/*.md`) are mirrored into each tool's own format: Claude Code
  `~/.claude/agents`, Codex `~/.codex/agents/*.toml`, and a small local Cursor plugin, because Cursor only reads
  custom agents from projects and plugins. Each tool's copy runs on a model you choose for that tool, and you can
  add your own notes to an agent's prompt. Skills a package stops shipping are uninstalled.
- **Your own skills** (the ones you made in the source tool, not from a package) are copied to the other tools.
  A copy is marked as agent-sync's, so it follows the source and goes away with it; a folder you made by hand in
  the other tool is never touched.
- **No skill twice**: a copy is left out when a tool that would read it already loads a skill of that name, and
  the log says which. Skills inside Claude Code plugins are never copied: Cursor loads them from Claude Code
  already. To give Codex a plugin's skills, install the plugin in Codex too (`codex plugin list`).
- **Duplicate skills**: every upkeep checks whether a tool loads the same skill name from two places, such as a
  pack installed both as a package and as a plugin, and names the skills and both places in the log. Codex
  skills you switched off and Claude Code plugins you disabled don't count. Cursor reads Claude Code's skills and
  plugins too unless you turned off its third-party setting, so a pack installed as a Claude Code plugin and as a
  package shows up twice there. Skills you keep twice on purpose go in `allow_duplicate_skills`.
- **pstack model roles** (`feature, refactoring: <model>` and the rest) are copied from the source tool's
  `pstack-models` file to the others, and each tool keeps its own notes on how to run each model.
- **MCP servers** at user level are added to the tools that don't have them yet. Nothing is ever deleted, and
  servers that already exist are left alone, with one exception: every copy follows the newest version of a server
  an app rewrites on its updates (Codex's `node_repl`). A copy that points at files that no longer exist, or that
  differs only in older version numbers, is brought up to the newest working copy, and the original is backed up.
- **Global instructions** (optional): `CLAUDE.md` is merged into Codex's `AGENTS.md` and copied into a Cursor rule
  that always applies, or `AGENTS.md` the other way round. The merge adds new lines and replaces changed ones, and
  a line you delete from the source leaves the other file too. Lines only the other file has stay as its own notes.
  A shared line you reword in the other file (or Codex rewords when it remembers something) is kept, and the run
  warns until the source has the same wording, so make lasting changes in the source.
- **Plugins**: Claude Code's plugin marketplaces and the plugins you installed, the Git plugin marketplaces you
  added to Codex, and the Cursor plugin marketplaces you added (Cursor manages its built-in ones itself).
- **The three CLIs** are updated: Claude Code and Cursor Agent through Homebrew when installed that way,
  otherwise through their own updaters, and Codex with `codex update`. git and Node (which runs the `skills`
  tool) are upgraded too when Homebrew installed them.
- **agent-sync itself** is updated the way you installed it: `brew upgrade` for the Homebrew formula,
  `pipx upgrade`, or the newest release from GitHub for an install from the repository.
- **A double-click launcher** (macOS, optional): "Agent Upkeep" on the Desktop runs the upkeep with every step on
  screen, then lets you press `r` to review differences or `s` to change the settings. Every run puts it back if
  it is missing and rewrites it when a new version of agent-sync changes it.
- **A small log** with a timestamp on every line keeps the last 7 days, and warns when a model you listed in
  `warn_models` (one you have no subscription for, say) shows up in any tool's settings.

It runs hourly and at login but only does the work once every 20 hours, so a day the Mac was off is caught up
soon after it starts.

> Status: 0.5.1. macOS (launchd) and Linux (systemd user timer). See the [roadmap](docs/ROADMAP.md).

## Install

Requires Python 3.11+, git, and Node (for `npx`). Without Homebrew or
[pipx](https://pipx.pypa.io), give it a virtual environment of its own:
`python3 -m venv ~/.local/share/agent-sync/venv && ~/.local/share/agent-sync/venv/bin/pip install
git+https://github.com/Kasakir-AI/agent-sync.git`, then link `~/.local/share/agent-sync/venv/bin/agent-sync` into a
folder on your PATH. 
```sh
brew install kasakir-ai/tap/agent-sync      # or: pipx install git+https://github.com/Kasakir-AI/agent-sync.git
agent-sync setup                  # install whatever is missing: git, Node, Claude Code, Codex, Cursor Agent
agent-sync config                 # change the settings by answering questions (optional)
agent-sync review                 # compare settings across the tools and choose which versions to keep
agent-sync run --dry-run          # see what it would change first
agent-sync run --force            # first run, with every step on screen
agent-sync install --launcher     # schedule it (hourly + at login, works once a day); --launcher adds
                                  # a double-click "Agent Upkeep" to the Desktop (macOS)
agent-sync launcher               # just the Desktop launcher, if you want it without reinstalling
```

`agent-sync setup` installs only what is missing, and asks first (`--dry-run` lists it, `--yes` skips the
question). On a Mac without Homebrew it installs Homebrew first (that needs an administrator's password); then
git, Node and the three CLIs come from Homebrew, the CLIs as the casks `claude-code`, `codex` and `cursor-cli`.
Where Homebrew isn't available to your account, the CLIs come from their official per-user installers into
`~/.local/bin` (no administrator needed), and setup tells you how to get git or Node. The daily run never
installs anything; it notes a missing CLI instead.

On Linux, `agent-sync install` sets up a systemd user timer instead; run `loginctl enable-linger` if it should also
run while you are logged out.

`agent-sync run --dry-run` shows everything a run would change (files it would write, commands it would run)
and changes nothing: no log, no state, no package cache. `agent-sync status` shows the last run, the log
location and the installed CLI versions, and which Claude Code plugins have skills you could offer to Codex and
Cursor. `agent-sync uninstall` removes the schedule and the launcher (and switches the launcher setting off, so
the next run doesn't bring it back).

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
explains each one with examples, and asks what you want to change: the source tool, skill packages, which
plugins' skills go to Codex and Cursor, which parts to keep in step, agent models and notes, model warnings,
update and install commands, extra steps, and the pstack model roles. It also opens your global instructions in
your editor. Nothing is written until you save; saving keeps the comments in your file, leaves the old one next to
it as `config.toml.bak`, and then offers a dry run so you can see what the next upkeep would do.

Or edit the file yourself: copy [`config.example.toml`](config.example.toml) to `~/.config/agent-sync/config.toml`
and change what you need. Every key is optional:

| key | default | what it does |
| --- | --- | --- |
| `source` | `"claude"` | the tool the others mirror |
| `skill_packages` | `["backnotprop/pstack"]` | GitHub skill packages to install for all three tools |
| `skills_cli` | `"skills@latest"` | the `skills` CLI version (pin one to hold it) |
| `remove_dropped_skills` | `true` | uninstall skills a package no longer ships |
| `mirror_roles` / `mirror_mcp` / `mirror_skills` / `update_plugins` | `true` | switch parts off |
| `skip_skills` | `[]` | your own skills that stay in the source tool only |
| `allow_duplicate_skills` | `[]` | skill names a tool may load from two places without a warning, e.g. `["pdf"]` |
| `mirror_instructions` | `false` | copy the source tool's global instructions to the others |
| `warn_models` | `[]` | warn when a model name containing one of these appears, e.g. `["preview-"]` |
| `update_clis` | `true` | `false`, or a list such as `["claude", "codex"]` |
| `[cli_commands]` | none | your own update command for a CLI, e.g. `codex = ["npm", "install", "-g", "@openai/codex@latest"]` |
| `[install_commands]` | none | your own install command for `agent-sync setup`, per item: `homebrew`, `git`, `node`, `claude`, `codex`, `cursor` |
| `[[extra_steps]]` | none | your own commands, run at the end of every upkeep |
| `update_self` | `true` | update agent-sync itself (Homebrew, pipx, or a pip install from GitHub, which updates to the newest release) |
| `launcher` | `false` | keep the double-click "Agent Upkeep" on the Desktop and up to date (macOS); `install --launcher` and `agent-sync launcher` switch it on |
| `log_days` | `7` | how long log lines are kept |
| `[agent_models]` | `opus` / `claude-opus-5-5-high-fast` / `gpt-6.2-sol` | the model each tool's copy of an agent uses |
| `[agent_notes]` | none | text added to the end of an agent's prompt in every tool, e.g. `poteto-agent = "..."` |

A misspelled setting is an error, not silently ignored. On Linux the config and state follow `XDG_CONFIG_HOME`
and `XDG_STATE_HOME` when set.

## Make it yours

- **Settings first.** Most changes need only the config file, and `agent-sync config` walks you through them:
  other skill packages (`skill_packages = ["you/your-skills", "backnotprop/pstack"]`), another source tool, your
  own models for each tool's agents, parts switched off, your own update
  commands, notes on agents, or extra steps at the end.
- **Try changes safely.** `agent-sync run --dry-run` after every config change shows exactly what would happen
  (`agent-sync config` offers it after you save).
- **Change the code.** Each part is one small module in `agent_sync/` with no dependencies: `agents.py` (agent
  formats per tool), `skills.py` (your own skills, package skills), `duplicates.py` (skills a tool loads twice),
  `roles.py` (pstack model roles), `mcp.py` (MCP servers), `instructions.py` (global instructions), `tools.py`
  (skill packages, plugins, CLI updates, extra steps), `checks.py` (warnings), `schedule.py` (launchd, systemd,
  launcher), `setup.py` (installing what is missing), `configure.py` and `tomledit.py` (the settings editor),
  `review.py` (comparing settings and recommending one), `textmerge.py` (merging lines), `writes.py` (writing a
  choice), `reviewpage.py` and `review.html` (the review page), `cli.py` (the order of the steps). To add a step,
  write a function that takes `log` and `dry`, never writes when `dry` is true, and call it from `run()` in
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
| Claude Code | `~/.claude/skills/*` (links and copies), `~/.claude/agents/*.md`, `~/.claude/rules/pstack-models.md`, `~/.claude.json` (`mcpServers`), `~/.claude/CLAUDE.md` (block, opt-in) |
| Codex | `~/.agents/skills/*`, `~/.codex/agents/*.toml`, `~/.agents/pstack-models.md`, `~/.codex/config.toml` (`[mcp_servers.*]`, appended), `~/.codex/AGENTS.md` (block, opt-in) |
| Cursor | `~/.agents/skills/*`, `~/.cursor/plugins/local/<package>-agents/`, `~/.cursor/rules/pstack-models.mdc`, `~/.cursor/mcp.json`, `~/.cursor/rules/agent-sync-instructions.mdc` (opt-in) |

State and the package checkouts live in `~/.local/state/agent-sync/`. The log is `~/Library/Logs/agent-sync.log`
on macOS and `~/.local/state/agent-sync/agent-sync.log` on Linux. The schedule is
`~/Library/LaunchAgents/io.github.agent-sync.plist` or `~/.config/systemd/user/agent-sync.{service,timer}`.

## Safety

Read [SECURITY.md](SECURITY.md). In short: skill packages and the `skills` CLI run with your agents' full
permissions, so only add packages you trust, and pin `skills_cli` if you don't want new releases picked up
automatically.

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
