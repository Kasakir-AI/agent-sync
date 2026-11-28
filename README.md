# agent-sync

Keep your AI coding agents in step: one set of skills, MCP servers, subagents, instructions and memory for
**Claude Code**, **Codex**, **Cursor**, **Gemini CLI** and **Antigravity**, kept up to date on a schedule.

You pick the agents and one source agent. agent-sync copies what you set up there to the others, installs and
updates your skill packages, plugins and CLIs, and asks before it removes anything. It runs on macOS and Linux.

> Status: 0.10.0. See the [changelog](CHANGELOG.md) and the [roadmap](docs/ROADMAP.md).

## At a glance

| What | How it's kept in step |
| --- | --- |
| Skills | Skill packages install for every agent; skills you write in the source agent are copied to the others |
| Subagents | Package agents are written in each agent's own format, with the models you choose |
| MCP servers | Added where they're missing; stale copies repaired; pinned versions moved up within their major |
| Instructions | Optional: one set of global instructions shared by every agent |
| Memory | Optional: Claude Code's memory notes shared, with private ones kept back |
| Status line | One status line with model, limits, context and cost in every CLI that supports it |
| Updates | Plugins, extensions, CLIs, MCP programs and agent-sync itself, held to your major versions |

## Install

Requires Python 3.11+, git, and Node (for `npx`).

```sh
brew install kasakir-ai/tap/agent-sync    # or: pipx install git+https://github.com/Kasakir-AI/agent-sync.git
agent-sync setup                          # find your agents; install missing prerequisites and CLIs
agent-sync run --dry-run                  # see what the first run would change
agent-sync run --force                    # first run, every step on screen
agent-sync install --launcher             # schedule it; --launcher adds "Agent Upkeep" to the Desktop (macOS)
```

The schedule wakes hourly and at login and does the work once every 20 hours, so a day the computer was off is
caught up soon after it starts. On Linux it is a systemd user timer: run `loginctl enable-linger` to let it run
while you're logged out. Installing without Homebrew or pipx, setup options and uninstalling: [install](docs/install.md).

## Everyday commands

```sh
agent-sync review                 # compare settings across agents in a local page and choose what to keep
agent-sync pending                # what's waiting for your approval, and recent warnings
agent-sync pending --approve KEY  # let the next run go ahead with one item (--keep KEY: never)
agent-sync inventory              # what each agent loads, duplicates and outdated pins
agent-sync status                 # the last run, versions, log and backup size
agent-sync config                 # change the settings by answering questions
agent-sync agents add gemini      # keep another agent in step (agents remove ... to stop)
agent-sync skill suggest          # skills that fit the project you're in
agent-sync run --dry-run          # what the next run would change, without changing it
```

## Skills

agent-sync comes with a catalog of 140+ skills for 80+ technologies. A skill is a small bundle of instructions
that teaches an agent one kind of work. agent-sync spots what a project uses from its manifests and files
(`package.json`, `pyproject.toml`, `Cargo.toml`, a `Dockerfile` and more) and suggests the skills that fit.

```sh
# Spot the project's technologies and see the skills that fit
agent-sync skill suggest

# Install a skill
agent-sync skill install docker-expert

# Install every suggestion without asking
agent-sync skill suggest --install --all

# Search the catalog
agent-sync skill list kubernetes
```

- **[Kasakir-AI/agent-sync-skills](https://github.com/Kasakir-AI/agent-sync-skills)** holds the catalog's own
  skills. Improvements and new skills are welcome as pull requests.
- **Skills from other makers**, such as Angular, Vercel, Cloudflare, Expo, Stripe and Anthropic, install from
  their makers' own repositories, so you get the original, not a copy.
- **Every skill was read before it was listed**, scripts included, and is pinned to the commit that was read. The
  `skills` CLI records a hash of what it installed. Skills that steered agents to paid services, installed other
  skills on their own, ran commands without asking, or gave unsafe advice were left out; the list and the reasons are
  in [`catalog/excluded.toml`](catalog/excluded.toml).

Skills install into the project you're in, for each agent you keep in step. Add `--global` to install for every
project. `agent-sync skill install owner/repo/name` installs a skill the catalog doesn't list (`name` is the one in
its `SKILL.md`), from that repository's newest commit, unpinned.

Skill packages you want everywhere go in `skill_packages` (default: [pstack](https://github.com/backnotprop/pstack)).
Upkeep installs and updates them for every agent. If a package install would replace a skill it didn't install,
such as one you wrote, upkeep puts yours back and asks whether the package's version should replace it.

## Approvals and backups

- A run never removes something you or another program installed, and never installs a new major version, until
  you approve it. It asks in the terminal, in a desktop window, or in the
  [Plannotator](https://github.com/backnotprop/plannotator) Inbox when you have it.
- Before changing a file, a run saves the original under `~/.local/state/agent-sync/backups/`. Backups expire
  after 30 days or when they pass 500 MiB, starting with the largest.
- Values that look like secrets stay in the agent where they are: they aren't copied to MCP settings, shared
  memory or instructions.

Details: [approvals and backups](docs/approvals-and-backups.md), [memory and privacy](docs/memory-and-privacy.md).

## Review differences

`agent-sync review` opens a local page that compares instructions, rules, model roles, skills and MCP servers
across your agents. Each card says in one sentence what differs. Pick a version to keep, check the exact lines
each file gains or loses, and apply. Only the lines that differ are written, and every changed file is backed up
first. `agent-sync review --list` prints the same comparison in the terminal. More in [review](docs/review.md).

## Configure

Run `agent-sync config` to change settings by answering questions, or edit `~/.config/agent-sync/config.toml`
yourself (start from [`config.example.toml`](config.example.toml)). Every key is optional. The most used:

| Key | Default | What it does |
| --- | --- | --- |
| `agents` | `["claude", "codex", "cursor"]` | the agents to keep in step |
| `source` | `"claude"` | the agent the others follow |
| `skill_packages` | `["backnotprop/pstack"]` | skill packages installed for every agent |
| `mirror_instructions` | `false` | share global instructions |
| `mirror_memory` | unset | share Claude Code's memory notes (asked once) |
| `keep_installed` | `[]` | items upkeep never removes or replaces |
| `update_global_tools` | `false` | also update your global npm, pipx, uv (and Homebrew) tools |

Every setting: [configuration](docs/configuration.md). Your own steps, models and notes:
[make it yours](docs/customizing.md). The full list of what's kept in step and how:
[what it keeps in step](docs/what-it-keeps-in-step.md). The status line: [status line](docs/status-line.md).

## What it changes

| Agent | Files |
| --- | --- |
| Claude Code | `~/.claude/skills/*`, `~/.claude/agents/*.md`, `~/.claude/rules/pstack-models.md`, `~/.claude.json` (`mcpServers`), `~/.claude/settings.json` (`statusLine`), `~/.claude/CLAUDE.md` (opt-in) |
| Codex | `~/.agents/skills/*`, `~/.codex/agents/*.toml`, `~/.agents/pstack-models.md`, `~/.codex/config.toml` (`[mcp_servers.*]`, `[tui] status_line`), Codex plugins, `~/.codex/AGENTS.md` (opt-in, memory index) |
| Cursor | `~/.agents/skills/*`, `~/.cursor/plugins/local/<package>-agents/`, `~/.cursor/rules/pstack-models.mdc`, `~/.cursor/mcp.json`, `~/.cursor/cli-config.json` (`statusLine`), `~/.cursor/rules/agent-sync-instructions.mdc` (opt-in, memory index) |
| Gemini CLI | `~/.agents/skills/*`, `~/.gemini/agents/*.md`, `~/.agents/pstack-models.md`, `~/.gemini/settings.json` (`mcpServers`), `~/.gemini/GEMINI.md` (opt-in, memory index) |
| Antigravity | `~/.gemini/config/skills/*`, `~/.gemini/config/agents/*.md`, `~/.agents/pstack-models.md`, `~/.gemini/config/mcp_config.json`, `~/.gemini/antigravity-cli/settings.json` (`statusLine`), Antigravity plugins, `~/.gemini/GEMINI.md` (opt-in, memory index) |
| All | `~/.agents/memory/*` (memory copies), MCP server programs and global tools through their package managers |

`agent-sync skill install` writes only into the project's skill folders (`.claude/skills`, `.agents/skills` and
the like), or into the ones above with `--global`.

State, backups and pending approvals live in `~/.local/state/agent-sync/`. The log is
`~/Library/Logs/agent-sync.log` on macOS and `~/.local/state/agent-sync/agent-sync.log` on Linux.

## Safety

Skills, skill packages and the `skills` CLI run with your agents' full permissions, so install only what you
trust and read a skill before you rely on it. You can pin `skills_cli` to choose when it updates. Read
[SECURITY.md](SECURITY.md) for how to report a problem.

## Support

agent-sync is free. If it helps you, you can [star the repo](https://github.com/Kasakir-AI/agent-sync), [buy me a coffee on Ko-fi](https://ko-fi.com/kasakir), or [sponsor on GitHub](https://github.com/sponsors/Kasakir-AI), once or monthly.

### Sponsors

<!-- sponsors:start -->
<!-- This section is updated automatically. Edits here are overwritten. -->

No sponsors featured here yet. [Become the first](https://github.com/sponsors/Kasakir-AI)!
All sponsors: [SPONSORS.md](https://github.com/Kasakir-AI/.github/blob/main/SPONSORS.md).
<!-- sponsors:end -->

## License

MIT, see [LICENSE](LICENSE). Skills in the catalog keep their makers' licenses. pstack and the `skills` CLI are
separate projects with their own licenses.
