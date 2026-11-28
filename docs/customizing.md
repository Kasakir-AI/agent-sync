# Make it yours

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
