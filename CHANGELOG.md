# Changelog

## 0.3.0 (2026-11-19)

- `agent-sync config` (or `agent-sync settings`): change every setting by answering questions in the terminal,
  with an explanation and examples for each. It also changes the pstack model roles and opens your global
  instructions in your editor. Nothing is written until you save; saving keeps the comments in your settings
  file, leaves the old file as `config.toml.bak`, and offers a dry run afterwards.
- Skills inside Claude Code plugins can be copied to Codex and Cursor: name the plugins in `plugin_skills`
  (opt-in, because most plugins only make sense in Claude Code). A copy goes away when you take the plugin off
  the list or uninstall it. `agent-sync status` lists the installed plugins that have skills.
- A link in a skills folder that agent-sync didn't make is now left alone with a warning, instead of making the
  run fail.

## 0.2.0 (2026-11-19)

- `agent-sync setup` installs what is missing, after asking: Homebrew (on a Mac, with an administrator's
  password), git, Node, and Claude Code, Codex and Cursor Agent. They come from Homebrew when your account can
  use it, otherwise from each tool's official per-user installer. `--dry-run` lists what it would install;
  `[install_commands]` in the config replaces any install command.
- Codex installed with Homebrew (`codex` cask) is upgraded with Homebrew.
- The daily run notes a missing CLI and points to `agent-sync setup`.
- The Homebrew formula now brings Node along.

## 0.1.0 (2026-11-19)

First release.

- Skill packages installed and updated for Claude Code, Codex and Cursor from one shared copy (`skills` CLI).
- Package agents mirrored into each tool's format, with a model per tool; Cursor gets them as a local plugin.
- pstack model roles mirrored from the source tool.
- User-level MCP servers added to the tools that lack them (never deleted).
- Claude Code, Codex and Cursor Agent updated (Homebrew when installed that way).
- Plugin updates: Claude Code marketplaces and installed plugins, Cursor marketplaces you added.
- `agent-sync run --dry-run` shows every file and command a run would change, and changes nothing.
- Customization in the config: which CLIs to update, your own update command per CLI, and extra steps.
- Your own skills (not from a package) copied from the source tool to the others, marked so they follow the source.
- Skills a package stops shipping are uninstalled.
- Your own notes on package agents (`[agent_notes]`), and warnings for models you never want (`warn_models`).
- Global instructions mirrored on request: CLAUDE.md, Codex AGENTS.md (in a marked block) and a Cursor rule.
- Codex plugin marketplaces are refreshed along with the Claude Code and Cursor ones.
- Daily schedule on macOS (launchd) and Linux (systemd user timer): hourly and at login, works once every
  20 hours; a 7-day log; `install --launcher` adds a double-click launcher on the Mac Desktop.
- A misspelled setting is reported instead of ignored; Linux follows the XDG folders.
- A CLI installed by a Homebrew that another account owns is left alone (with a note) instead of failing or being
  reinstalled by its own updater; `brew update` only runs when a Homebrew copy will be upgraded.
- A dry run and `status` never start a CLI that has not run in this account yet (it would create its settings).
- The schedule and the launcher start the installed `agent-sync` command, so they keep working after upgrades.
- `agent-sync --version`.
