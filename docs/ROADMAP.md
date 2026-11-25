# Roadmap

0.1.0 (published 2026-11-19):

- [x] Plugins: Claude Code marketplaces and installed plugins, and the Cursor marketplaces the user added.
- [x] Plugins: Codex Git plugin marketplaces (`codex plugin marketplace upgrade`).
- [x] `agent-sync run --dry-run`: show every change without writing anything.
- [x] Customization: CLI selection, per-CLI update commands, extra steps, agent notes, model warnings,
      "Make it yours" guide.
- [x] Mirror other skills that live only in the source tool (not from a package) to the other tools.
- [x] Uninstall skills a package no longer ships.
- [x] Mirror the tools' global instructions (CLAUDE.md, Codex AGENTS.md, a Cursor rule) on request.
- [x] Desktop launcher installed by `agent-sync install --launcher`.
- [x] Linux: a systemd user timer (`agent-sync install` on Linux) and XDG locations.
- [x] Checked end to end in an empty home folder (real package install, stubbed CLIs).
- [x] Verified on a fresh macOS user account (standard user, no setup): install, dry run, first run, launchd at
      login, launcher, uninstall.
- [x] CI: lint (ruff) and the tests on macOS and Linux.
- [x] Publish 0.1.0 (GitHub release, Homebrew tap formula).

0.2.0 (published 2026-11-19):

- [x] `agent-sync setup`: install what is missing (Homebrew, git, Node, Claude Code, Codex, Cursor Agent).

0.3.0 (published 2026-11-19):

- [x] Skills that come inside Claude Code plugins, offered to Codex and Cursor (opt-in per plugin with
      `plugin_skills`; most plugins only make sense in the tool they were written for).
- [x] `agent-sync config`: change the settings by answering questions in the terminal.

0.4.0 (published 2026-11-19):

- [x] agent-sync updates itself (Homebrew, pipx, or a pip install from GitHub).
- [x] The Desktop launcher is put back and rewritten by every run when `launcher = true`.

0.4.1 (published 2026-11-20):

- [x] Fixes in `agent-sync config` and in the TOML written for Codex, from code review (see the changelog).

0.5.0 (published 2026-11-20):

- [x] `agent-sync review`: compare settings across the tools, see where each version came from, and apply your
      choices with backups.

0.5.1 (published 2026-11-20):

- [x] No upkeep warning for skills made separately for each tool.

0.6.0 to 0.8.1 (published 2026-11-23 to 2026-11-25):

- [x] Warn about skills a tool loads twice; the first package keeps a shared skill name.
- [x] Replace stale MCP copies; merge mirrored instructions line by line.
- [x] Gemini CLI and Antigravity; choose which agents to keep in step.

0.9.0 (published 2026-11-25):

- [x] `agent-sync inventory`: plugins, MCP servers and skills per agent, and what is loaded twice.
- [x] Each tool's own plugin version; MCP servers and plugins loaded once.
- [x] Update every skill, Antigravity plugin, MCP server program, pinned MCP package and npm; global tools on
      request.
- [x] Instructions shared by every agent; Claude Code's memory shared, with sensitive notes kept private.
- [x] A backup before every change; removals only after approval by click, terminal or Plannotator Inbox.
- [x] One status line in Claude Code, Cursor CLI and Antigravity.

Later:

- Cursor as the source of global instructions (its User Rules live in the app's own database).
