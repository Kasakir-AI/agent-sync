# Roadmap

Planned before 0.1.0 is published:

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
      login, launcher, uninstall. The run started at login was stopped by the cleanup before it finished.
- [x] CI: lint (ruff) and the tests on macOS and Linux.
- [ ] Publish 0.1.0 (GitHub release, Homebrew tap formula).

Later:

- Skills that come inside Claude Code plugins, offered to Codex and Cursor (opt-in per plugin; most plugins only
  make sense in the tool they were written for).
- Cursor as the source of global instructions (its User Rules live in the app's own database).
