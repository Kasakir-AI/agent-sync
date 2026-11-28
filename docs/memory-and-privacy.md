# Memory and privacy

Claude Code keeps long-term memory as notes in `~/.claude/projects/<your home folder>/memory/`. When it has notes
and another agent is enabled, upkeep asks if you want to share them. You can answer in the terminal or a desktop
window. If the choice is still open, the Plannotator Inbox offers **Share** and **Don't share**.
You can also run `agent-sync pending --approve memory-sharing` or `agent-sync pending --keep memory-sharing`.

Upkeep saves your choice as `mirror_memory = true` or `mirror_memory = false`. You can set either value yourself
to skip the prompt. While you have a choice waiting, upkeep keeps the notes in Claude Code and asks again on the
next upkeep. A dry run shows the question without asking you to answer.

If you share, upkeep copies eligible notes to `~/.agents/memory/` and adds the index to the other agents'
instructions. Upkeep manages only the copies it recorded in its manifest. It skips symlinked notes and asks before
overwriting an edited copy. The shared index lists only notes that are shared. If Claude Code's memory folder is
removed, upkeep backs up the instruction files and clears the index it added. If you set `mirror_memory = false`
after sharing, upkeep removes the index and the copies it made (backed up) and asks before removing a copy you edited.

The other agents send their instructions to their own companies, so some notes stay in Claude Code:

- A note that contains a secret value, such as a GitHub or API token, an AWS key or a private key, never leaves
  Claude Code. The same goes for an instruction line with a secret value: it stays in its file.
- A note that mentions credentials, tokens, keys, a legal name, an address, a phone number or bank details stays in
  Claude Code by default. Each run lists these notes and why. Add a note's name to `share_memory` to share it.
- A note with `private: true` in its front matter, or listed in `private_memory`, stays in Claude Code.
- Lines between `<!-- agent-sync: private -->` and `<!-- agent-sync: end private -->` in an instruction file stay in
  that file. Lines between `<!-- agent-sync: local -->` and `<!-- agent-sync: end local -->` do too.
- Installer blocks with matching `<!-- NAME:start ... -->` and `<!-- NAME:end -->` markers, such as Plannotator's
  Inbox block, stay in the instruction file where the installer wrote them. This includes nested blocks that use
  the same marker name.
