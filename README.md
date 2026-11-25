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
  differs only in an older app version (a versioned folder or a `*VERSION` setting), is brought up to the newest
  working copy, and the original is backed up.
- **Global instructions** (optional) follow the source into the other agents' instruction files. Gemini CLI and
  Antigravity share `~/.gemini/GEMINI.md`. Cursor receives an always-applied rule. Changes and deletions in the
  source carry over, while local notes stay in each file. If you reword a shared line in another file, upkeep
  keeps your wording and reminds you to make the same change in the source.
- **Plugins and extensions** are updated for enabled agents. Upkeep refreshes Claude Code's plugins and
  marketplaces, Git plugin marketplaces in Codex, user marketplaces in Cursor, and Gemini CLI extensions.
- **CLIs** are updated through Homebrew when it installed them or through their own update commands. Gemini CLI
  uses npm outside Homebrew. Antigravity uses `agy update`. Upkeep also upgrades git and Node when Homebrew
  installed them.
- **agent-sync itself** is updated the way you installed it: `brew upgrade` for the Homebrew formula,
  `pipx upgrade`, or the newest release from GitHub for an install from the repository.
- **A double-click launcher** (macOS, optional): "Agent Upkeep" on the Desktop runs the upkeep with every step on
  screen, then lets you press `r` to review differences or `s` to change the settings. Every run puts it back if
  it is missing and rewrites it when a new version of agent-sync changes it.
- **A small log** with a timestamp on every line keeps the last 7 days, and warns when a model you listed in
  `warn_models` (one you have no subscription for, say) shows up in any tool's settings.

It runs hourly and at login but only does the work once every 20 hours, so a day the Mac was off is caught up
soon after it starts.

> Status: 0.8.0. macOS (launchd) and Linux (systemd user timer). See the [roadmap](docs/ROADMAP.md).

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
```

In a terminal, `agent-sync setup` first offers to add installed agents you haven't enabled. It then offers to
install missing prerequisites and CLIs for all enabled agents. With `--dry-run` it lists the installation plan.
With `--yes` it installs from the current list without prompting.

On macOS, installing Homebrew requires an administrator. Once Homebrew is available, setup uses its
`claude-code`, `codex`, and `cursor-cli` casks. Gemini CLI uses the `gemini-cli` formula. Setup uses each CLI's
official installer when Homebrew is unavailable to your account. Antigravity always uses its official installer.

On Linux, `agent-sync install` sets up a systemd user timer. Run `loginctl enable-linger` if it should also run
while you are logged out.

`agent-sync run --dry-run` lists the files it would change and the commands it would run.
`agent-sync status` shows the last run, the log location, and the enabled CLI versions.
`agent-sync uninstall` removes the schedule and the launcher and turns off the launcher setting.

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
| `mirror_instructions` | `false` | copy the source tool's global instructions to the others |
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

A misspelled setting is an error, not silently ignored. On Linux the config and state follow `XDG_CONFIG_HOME`
and `XDG_STATE_HOME` when set.

## Make it yours

- **Settings first.** Most changes need only the config file, and `agent-sync config` walks you through them:
  other skill packages (`skill_packages = ["you/your-skills", "backnotprop/pstack"]`), another source tool, your
  own models for each tool's agents, parts switched off, your own update
  commands, notes on agents, or extra steps at the end.
- **Try changes safely.** `agent-sync run --dry-run` after every config change shows exactly what would happen
  (`agent-sync config` offers it after you save).
- **Change the code.** The supported agents and their settings paths are in `agent_sync/registry.py`. See `agents.py` (agent
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
| Claude Code | `~/.claude/skills/*` (links and copies), `~/.claude/agents/*.md`, `~/.claude/rules/pstack-models.md`, `~/.claude.json` (`mcpServers`), `~/.claude/CLAUDE.md` (opt-in) |
| Codex | `~/.agents/skills/*`, `~/.codex/agents/*.toml`, `~/.agents/pstack-models.md`, `~/.codex/config.toml` (`[mcp_servers.*]`, appended), `~/.codex/AGENTS.md` (opt-in) |
| Cursor | `~/.agents/skills/*`, `~/.cursor/plugins/local/<package>-agents/`, `~/.cursor/rules/pstack-models.mdc`, `~/.cursor/mcp.json`, `~/.cursor/rules/agent-sync-instructions.mdc` (opt-in) |
| Gemini CLI | `~/.agents/skills/*`, `~/.gemini/agents/*.md`, `~/.agents/pstack-models.md`, `~/.gemini/settings.json` (`mcpServers`), `~/.gemini/GEMINI.md` (opt-in) |
| Antigravity | `~/.gemini/config/skills/*`, `~/.gemini/config/agents/*.md`, `~/.agents/pstack-models.md`, `~/.gemini/config/mcp_config.json`, `~/.gemini/GEMINI.md` (opt-in) |

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
