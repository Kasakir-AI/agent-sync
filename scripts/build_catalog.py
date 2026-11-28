#!/usr/bin/env python3
"""Build agent_sync/skill_catalog.json from catalog/sources.toml.

Each source repository is pinned to the commit its default branch points at now, and each skill is looked up in
that commit's file tree: the folder holding its SKILL.md. A skill that can't be found is left out and reported, so
`agent-sync skill install` never offers one that would fail. Needs `gh` signed in (for the GitHub API).

    scripts/build_catalog.py            # rebuild at the commits already pinned (new sources: their newest)
    scripts/build_catalog.py --update   # move every pin to its source's newest commit, and list what moved

catalog/excluded.toml lists skills left out after a review, each with its reason. A skill whose source moves to a
new commit is worth reading again before it stays in.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "catalog/sources.toml"
EXCLUDED = ROOT / "catalog/excluded.toml"     # skills left out after review, with the reason
OUTPUT = ROOT / "agent_sync/skill_catalog.json"
HOME = "Kasakir-AI/agent-sync-skills"


def gh(path: str) -> dict:
    result = subprocess.run(["gh", "api", path], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if result.returncode:
        raise LookupError(result.stderr.strip() or f"gh api {path} failed")
    return json.loads(result.stdout)


def front_matter(repo: str, commit: str, path: str) -> dict[str, str]:
    """`name` and `description` from a skill's SKILL.md at the commit."""
    import base64
    import re

    where = f"{path}/SKILL.md" if path else "SKILL.md"
    try:
        text = base64.b64decode(gh(f"repos/{repo}/contents/{where}?ref={commit}")["content"]).decode()
    except (LookupError, KeyError, ValueError):
        return {}
    head = re.match(r"^\ufeff?---\n(.*?)\n---", text, re.S)
    fields = {"_text": text, "_head": head[1] if head else ""}
    for key in ("name", "description"):
        match = head and re.search(rf'^{key}:\s*["\']?(.+?)["\']?\s*$', head[1], re.M)
        if match and match[1] not in (">", "|", ">-", "|-"):
            fields[key] = match[1]
    return fields


def risky(fields: dict[str, str]) -> list[str]:
    """Things in a SKILL.md that change what an agent may do without asking: broad pre-approved shell access in
    `allowed-tools`, and commands it runs when it loads (`!` followed by a backquoted command)."""
    found = []
    tools = re.search(r"(?m)^allowed-tools:(.*(?:\n[ \t-].*)*)", fields.get("_head", ""))
    if tools and re.search(r"Bash\((\*|npx|npm|curl|sh|bash|python|node|uvx|pip)[:)*]", tools[1]):
        found.append("pre-approves broad shell commands in allowed-tools")
    if re.search(r"(?<![\w`])!`[^`]+`", fields.get("_text", "")):
        found.append("runs a command when the skill loads (!`...`)")
    return found


def skill_folders(repo: str, commit: str) -> list[str]:
    """Every folder in the commit that holds a SKILL.md."""
    tree = gh(f"repos/{repo}/git/trees/{commit}?recursive=1")
    if tree.get("truncated"):
        raise LookupError(f"{repo} is too large to list in one call; give its skills a `path`")
    return [item["path"].rpartition("/")[0] for item in tree["tree"]
            if item["type"] == "blob" and (item["path"] == "SKILL.md" or item["path"].endswith("/SKILL.md"))]


def find(folders: list[str], skill: dict) -> str | None:
    """The skill's folder: its `path` when that still holds a skill, else the folder named after it (the shortest
    such path), as repositories move their skills around."""
    if skill.get("path") in folders:
        return skill["path"]
    name = skill.get("name", skill["id"])
    owner, _, repo = skill["source"].partition("/")
    # a repository often drops its own name from its skills' ids: expo/skills ships building-native-ui, not
    # expo-building-native-ui
    candidates = [name, *(name.removeprefix(f"{word}-") for word in {owner, repo, repo.split("-")[0]}
                          if name.startswith(f"{word}-"))]
    for candidate in candidates:
        named = sorted((f for f in folders if f.rpartition("/")[2] == candidate), key=lambda f: (len(f), f))
        if named:
            return named[0]
    return folders[0] if len(folders) == 1 else None


def main() -> int:
    keep = "--update" not in sys.argv
    sources = tomllib.loads(SOURCES.read_text())
    excluded = tomllib.loads(EXCLUDED.read_text()).get("excluded", {}) if EXCLUDED.exists() else {}
    sources["skills"] = [skill for skill in sources["skills"] if skill["id"] not in excluded]
    previous_pins = json.loads(OUTPUT.read_text()).get("sources", {}) if OUTPUT.exists() else {}
    previous = previous_pins if keep else {}
    commits, folders, problems = {}, {}, []
    for repo in sorted({skill["source"] for skill in sources["skills"]}):
        try:
            commits[repo] = previous.get(repo) or gh(f"repos/{repo}/commits/HEAD")["sha"]
            folders[repo] = skill_folders(repo, commits[repo])
        except LookupError as error:
            if "HTTP 404" not in str(error):      # rate limits, sign-in, network: don't mistake them for gone
                print(f"stopped, nothing written: {repo}: {error}")
                return 1
            problems.append(f"{repo}: {error}")
    skills = []
    for skill in sources["skills"]:
        repo = skill["source"]
        path = find(folders.get(repo, []), skill) if repo in folders else None
        if path is None:
            if repo in folders:
                problems.append(f"{skill['id']}: no skill folder in {repo} at {commits[repo][:12]}")
            continue
        fields = front_matter(repo, commits[repo], path)
        if re.search(r"(?m)^disable-model-invocation:\s*true", fields.get("_head", "")):
            problems.append(f"{skill['id']}: turns model invocation off, so an agent never loads it once installed")
            continue
        for concern in risky(fields):
            print(f"read before release: {skill['id']} ({repo}): {concern}")
        summary = skill["summary"]
        if repo == HOME and fields.get("description"):    # our own: the summary their SKILL.md gives
            summary = fields["description"].split(". Use when")[0].rstrip(".") + "."
        # the `skills` CLI names the installed folder after the skill's own `name`, not the catalog id
        name = fields.get("name") or path.rpartition("/")[2] or skill["id"]
        skills.append({"id": skill["id"], "name": name, "source": repo, "path": path, "title": skill["title"],
                       "summary": summary})
    found = {skill["id"] for skill in skills}
    technologies = [{**tech, "skills": [s for s in tech["skills"] if s in found]} for tech in sources["technologies"]]
    combos = [{**combo, "skills": [s for s in combo["skills"] if s in found]} for combo in sources.get("combos", [])]
    combos = [combo for combo in combos if combo["skills"]]
    needed = {tech for combo in combos for tech in combo["requires"]}
    kept = {tech["id"] for tech in technologies if tech["skills"] or tech["id"] in needed}
    catalog = {
        "version": 1,
        "built": date.today().isoformat(),
        "sources": {repo: commits[repo] for repo in sorted({skill["source"] for skill in skills})},
        "skills": skills,
        "technologies": [tech for tech in technologies if tech["id"] in kept],
        "combos": [combo for combo in combos if combo["skills"] and set(combo["requires"]) <= kept],
    }
    OUTPUT.write_text(json.dumps(catalog, indent=1, ensure_ascii=False) + "\n")
    for problem in problems:
        print(f"left out: {problem}")
    moved = sorted(repo for repo in catalog["sources"]
                   if repo in previous_pins and previous_pins[repo] != commits[repo])
    if moved:
        print("pins moved; read these skills again before release: " + ", ".join(moved))
    print(f"{len(skills)} skills from {len(catalog['sources'])} repositories, "
          f"{len(catalog['technologies'])} technologies, {len(catalog['combos'])} combinations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
