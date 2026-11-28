# Install

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

## Check what each agent loads

`agent-sync inventory` lists, for each enabled agent, the plugins it loads (with the version and the tool each was
made for), its MCP servers (from its settings or from a plugin, with pinned package versions and newer ones), and
the number of skills it loads. It reports duplicates, outdated pins, and settings files it could not read. A
duplicate skill from two plugins awaiting a replacement approval appears as an informational message.
