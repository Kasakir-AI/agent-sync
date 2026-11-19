# Security

agent-sync runs on your own machine with your permissions. Things to know:

- **Skill packages are code your agents will follow.** A package in `skill_packages` is installed for all three
  tools and its agents are mirrored. Only list packages you trust, and read what changed when they update.
- **`skills_cli = "skills@latest"` picks up new releases of the `skills` CLI automatically.** Pin a version
  (for example `"skills@1.9.0"`) if you'd rather review updates first.
- **MCP servers are copied with their environment variables.** If a server's `env` holds a token, the copy in
  `~/.cursor/mcp.json` or `~/.codex/config.toml` holds it too. These files stay on your machine; keep them
  out of any dotfile repository you publish.
- agent-sync never deletes MCP servers, agents it didn't write, or skills that didn't come from a listed package.

Please report a vulnerability privately through GitHub's "Report a vulnerability" (Security tab) rather than in a
public issue.
