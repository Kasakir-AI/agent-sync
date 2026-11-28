# Configuration

The easy way: run `agent-sync config` (or `agent-sync settings`). It shows every setting with its current value,
explains each one with examples, and asks what you want to change: the enabled agents, the source tool, skill packages, which parts to keep in step, agent models and notes, model warnings,
update and install commands, extra steps, and the pstack model roles. It also opens your global instructions in
your editor. Nothing is written until you save; saving keeps the comments in your file, leaves the old one next to
it as `config.toml.bak`, and then offers a dry run so you can see what the next upkeep would do.

Or edit the file yourself: copy [`config.example.toml`](../config.example.toml) to `~/.config/agent-sync/config.toml`
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
