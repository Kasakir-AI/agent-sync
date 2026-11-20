"""Mirror a skill package's agents/*.md into Claude Code, Codex and Cursor.

- Claude Code: ~/.claude/agents/<name>.md with the configured Claude model.
- Codex: ~/.codex/agents/<name>.toml (name, description, model, model_reasoning_effort, developer_instructions).
- Cursor: a local plugin, ~/.cursor/plugins/local/<package>-agents/. Cursor reads custom agents from a project's
  .cursor/agents and from plugins, not from ~/.cursor/agents, so a small agents-only plugin makes them global.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .config import AgentModels
from .tomledit import basic_string


@dataclass
class Agent:
    name: str
    description: str
    front: list[str]        # front-matter lines without `model:`
    pins_model: bool
    body: str

    @classmethod
    def parse(cls, text: str) -> Agent:
        m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
        if not m:
            raise ValueError("no YAML front matter")
        front, body = m.group(1), m.group(2)
        name = re.search(r"^name:\s*(.+)$", front, re.M)
        desc = re.search(r"^description:\s*(.+)$", front, re.M)
        if not name or not desc:
            raise ValueError("front matter needs name and description")
        lines = [l for l in front.splitlines() if not l.startswith("model:")]
        return cls(name.group(1).strip(), desc.group(1).strip(), lines,
                   re.search(r"^model:", front, re.M) is not None, body.rstrip() + "\n")

    def markdown(self, model: str) -> str:
        front = self.front + ([f"model: {model}"] if self.pins_model else [])
        return "---\n" + "\n".join(front) + "\n---\n" + self.body

    def codex_toml(self, model: str, effort: str) -> str:
        return (f"name = {basic_string(self.name)}\ndescription = {basic_string(self.description)}\n"
                f"model = {basic_string(model)}\nmodel_reasoning_effort = {basic_string(effort)}\n"
                f"developer_instructions = {basic_string(self.body.strip())}\n")


def write_if_changed(path: Path, text: str, dry: bool = False) -> bool:
    if path.exists() and path.read_text() == text:
        return False
    if dry:
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return True


def mirror(package_dir: Path, package: str, home: Path, models: AgentModels, version: str = "0",
           dry: bool = False, notes: dict[str, str] | None = None) -> list[str]:
    """Write every agent of `package_dir/agents` for the three tools. Returns one line per changed file.
    `notes` adds your own text to the end of an agent's prompt, by agent name.
    With dry=True nothing is written and the lines say "would ..."."""
    changes: list[str] = []
    verb = "would update" if dry else "updated"
    agents = [Agent.parse(f.read_text()) for f in sorted((package_dir / "agents").glob("*.md"))]
    slug = package.split("/")[-1]
    plugin = home / ".cursor/plugins/local" / f"{slug}-agents"
    for a in agents:
        if (notes or {}).get(a.name):
            a.body = a.body.rstrip() + "\n\n" + notes[a.name].strip() + "\n"
        targets = [(home / ".claude/agents" / f"{a.name}.md", a.markdown(models.claude)),
                   (plugin / "agents" / f"{a.name}.md", a.markdown(models.cursor)),
                   (home / ".codex/agents" / f"{a.name}.toml", a.codex_toml(models.codex, models.codex_effort))]
        changes += [f"{verb} {p}" for p, text in targets if write_if_changed(p, text, dry)]
    if agents:
        manifest = {"name": f"{slug}-agents", "displayName": f"{package} agents", "version": version,
                    "description": f"The agents of {package}, mirrored by agent-sync (skills come from the skills "
                                   "folder; this plugin only adds agents).", "agents": "./agents/"}
        if write_if_changed(plugin / ".cursor-plugin/plugin.json", json.dumps(manifest, indent=2) + "\n", dry):
            changes.append(f"{verb} {plugin}/.cursor-plugin/plugin.json")
        names = {f"{a.name}.md" for a in agents}
        for old in (plugin / "agents").glob("*.md"):
            if old.name not in names:
                if not dry:
                    old.unlink()
                changes.append(f"{'would remove' if dry else 'removed'} {old} (no longer in {package})")
    return changes
