# Security

agent-sync runs on your own machine with your permissions. Things to know:

- **Skill packages are code your agents will follow.** A package in `skill_packages` is installed for every enabled
  agent and its agents are mirrored. Only list packages you trust, and read what changed when they update.
- **`skills_cli = "skills@latest"` picks up new releases of the `skills` CLI automatically.** Pin a version
  (for example `"skills@1.9.0"`) if you'd rather review updates first.
- **Pinned MCP packages are updated within their major version.** A release must be at least 2 days old first, so
  a broken or compromised release does not reach every agent the hour it is published. `keep_mcp_versions` holds a
  pin. `mcp_major_updates` allows major versions.
- **agent-sync runs only the update commands you list for programs it doesn't manage.** Programs installed with
  Homebrew, npm, pipx or uv are upgraded through those tools. Any other program runs only the command you put in
  `program_updates`. A dry run runs none of them.
- **Secrets stay where they are.** An MCP server copied to another agent does not carry environment values that look
  like tokens or keys. A memory note or an instruction line that contains a secret value never leaves its tool.
  Notes about credentials or personal details stay in Claude Code unless you list them in `share_memory`. The other
  agents send their instructions to their own companies, so review what `mirror_memory` and `mirror_instructions`
  share.
- **Every change is backed up, and nothing is removed without your approval.** Backups of changed files are in
  `~/.local/state/agent-sync/backups/` and can hold copies of settings that contain tokens. The folder is readable
  only by you. New MCP settings files are created readable only by you.

Please report a vulnerability privately through GitHub's "Report a vulnerability" (Security tab) rather than in a
public issue.
