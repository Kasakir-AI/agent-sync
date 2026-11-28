"""The skill catalog: which skills fit a project, and where each one comes from.

skill_catalog.json (built by scripts/build_catalog.py from catalog/sources.toml) lists skills, the technologies
that call for them and how to spot those technologies in a project. Every skill names its own repository and the
commit it was checked at, so `agent-sync skill install` fetches it from its maker, pinned, through the `skills`
CLI.
"""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from pathlib import Path

SKIP = {".git", "node_modules", "vendor", "dist", "build", "target", ".venv", "venv", "__pycache__", ".next",
        ".nuxt", ".svelte-kit", ".turbo", ".cache", "coverage", ".tox", "Pods", "DerivedData", ".gradle"}
DEPTH = 4
MAX_FILES = 20_000


@dataclass(frozen=True)
class Skill:
    id: str
    source: str          # owner/repo
    path: str            # the skill's folder in that repository
    title: str
    summary: str
    commit: str
    name: str = ""       # the folder it installs as: the name its own SKILL.md gives

    @property
    def url(self) -> str:
        """The skill pinned to its commit, as `skills add` takes it."""
        return f"https://github.com/{self.source}/tree/{self.commit}" + (f"/{self.path}" if self.path else "")


@dataclass(frozen=True)
class Technology:
    id: str
    name: str
    skills: tuple[str, ...]
    packages: frozenset[str] = frozenset()
    package_patterns: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    extensions: frozenset[str] = frozenset()
    content_files: tuple[str, ...] = ()
    content_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class Combo:
    id: str
    name: str
    requires: frozenset[str]
    skills: tuple[str, ...]


@dataclass(frozen=True)
class Catalog:
    skills: dict[str, Skill]
    technologies: tuple[Technology, ...]
    combos: tuple[Combo, ...]


@cache
def load() -> Catalog:
    data = json.loads(resources.files("agent_sync").joinpath("skill_catalog.json").read_text())
    commits = data["sources"]
    skills = {s["id"]: Skill(s["id"], s["source"], s["path"], s["title"], s["summary"], commits[s["source"]],
                             s.get("name", s["id"]))
              for s in data["skills"]}
    technologies = []
    for tech in data["technologies"]:
        detect = tech.get("detect", {})
        content = detect.get("content", {})
        technologies.append(Technology(
            tech["id"], tech["name"], tuple(tech["skills"]),
            frozenset(name.lower() for name in detect.get("packages", [])), tuple(detect.get("package_patterns", [])),
            tuple(detect.get("files", [])), frozenset(detect.get("extensions", [])),
            tuple(content.get("files", [])), tuple(content.get("patterns", []))))
    combos = tuple(Combo(c["id"], c["name"], frozenset(c["requires"]), tuple(c["skills"])) for c in data["combos"])
    return Catalog(skills, tuple(technologies), combos)


@dataclass
class Project:
    """What a project folder shows: its files (relative paths), their extensions, and its dependencies."""
    root: Path
    files: set[str] = field(default_factory=set)
    folders: set[str] = field(default_factory=set)
    extensions: set[str] = field(default_factory=set)
    packages: set[str] = field(default_factory=set)


def scan(root: Path) -> Project:
    project = Project(root)
    stack = [(root, 0)]
    while stack and len(project.files) < MAX_FILES:
        folder, depth = stack.pop()
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if depth < DEPTH and entry.name not in SKIP:
                    project.folders.add(entry.relative_to(root).as_posix())
                    stack.append((entry, depth + 1))
                continue
            relative = entry.relative_to(root).as_posix()
            project.files.add(relative)
            if entry.suffix:
                project.extensions.add(entry.suffix.lower())
            project.packages |= dependencies(entry)
    return project


def dependencies(path: Path) -> set[str]:
    """The dependency names a manifest declares, lowercased. Unreadable or unknown files give none."""
    name = path.name
    try:
        if name == "package.json":
            data = json.loads(path.read_text())
            return {dep.lower() for key in ("dependencies", "devDependencies", "peerDependencies",
                                            "optionalDependencies") for dep in (data.get(key) or {})}
        if name == "pyproject.toml":
            data = tomllib.loads(path.read_text())
            project = data.get("project", {})
            specs = list(project.get("dependencies", []))
            specs += [spec for group in project.get("optional-dependencies", {}).values() for spec in group]
            specs += [spec for group in data.get("dependency-groups", {}).values() for spec in group
                      if isinstance(spec, str)]
            poetry = data.get("tool", {}).get("poetry", {})
            names = set(poetry.get("dependencies", {})) | set(poetry.get("dev-dependencies", {}))
            names |= {dep for group in poetry.get("group", {}).values() for dep in group.get("dependencies", {})}
            return {requirement(spec) for spec in specs} | {dep.lower() for dep in names} - {"python", ""}
        if re.fullmatch(r"requirements.*\.(txt|in)", name):
            return {requirement(line) for line in path.read_text().splitlines()
                    if line.strip() and not line.lstrip().startswith(("#", "-"))} - {""}
        if name == "Pipfile":
            data = tomllib.loads(path.read_text())
            return {dep.lower() for key in ("packages", "dev-packages") for dep in data.get(key, {})}
        if name == "Cargo.toml":
            data = tomllib.loads(path.read_text())
            return {dep.lower() for key in ("dependencies", "dev-dependencies") for dep in data.get(key, {})}
        if name == "go.mod":
            return {match.lower() for match in re.findall(r"^\s*(?:require\s+)?([\w.-]+\.[\w./-]+)\s+v",
                                                          path.read_text(), re.M)}
        if name == "composer.json":
            data = json.loads(path.read_text())
            return {dep.lower() for key in ("require", "require-dev") for dep in (data.get(key) or {})}
        if name == "Gemfile":
            return {gem.lower() for gem in re.findall(r"""^\s*gem\s+["']([^"']+)""", path.read_text(), re.M)}
    except (OSError, ValueError, AttributeError, TypeError):
        return set()
    return set()


def requirement(spec: str) -> str:
    """The package name in a Python requirement such as `fastapi[all]>=0.110 ; python_version>'3.9'`."""
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
    return match[1].lower().replace("_", "-") if match else ""


def detect(catalog: Catalog, project: Project) -> dict[str, str]:
    """technology id -> the evidence that the project uses it."""
    found = {}
    for tech in catalog.technologies:
        evidence = evidence_for(tech, project)
        if evidence:
            found[tech.id] = evidence
    return found


def evidence_for(tech: Technology, project: Project) -> str | None:
    packages = sorted(tech.packages & project.packages)
    if packages:
        return f"depends on {packages[0]}"
    for pattern in tech.package_patterns:
        try:
            match = next((name for name in sorted(project.packages) if re.search(pattern, name)), None)
        except re.error:
            continue
        if match:
            return f"depends on {match}"
    for name in tech.files:
        name = name.rstrip("/")               # a marker can be a folder, such as .github/workflows/
        hit = next((f for f in sorted(project.files | project.folders) if f == name or f.endswith(f"/{name}")), None)
        if hit:
            return f"has {hit}"
    for name in tech.content_files:
        for relative in sorted(f for f in project.files if f == name or f.endswith(f"/{name}")):
            try:
                text = (project.root / relative).read_text(errors="replace")[:200_000]
            except OSError:
                continue
            for pattern in tech.content_patterns:
                try:
                    if re.search(pattern, text):
                        return f"{relative} mentions {tech.name}"
                except re.error:
                    break
    extensions = sorted(tech.extensions & project.extensions)
    if extensions:
        return f"has {extensions[0]} files"
    return None


@dataclass(frozen=True)
class Suggestion:
    skill: Skill
    reasons: tuple[str, ...]


def suggest(catalog: Catalog, found: dict[str, str]) -> list[Suggestion]:
    reasons: dict[str, list[str]] = {}
    names = {tech.id: tech.name for tech in catalog.technologies}
    for tech in catalog.technologies:
        if tech.id in found:
            for skill in tech.skills:
                reasons.setdefault(skill, []).append(f"{tech.name} ({found[tech.id]})")
    for combo in catalog.combos:
        if combo.requires <= set(found):
            for skill in combo.skills:
                reasons.setdefault(skill, []).append(" + ".join(names.get(t, t) for t in sorted(combo.requires)))
    return [Suggestion(catalog.skills[skill], tuple(why)) for skill, why in reasons.items() if skill in catalog.skills]


def skill_roots(root: Path | None, home: Path, agents) -> set[Path]:
    """The skill folders the agents read: the project's (root) or, with root None, your account's."""
    if root is None:
        from .skills import folders
        return set(folders(home, agents).values())
    return {root / (".claude/skills" if tool == "claude" else ".agents/skills") for tool in agents}


def installed(root: Path | None, home: Path, agents) -> set[str]:
    """Skill folder names (lowercased) that every agent can load: a SKILL.md in each of their skill folders."""
    found = None
    for folder in skill_roots(root, home, agents):
        names = {entry.name.lower() for entry in folder.iterdir() if (entry / "SKILL.md").is_file()} \
            if folder.is_dir() else set()
        found = names if found is None else found & names
    return found or set()


def search(catalog: Catalog, words: str) -> list[Skill]:
    terms = words.lower().split()
    return [skill for skill in catalog.skills.values()
            if all(term in f"{skill.id} {skill.title} {skill.summary} {skill.source}".lower() for term in terms)]
