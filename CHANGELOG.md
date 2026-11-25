# Changelog

## 0.9.0 (2026-11-25)

New: every agent gets the right version of each plugin, skill and MCP server, each loaded once and kept up to date,
with a backup before every change and nothing removed until you approve it.

**Loaded once, in each tool's own version**

- `agent-sync inventory` lists, for each enabled agent, the plugins it loads (version, which tool each was made
  for), its MCP servers (from its settings or from a plugin, with pinned package versions and newer ones), the
  number of skills it loads, and every problem found. It changes nothing.
- Codex online-catalog plugins now count: Codex loads them, so their skills and MCP servers are checked for
  duplicates. Antigravity plugins and Gemini CLI extensions count too.
- An MCP server a tool's settings list and one of its plugins also brings is loaded twice. When the settings
  entry is an exact copy, upkeep asks you to approve removing it. Otherwise it warns. The source tool's settings
  are never changed this way, because the other tools copy from them.
- A plugin installed twice in one tool, from two catalogs, is reported.
- Codex uses a plugin made for Codex in place of a Claude Code-format copy when the Codex one is not older and keeps
  every skill, MCP server, command, agent and hook. Otherwise both stay and the run says why.

**Kept up to date**

- Every installed skill is updated, whichever repository it came from, not only the packages in `skill_packages`.
- Antigravity plugins are reinstalled when its catalog has a newer version. A release never moves to a prerelease.
- Programs that MCP servers run are upgraded the way they were installed: Homebrew, npm, pipx or uv
  (`update_mcp_programs`). A program with its own updater runs it only when you list it in `program_updates`.
  Interpreters such as `python3` or `node` are never upgraded on an MCP server's behalf.
- A pinned package in an MCP server (`npx -y name@1.2.3`) moves to the newest release in the same major version
  once that release is at least 2 days old. `mcp_major_updates = true` allows new major versions.
  `keep_mcp_versions` holds a server's pin.
- npm itself is updated when it is installed globally. `update_global_tools` (off by default) also updates your
  other global npm, pipx and uv tools, and Homebrew packages when you list `"homebrew"`.
- Homebrew refreshes once per run instead of once per step.

**Instructions and memory**

- With `mirror_instructions = true`, every agent's instruction file contributes: a line added in any of them
  reaches the others, next to the lines around it. A rewording in the source tool's file reaches the others too.
- A line is never deleted from another tool on its own. When another tool drops or rewords a shared line (Codex and
  Gemini do when they remember something), the others keep it and the run warns until you choose in
  `agent-sync review`. A large deletion in the source, a missing source file, or an empty one is held for approval.
- Lines between `<!-- agent-sync: local -->` and `<!-- agent-sync: end local -->` stay in their file. So do lines
  between `<!-- agent-sync: private -->` and `<!-- agent-sync: end private -->`.
- Claude Code's memory is shared with the other agents (`mirror_memory`, on by default): the notes are copied to
  `~/.agents/memory/`, and the other agents' instructions get the memory index.
- Notes that mention credentials, tokens, keys, legal names or similar personal details stay in Claude Code. The run
  lists them. `private_memory` and `private: true` keep a note private. `share_memory` shares a flagged note. A note
  or an instruction line that contains a secret value, such as a token or a private key, never leaves its file.
- An MCP server copied to another tool no longer carries environment values that look like secrets. The run says
  which variable to set there.

**Safety**

- Every run saves the original of each file it changes in `~/.local/state/agent-sync/backups/run-<date-time>/`,
  kept for 30 days (`backup_days`). Folders are saved as APFS clones on macOS, which take almost no space.
- Nothing you or another program installed is removed until you approve it. A scheduled run asks in a macOS window
  where you pick what may go. In a terminal it asks yes or no. Your Plannotator Inbox gets buttons to approve or
  keep each removal, when Plannotator is installed. `agent-sync pending` lists what waits and takes
  `--approve`, `--keep` and `--ack`. `keep_installed` keeps an item for good.
- New warnings appear in a macOS alert that stays until you click **OK**, and in the Inbox. A warning you
  acknowledged is not shown again until it changes. `notify = false` turns off the window and the Inbox messages.
- Settings files are written through one checked writer. When Claude Code or another tool changes a file while a run
  updates it, the run plans the change again from the new contents instead of overwriting them. New MCP settings
  files are readable only by you.
- One failed step no longer stops the rest of the run.

**Status line**

- `agent-sync-statusline` draws one status line for Claude Code, Cursor CLI and Antigravity: the model and mode,
  the 5-hour and weekly limits with their reset times, the context left, the cost, and running agents and tasks.
  It uses one line when the terminal is wide and wraps between parts on narrow terminals.
- Upkeep sets it up in those tools and sets Codex's built-in status items (`status_line`, on by default).
  Replacing a status line of your own needs your approval. Gemini CLI has no custom status line.

## 0.8.1 (2026-11-25)

- A package agent whose description spans several lines (`description: >` or `|`) keeps that description in
  its Gemini CLI and Antigravity copies. Before, those copies got an empty description.
- The README says which skills reach Codex and Cursor when Gemini CLI is the source.

## 0.8.0 (2026-11-25)

- Keep any selection of Claude Code, Codex, Cursor, Gemini CLI, and Antigravity in step. Existing settings still
  use the first three by default.
- Run `agent-sync agents` to see which agents are installed and add them. Use `agents add`, `agents remove`,
  or the **Agents** section in `agent-sync config` to change the list. Removing an agent keeps its files.
- Setup offers to add installed agents and installs missing enabled CLIs. Review shows shared settings once
  with the names of the enabled agents that use them.
- On a Google AI Pro or other Google One plan, add `antigravity`: Google replaced Gemini CLI with the Antigravity CLI
  (`agy`) for those plans. Antigravity and Gemini CLI share `~/.gemini/GEMINI.md`, so it is written once.
- An empty MCP settings file (Antigravity creates one) counts as having no servers instead of stopping the MCP step.

## 0.7.1 (2026-11-24)

- A shared instruction you reword in Codex's `AGENTS.md` (or that Codex rewords when it remembers something) is no
  longer put back to the source's wording by the next run. It is kept, and the run warns until `CLAUDE.md` has the
  same wording, so the other tools get it too. Changes made in the source still reach every tool.
- The review page recommends the version that reworded a shared line since the last sync, instead of deciding by
  file dates. An agent-sync copy never wins over your own edits this way.
- Copies of an MCP server an app rewrites on its updates, such as Codex's `node_repl`, follow its newest version.
  They used to be replaced only when the old version's folder was gone; now a copy that differs only in an older
  app version (a versioned folder in a path, or a setting named like `APP_VERSION`) is brought up to date too, and
  the review recommends the newest. Other differences, such as addresses, are left for you to choose.
- Each run upgrades git and Node when Homebrew installed them and a newer version is out, next to the CLIs.

## 0.7.0 (2026-11-24)

New: no tool loads a skill twice, stale MCP server copies are replaced, and mirrored instructions are merged
instead of repeated.

- Skills inside Claude Code plugins are no longer copied to Codex and Cursor. Cursor loads them from Claude Code
  itself, so the copy made Cursor load each one twice. Copies made by earlier versions are removed. The
  `plugin_skills` setting is retired: while it is still set, a run says so. To give Codex a plugin's skills,
  install the plugin in Codex as well (`codex plugin list` shows what it offers).
- A skill is never copied where a tool already loads a skill of that name, and the run says which tool has it.
- An MCP server copy that points at files that no longer exist is replaced with the working version when every
  working copy agrees, and the original is backed up. This happens when an app updates a server it owns, such as
  Codex's `node_repl`, while the copies in the other tools keep the old folder. Only a missing program or a path
  whose folder is gone counts, so a log file a server has not created yet does not. The review page marks such a
  copy stale and never recommends it.
- Mirrored global instructions are merged into `AGENTS.md` (or `CLAUDE.md`) line by line instead of being added
  as a marked block below the file's own text. A file that already had most of the instructions by hand used to
  end up with each one twice. A line you delete from the source now leaves the other file too, and lines only
  that file has stay. A run folds an old block into the merged text.
- Updating an MCP server in `~/.claude.json` or Cursor's `mcp.json` changes only that server's lines. It used to
  rewrite every line with a non-ASCII character, such as a long dash.
- A run ends with what it did: how many files it changed, commands it ran, warnings and problems. Warnings used to
  be easy to miss.
- A copy installed from GitHub with pip or pipx updates itself to the newest release, not to whatever is on the
  main branch, so a change reaches it only once it is released.
- `agent-sync run` prints each step when you start it from a terminal, so `--echo` is only needed elsewhere. A
  run that is not due yet says when the last one was and how to run now.
- `agent-sync review --list` lists the settings that need a choice first. Settings that are the same in every
  tool fold into one line per kind.
- The review page opens short differences on the cards that need a choice, so you see what differs without a
  click. Tabs show how many settings each holds, and facts every version shares are said once per card. The
  Plugins tab shows what is installed and no longer offers to share plugin skills.

## 0.6.0 (2026-11-23)

New: the upkeep warns when a tool loads the same skill twice.

- Every upkeep checks the skills Claude Code, Codex and Cursor each load, from their skill folders and their
  plugins, and warns about any name a tool gets from two places. Each warning names the skills and where both
  copies come from, for example a pack installed both with the `skills` CLI and as a Codex plugin.
- Skills switched off in Codex and plugins switched off in Claude Code don't count. Skills you keep twice on
  purpose can go in the new `allow_duplicate_skills` setting (or in `agent-sync config`).
- When two packages in `skill_packages` ship a skill with the same name, the package listed first keeps it. The
  later package's copy is no longer installed, and one installed before is removed.
- A plugin skill is no longer copied to Codex and Cursor when both already load it from somewhere else. A copy
  that Codex switched off counts as not needed there.
- A plugin update that leaves a skill's files the same no longer copies the skill again on every run.

## 0.5.2 (2026-11-20)

- Applying global instructions clears the old mirrored block and keeps each tool's own notes.
- Skill helpers keep their executable permissions when review copies them.
- The next upkeep preserves skill notes and files kept during review.
- Rules scoped to particular files stay separate in review.

## 0.5.1 (2026-11-20)

- The upkeep no longer warns on every run about skills a plugin or installer made separately for each tool (for
  example a Claude Code version using `allowed-tools` next to a Codex version with `agents/openai.yaml`), or about a
  folder that is already the same. A folder you made by hand that differs is still reported.

## 0.5.0 (2026-11-20)

New: `agent-sync review`, to see how your settings differ between the tools and choose what to keep.

- `agent-sync review` compares settings across the tools in a local page. See the origin of each version, choose
  what to keep, and preview changes before applying them. The review backs up the files it replaces.
- `agent-sync review --list` prints the comparison in the terminal. `--no-open` prints the page's local URL.
- The upkeep log reports settings that differ or are missing. Press `r` in the Desktop launcher to review them.
- Keeping a version writes only the differences. Notes, lines and files that only one tool has are kept.
- Lines only one tool has are shown once under **Check once**, so old rules can't hide among a tool's own notes.
  After you check them, the setting counts as the same until a new line shows up.
- Skills a plugin or installer writes separately for each tool are shown as made for each tool and left alone.
  Plugin skill copies and MCP servers from Claude Code plugins are no longer shown as missing.
- Each card says in one sentence what differs. Review changes shows the exact lines each file gains or loses.
- The page fits a phone screen.
- A line you delete from the version you keep is removed from the other tools by default. The review remembers
  the version it last kept everywhere, so it can tell a deleted line from a tool's own note. A Keep / Remove switch
  on each line that only one tool has lets you decide yourself.

## 0.4.1 (2026-11-20)

Fixes in `agent-sync config`:

- A command typed with `~` (like the example `git -C ~/dotfiles pull --ff-only`) now runs: `~` becomes your home
  folder when you type it, and the wizard shows the full path before saving.
- A table name written in quotes is recognized, so removing an extra step no longer deletes that table.
- A multi-line note that ends in a quote mark no longer wipes the settings after it when edited.
- Emoji in notes and commands are saved as valid TOML instead of being refused.
- Changing the source tool in the settings file (`e`) now also switches the pstack roles file that `r` edits.
- Switching off the last plugin that is no longer installed is saved instead of being dropped.
- The extra-step time limit question now says what Enter means: 900 seconds (15 minutes).

And in the upkeep:

- Emoji in a skill package's agents or in an MCP server's settings are now written to Codex as valid TOML, so
  they can no longer leave a Codex agent file or `~/.codex/config.toml` unreadable.

## 0.4.0 (2026-11-19)

- agent-sync now updates itself as part of the upkeep, the way you installed it: `brew upgrade` for the Homebrew
  formula, `pipx upgrade`, or a fresh install from GitHub. `update_self = false` turns this off.
- The Desktop launcher stays put and up to date: with `launcher = true` (set by `install --launcher` and the new
  `agent-sync launcher`), every run puts "Agent Upkeep" back if it is missing and rewrites it when a new version
  changes it, right after agent-sync updates itself.
- The launcher runs the upkeep, then lets you press `s` to change the settings.
- `agent-sync uninstall` switches the launcher setting off, so the launcher stays gone.

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
