# What agent-sync keeps in step

- **Skill packages** such as [pstack](https://github.com/backnotprop/pstack) are installed and updated for the
  enabled agents using the [`skills` CLI](https://www.npmjs.com/package/skills). When two packages ship a skill
  with the same name, the first package in `skill_packages` keeps it. Upkeep removes skills a package drops.
  If installing a package would replace a skill that package didn't install, such as one you wrote, upkeep puts
  yours back and asks whether the package's version should replace it.
- **Package subagents** (`agents/*.md`) are written in each enabled agent's format. Cursor gets a local
  subagent plugin. You can set models for Claude Code, Codex, Cursor, and Gemini CLI and add notes to the
  subagent prompts. Gemini CLI and Antigravity keep multiline descriptions as written. Antigravity chooses its own model.
  Each agent file carries a one-line agent-sync mark. A same-name agent without the mark, or one you edited, is
  replaced only after you approve it.
- **Your own skills** (the ones you made in the source tool, not from a package) are copied to the other tools.
  A copy is marked as agent-sync's, so it follows the source and goes away with it; a folder you made by hand in
  the other tool stays in place. Upkeep warns if a file or folder blocks a copy and continues with the other skills.
  With Gemini CLI as the source, skills only in `~/.gemini/skills` reach
  Antigravity but not Codex or Cursor, which share Gemini CLI's other folder (`~/.agents/skills`): keep a skill
  there to share it with them.
- **Loaded once**: a shared copy is left out only when every tool that reads its folder loads an identical skill elsewhere. Every upkeep
  checks each tool for a skill name it loads from two places, a plugin installed twice from two catalogs, and an
  MCP server it loads twice (from its settings and from a plugin), and names both places. Codex online-catalog
  plugins, Antigravity plugins and Gemini CLI extensions count. Codex skills you switched off and plugins you
  disabled don't. Cursor reads Claude Code's skills and plugins too unless you turned off its third-party setting.
  Skills you keep twice on purpose go in `allow_duplicate_skills`.
- **Each tool's own version**: Codex uses a plugin made for Codex in place of a Claude Code-format copy when the
  Codex one is not older and keeps every skill, MCP server, command, agent and hook invocation. Otherwise both stay and the
  run says why.
- **pstack model roles** (`feature, refactoring: <model>` and the rest) are copied from the source tool's
  `pstack-models` file to the others, and each tool keeps its own notes on how to run each model.
- **MCP servers** at user level are added to the tools that don't have them yet. A server a tool's plugin already
  brings is not added again. An exact settings copy of a plugin's server can be removed after you approve it.
  Environment values that look like secrets are not copied: the run tells you which variable to set. A server with a
  credential in its arguments or URL is not copied at all, and review never offers to copy one. Upkeep brings
  copies of an app's server, such as Codex's `node_repl`, to the newest working version when only the app version
  differs. It does the same after you move a folder a server runs from: copies that still name the old place are
  pointed at the new one. Settings such as endpoint URLs and log destinations stay for review. A pinned package
  (`npx -y name@1.2.3`) moves to the newest release in the same major version once it is at least 2 days old.
  A plugin suppresses a pin update only when its server settings match apart from the pin version.
- **Global instructions** (optional, `mirror_instructions`) are shared by every enabled agent. A line added in any
  agent's file reaches the others. A deletion in the source file pauses mirroring until you choose in
  `agent-sync pending`. If another tool drops or rewords a shared line, the other copies stay until you choose in
  `agent-sync review`, even if the same line appears elsewhere in the file. Gemini CLI and Antigravity share
  `~/.gemini/GEMINI.md`. Cursor receives an always-applied rule.
- **Memory**: you choose whether to share Claude Code's memory notes with the other agents. Notes about
  credentials, keys or personal details stay in Claude Code by default. See [Memory and privacy](memory-and-privacy.md).
- **Plugins and extensions** are updated for enabled agents: Claude Code's plugins and marketplaces, Git plugin
  marketplaces in Codex, user marketplaces in Cursor, Antigravity plugins (from its catalog), and Gemini CLI
  extensions.
- **Skills** from every source are updated (`skills update`), not only the packages in `skill_packages`.
- **MCP server programs** are upgraded the way they were installed (Homebrew, npm, pipx or uv). A program with its
  own updater runs it when you list it in `program_updates`.
- **Upgrades stay in the installed major version.** MCP programs, global tools, git, Node and npm move to the
  newest release in the major version you have, once that release is at least 2 days old. A newer major version
  can include breaking changes, so upkeep asks you first (see [Approve removals](approvals-and-backups.md));
  `global_major_updates` installs it without asking, still after 2 days. Explicit keep rules still apply.
  Homebrew has no 2-day wait, because Homebrew reviews a release before publishing it. A pipx tool installed from a URL
  or a folder, or with its own pip arguments (such as a private index) or injected packages, is left alone. So is a
  global npm tool that is linked to a folder or at a version the npm registry never published (installed from git,
  a tarball or a fork), and a uv tool whose receipt names a git, folder or index source or can't be read. Pipx upgrades use the tool's existing suffix, and uv upgrades keep its extras and
  additional dependencies. Both choose releases compatible with the tool's Python interpreter when its version is known. A uv tool
  installed with a version range or with excluded dependencies is left for you to upgrade.
- **CLIs** are updated through Homebrew when it installed them or through their own update commands. Gemini CLI
  uses npm outside Homebrew. Antigravity uses `agy update`. Upkeep also upgrades git and Node when Homebrew
  installed them, and npm when it is installed globally. `update_global_tools` (off by default) also updates your
  other global npm, pipx and uv tools, and Homebrew packages when you list `"homebrew"`.
- **agent-sync itself** is updated the way you installed it: `brew upgrade` for the Homebrew formula,
  `pipx upgrade`, or the newest release from GitHub for an install from the repository.
- **A double-click launcher** (macOS, optional): "Agent Upkeep" on the Desktop runs the upkeep with every step on
  screen, then lets you press `r` to review differences or `s` to change the settings. Every run puts it back if
  it is missing and rewrites it when a new version of agent-sync changes it.
- **A status line** in Claude Code, Cursor CLI and Antigravity, and Codex's built-in status items. See
  [Status line](status-line.md).
- **Backups and approvals**: upkeep saves your files before changing them. You approve removals of installed items.
  See [Approve removals](approvals-and-backups.md).
- **A small log** with a timestamp on every line keeps the last 7 days, and warns when a model you listed in
  `warn_models` (one you have no subscription for, say) shows up in any tool's settings.

It runs hourly and at login but only does the work once every 20 hours, so a day the Mac was off is caught up
soon after it starts.
