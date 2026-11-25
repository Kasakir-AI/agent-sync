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

from . import backups
from .config import AgentModels
from .registry import DEFAULT_AGENTS, TOOLS, grouped_paths
from .tomledit import basic_string


def description(front: str) -> tuple[str, str]:
    """The description in YAML front matter: (its text, the value to write after `description:`). A block value
    (`description: >` folds lines, `|` keeps them) becomes one quoted string; an inline one is kept as written."""
    found = re.search(r"^description:[ \t]*(.*)$", front, re.M)
    if not found:
        return "", ""
    head = found.group(1).strip()
    if not re.fullmatch(r"[>|][+-]?", head):
        return head, head
    block = []
    for line in front[found.end():].splitlines()[1:]:
        if line.strip() and not line[:1].isspace():
            break
        block.append(line.strip())
    text = "\n".join(block)
    text = (text if head[0] == "|" else re.sub(r"[ \t]*\n(?!\n)[ \t]*", " ", text)).strip()
    return text, json.dumps(text, ensure_ascii=False)       # JSON's quoted string is a YAML quoted string


@dataclass
class Agent:
    name: str
    description: str
    description_yaml: str   # the description as written after `description:` in front matter
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
        desc, desc_yaml = description(front)
        if not name or not desc:
            raise ValueError("front matter needs name and description")
        value = name.group(1).strip()
        if not value or value in (".", "..") or "/" in value or "\\" in value:
            raise ValueError(f"invalid agent name: {value!r}")
        lines = [l for l in front.splitlines() if not l.startswith("model:")]
        return cls(value, desc, desc_yaml, lines,
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
    backups.save(path, follow=True)
    path.write_text(text)
    return True


def gemini_markdown(agent: Agent, models: AgentModels) -> str:
    front = [f"name: {agent.name}", f"description: {agent.description_yaml}"]
    if models.gemini:
        front.append(f"model: {models.gemini}")
    return "---\n" + "\n".join(front) + "\n---\n" + agent.body


def antigravity_markdown(agent: Agent, models: AgentModels) -> str:
    return (f"---\nname: {agent.name}\ndescription: {agent.description_yaml}\nsubagent: true\nmainAgent: false\n"
            "tools: [read_file, edit_file, write_file, run_command, manage_task, search_web]\n---\n" + agent.body)


FORMATS = {
    "claude-md": (".md", lambda a, models: a.markdown(models.claude)),
    "codex-toml": (".toml", lambda a, models: a.codex_toml(models.codex, models.codex_effort)),
    "cursor-plugin": (".md", lambda a, models: a.markdown(models.cursor)),
    "gemini-md": (".md", gemini_markdown),
    "antigravity-md": (".md", antigravity_markdown),
}


def mirror(package_dir: Path, package: str, home: Path, models: AgentModels, version: str = "0",
           dry: bool = False, notes: dict[str, str] | None = None, *, enabled=DEFAULT_AGENTS) -> list[str]:
    """Write every agent of `package_dir/agents` for the enabled tools. Returns one line per changed file.
    `notes` adds your own text to the end of an agent's prompt, by agent name.
    With dry=True nothing is written and the lines say "would ..."."""
    changes: list[str] = []
    verb = "would update" if dry else "updated"
    agents = [Agent.parse(f.read_text()) for f in sorted((package_dir / "agents").glob("*.md"))]
    slug = package.split("/")[-1]
    targets = grouped_paths({tool.id: home / tool.agents_dir.format(pkg=slug)
                             for tool in TOOLS.values() if tool.id in enabled and tool.agents_dir})
    for a in agents:
        if (notes or {}).get(a.name):
            a.body = a.body.rstrip() + "\n\n" + notes[a.name].strip() + "\n"
        for folder, readers in targets.items():
            fmt = TOOLS[readers[0]].agent_format
            if fmt == "gemini-md" and not re.fullmatch(r"[a-z0-9_-]+", a.name):
                changes.append(f"WARN  {a.name!r} is not a valid Gemini CLI agent name; skipped")
                continue
            suffix, render = FORMATS[fmt]
            path = folder / (a.name + suffix)
            if write_if_changed(path, render(a, models), dry):
                changes.append(f"{verb} {path}")
    for folder, readers in targets.items():
        if not agents or TOOLS[readers[0]].agent_format != "cursor-plugin":
            continue
        plugin = folder.parent
        manifest = {"name": f"{slug}-agents", "displayName": f"{package} agents", "version": version,
                    "description": f"The agents of {package}, mirrored by agent-sync (skills come from the skills "
                                   "folder; this plugin only adds agents).", "agents": "./agents/"}
        if write_if_changed(plugin / ".cursor-plugin/plugin.json", json.dumps(manifest, indent=2) + "\n", dry):
            changes.append(f"{verb} {plugin}/.cursor-plugin/plugin.json")
        names = {f"{a.name}.md" for a in agents}
        for old in folder.glob("*.md"):
            if old.name not in names:
                if not dry:
                    backups.save(old)
                    old.unlink()
                changes.append(f"{'would remove' if dry else 'removed'} {old} (no longer in {package})")
    return changes
