# Security

agent-sync runs on your own machine with your permissions. Things to know:

- **Skill packages are code your agents will follow.** A package in `skill_packages` is installed for every enabled
  agent and its agents are mirrored. Only list packages you trust, and read what changed when they update.
- **`skills_cli = "skills@latest"` picks up new releases of the `skills` CLI automatically.** Pin a version
  (for example `"skills@1.9.0"`) if you'd rather review updates first.
- **Packages agent-sync upgrades are updated within their major version.** This covers pinned MCP packages, the
  programs MCP servers run, global tools, git, Node and npm. A release must be at least 2 days old first, so a broken or
  compromised release does not reach every agent the hour it is published (Homebrew reviews its releases, so they
  have no wait). A newer major version waits for your approval. `keep_mcp_versions` holds a pin.
  `mcp_major_updates` and `global_major_updates` allow major versions without asking.
- **agent-sync runs only the update commands you list for programs it doesn't manage.** Programs installed with
  Homebrew, npm, pipx or uv are upgraded through those tools. Any other program runs only the command you put in
  `program_updates`. Upkeep backs up the resolved executable and its dedicated install folder, when present,
  before running a listed command. A dry run runs none of them.
- **Secrets stay where they are.** An MCP server copied to another agent does not carry environment values that look
  like tokens or keys. A memory note or an instruction line that contains a secret value never leaves its tool.
  Upkeep asks you before sharing Claude Code's memory and saves your answer in `mirror_memory`. Notes about
  credentials or personal details stay in Claude Code unless you list them in `share_memory`. The other agents
  send their instructions to their own companies, so review what `mirror_memory` and `mirror_instructions` share.
  Upkeep manages memory copies by manifest and skips symlinks. It checks the copy for concurrent edits before
  replacing it.
- **Upkeep backs up your files and asks before removing installed items.** Backups of changed files live in
  `~/.local/state/agent-sync/backups/` and can hold settings that contain tokens. New backup folders and MCP
  settings files are readable only by you. Retention removes expired backups, obsolete package checkouts, and
  old records that agent-sync owns. It skips symlinked backup folders and entries and warns you. All instruction
  edits wait for approval or a keep decision on the exact deletion batch, including the count of each repeated
  line. The instruction baseline records hidden section lines as fingerprints. MCP duplicate approvals expire
  when upkeep confirms the original item is gone from its own settings file.

Please report a vulnerability privately through GitHub's "Report a vulnerability" (Security tab) rather than in a
public issue.
