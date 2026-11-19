# agent-sync

Keep **Claude Code**, **Codex** and **Cursor** in step on one Mac (or Linux machine). Pick one of them as the source of truth, and
agent-sync makes the other two match it, once a day:

- **Skill packages** (for example [pstack](https://github.com/backnotprop/pstack)) are installed and updated for
  all three tools from one shared copy, with the [`skills`](https://www.npmjs.com/package/skills) CLI.
- **Agents** that ship with a package (`agents/*.md`) are mirrored into each tool's own format: Claude Code
  `~/.claude/agents`, Codex `~/.codex/agents/*.toml`, and a small local Cursor plugin, because Cursor only reads
  custom agents from projects and plugins. Each tool's copy runs on a model you choose for that tool, and you can
  add your own notes to an agent's prompt. Skills a package stops shipping are uninstalled.
- **Your own skills** (the ones you made in the source tool, not from a package) are copied to the other tools.
  A copy is marked as agent-sync's, so it follows the source and goes away with it; a folder you made by hand in
  the other tool is never touched.
- **pstack model roles** (`feature, refactoring: <model>` and the rest) are copied from the source tool's
  `pstack-models` file to the others, and each tool keeps its own notes on how to run each model.
- **MCP servers** at user level are added to the tools that don't have them yet. Nothing is ever deleted, and
  servers that already exist are left alone.
- **Global instructions** (optional): `CLAUDE.md` is copied into Codex's `AGENTS.md` (in a marked block, so the
  file's own text stays) and into a Cursor rule that always applies, or `AGENTS.md` the other way round.
- **Plugins**: Claude Code's plugin marketplaces and the plugins you installed, the Git plugin marketplaces you
  added to Codex, and the Cursor plugin marketplaces you added (Cursor manages its built-in ones itself).
- **The three CLIs** are updated: Claude Code and Cursor Agent through Homebrew when installed that way,
  otherwise through their own updaters, and Codex with `codex update`.
- **A small log** with a timestamp on every line keeps the last 7 days, and warns when a model you listed in
  `warn_models` (one you have no subscription for, say) shows up in any tool's settings.

It runs hourly and at login but only does the work once every 20 hours, so a day the Mac was off is caught up
soon after it starts.

> Status: 0.1.0, the first release. macOS (launchd) and Linux (systemd user timer). See the
> [roadmap](docs/ROADMAP.md).

## Install

Requires Python 3.11+, git, and Node (for `npx`). Without Homebrew or
[pipx](https://pipx.pypa.io), give it a virtual environment of its own:
`python3 -m venv ~/.local/share/agent-sync/venv && ~/.local/share/agent-sync/venv/bin/pip install
git+https://github.com/Kasakir-AI/agent-sync.git`, then link `~/.local/share/agent-sync/venv/bin/agent-sync` into a
folder on your PATH. 
```sh
brew install kasakir-ai/tap/agent-sync      # or: pipx install git+https://github.com/Kasakir-AI/agent-sync.git
agent-sync setup                  # install whatever is missing: git, Node, Claude Code, Codex, Cursor Agent
agent-sync run --dry-run          # see what it would change first
agent-sync run --force --echo     # first run, with every step on screen
agent-sync install --launcher     # schedule it (hourly + at login, works once a day); --launcher adds
                                  # a double-click "Agent Upkeep" to the Desktop (macOS)
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
location and the installed CLI versions.
`agent-sync uninstall` removes the schedule and the launcher.

## Configure

Copy [`config.example.toml`](config.example.toml) to `~/.config/agent-sync/config.toml` and change what you need.
Every key is optional:

| key | default | what it does |
| --- | --- | --- |
| `source` | `"claude"` | the tool the others mirror |
| `skill_packages` | `["backnotprop/pstack"]` | GitHub skill packages to install for all three tools |
| `skills_cli` | `"skills@latest"` | the `skills` CLI version (pin one to hold it) |
| `remove_dropped_skills` | `true` | uninstall skills a package no longer ships |
| `mirror_roles` / `mirror_mcp` / `mirror_skills` / `update_plugins` | `true` | switch parts off |
| `skip_skills` | `[]` | your own skills that stay in the source tool only |
| `mirror_instructions` | `false` | copy the source tool's global instructions to the others |
| `warn_models` | `[]` | warn when a model name containing one of these appears, e.g. `["preview-"]` |
| `update_clis` | `true` | `false`, or a list such as `["claude", "codex"]` |
| `[cli_commands]` | none | your own update command for a CLI, e.g. `codex = ["npm", "install", "-g", "@openai/codex@latest"]` |
| `[install_commands]` | none | your own install command for `agent-sync setup`, per item: `homebrew`, `git`, `node`, `claude`, `codex`, `cursor` |
| `[[extra_steps]]` | none | your own commands, run at the end of every upkeep |
| `log_days` | `7` | how long log lines are kept |
| `[agent_models]` | `opus` / `claude-opus-5-5-high-fast` / `gpt-6.2-sol` | the model each tool's copy of an agent uses |
| `[agent_notes]` | none | text added to the end of an agent's prompt in every tool, e.g. `poteto-agent = "..."` |

A misspelled setting is an error, not silently ignored. On Linux the config and state follow `XDG_CONFIG_HOME`
and `XDG_STATE_HOME` when set.

## Make it yours

- **Settings first.** Most changes need only the config file: other skill packages (`skill_packages =
  ["you/your-skills", "backnotprop/pstack"]`), another source tool, your own models for each tool's agents, parts
  switched off, your own update commands, notes on agents, or extra steps at the end.
- **Try changes safely.** `agent-sync run --dry-run` after every config change shows exactly what would happen.
- **Change the code.** Each part is one small module in `agent_sync/` with no dependencies: `agents.py` (agent
  formats per tool), `skills.py` (your own skills, dropped package skills), `roles.py` (pstack model roles),
  `mcp.py` (MCP servers), `instructions.py` (global instructions), `tools.py` (skill packages, plugins, CLI
  updates, extra steps), `checks.py` (warnings), `schedule.py` (launchd, systemd, launcher), `setup.py` (installing what
  is missing), `cli.py` (the order of the steps). To add a step, write a function that takes `log` and
  `dry`, never writes when `dry` is true, and call it from `run()` in `cli.py`. `tests/` shows how to test a part
  against a throwaway home folder (`python -m pytest`).

## Moving from your own upkeep scripts

If you already keep the tools in step with scripts of your own, move what is personal to your machine into the
config file, which stays on your machine: notes you add to agents go in `[agent_notes]`, commands only you need
go in `[[extra_steps]]`, models you never want in `warn_models`. Then:

1. `agent-sync run --dry-run` until it lists only changes you expect (ideally none).
2. Stop and remove your old scheduled job, so two jobs never write the same files.
3. `agent-sync run --force --echo`, then `agent-sync install --launcher`.

## What it changes

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

## License

MIT, see [LICENSE](LICENSE). pstack and the `skills` CLI are separate projects with their own licenses.
