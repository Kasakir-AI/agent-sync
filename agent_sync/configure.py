"""`agent-sync config`: change the settings by answering questions in the terminal.

Covers every setting in config.toml, plus the pstack model roles (in the source tool's pstack-models file) and the
global instructions (opened in your editor). Changes are kept until you save; saving keeps your comments in
config.toml and leaves the old file next to it as config.toml.bak.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from . import instructions, roles, skills, tomledit
from .config import AgentModels, config_path, parse
from .registry import TOOLS

PARTS = [  # (setting, what it does) for the on/off switches
    ("mirror_skills", "Copy your own skills (not from a package) from the source tool to the others"),
    ("mirror_roles", "Copy pstack's model roles from the source tool to the others"),
    ("mirror_mcp", "Add the source tool's MCP servers to the other tools (never deletes any)"),
    ("mirror_instructions", "Copy the global instructions (CLAUDE.md / AGENTS.md) to the other tools"),
    ("update_plugins", "Update plugin marketplaces and plugins in all three tools"),
    ("remove_dropped_skills", "Uninstall skills a skill package stops shipping"),
    ("update_self", "Update agent-sync itself when a new version is out"),
    ("launcher", "Keep the double-click \"Agent Upkeep\" launcher on the Desktop (macOS)"),
]
INSTALL_ITEMS = ("homebrew", "git", "node", *TOOLS)


class Quit(Exception):
    pass


def open_in_editor(path: Path, say: Callable[[str], None] = print) -> None:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or next(
        (e for e in ("nano", "vi") if shutil.which(e)), None)
    if not editor:
        say(f"  No text editor found. Set EDITOR, or open {path} yourself.")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([*shlex.split(editor), str(path)], check=False)


class Editor:
    def __init__(self, home: Path, path: Path | None = None, ask: Callable[[str], str] = input,
                 say: Callable[[str], None] = print, dry_run: Callable[[], object] | None = None):
        self.home, self.path = home, path or config_path(home)
        self.ask_line, self.say, self.dry_run = ask, say, dry_run
        self.text = self.saved = self.path.read_text() if self.path.exists() else ""
        self.cfg = parse(self.text, self.path, home)     # a ValueError here: run() offers the editor instead
        self.did_save = False
        self.roles_path = roles.files(home, self.cfg.agents)[self.cfg.source]
        self.roles_text = self.roles_saved = self.roles_path.read_text() if self.roles_path.exists() else None
        color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        self.bold = (lambda s: f"\033[1m{s}\033[0m") if color else (lambda s: s)
        self.dim = (lambda s: f"\033[2m{s}\033[0m") if color else (lambda s: s)

    def short(self, path: Path) -> str:
        return "~/" + str(path.relative_to(self.home)) if path.is_relative_to(self.home) else str(path)

    # ---------- asking ----------

    def ask(self, prompt: str) -> str:
        try:
            return self.ask_line(prompt).strip()
        except EOFError:
            raise Quit from None

    def yes(self, question: str, default: bool = False) -> bool:
        answer = self.ask(f"{question} [{'Y/n' if default else 'y/N'}] ").lower()
        return default if not answer else answer.startswith("y")

    def explain(self, title: str, *lines: str) -> None:
        self.say("")
        self.say(self.bold(title))
        for line in lines:
            self.say("  " + line)

    def choose(self, options: list[tuple[str, str]], prompt: str = "Choose") -> str | None:
        """Numbered options; returns the chosen key, or None for Enter (back)."""
        for n, (_, label) in enumerate(options, 1):
            self.say(f"  {n:>2}  {label}")
        while True:
            answer = self.ask(f"{prompt} (number, Enter to go back): ")
            if not answer:
                return None
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return options[int(answer) - 1][0]
            self.say(f"  Please type a number from 1 to {len(options)}, or just press Enter.")

    # ---------- changing settings ----------

    def change(self, new_text: str) -> bool:
        """Use `new_text` if it is valid settings; otherwise say why and keep the old text."""
        try:
            self.cfg = parse(new_text, self.path, self.home)
        except ValueError as e:
            self.say(f"  That didn't work, nothing changed: {e}")
            return False
        self.text = new_text
        self.refresh_roles()
        self.say("  " + self.dim("Changed. (Not saved yet: choose s to save.)"))
        return True

    def refresh_roles(self) -> None:
        """Point the roles file at the current source tool, unless there are role edits not saved yet."""
        if self.roles_text == self.roles_saved:
            self.roles_path = roles.files(self.home, self.cfg.agents)[self.cfg.source]
            self.roles_text = self.roles_saved = self.roles_path.read_text() if self.roles_path.exists() else None

    def set(self, key: str, value, table: str | None = None) -> bool:
        return self.change(tomledit.set_value(self.text, table, key, value))

    def edit_list(self, title: str, items: list[str], add_prompt: str, examples: list[str],
                  check: Callable[[str], str | None] | None = None,
                  suggestions: list[str] | None = None) -> list[str] | None:
        """Add or remove items. Returns the new list, or None when nothing changed."""
        items = list(items)
        before = list(items)
        while True:
            self.say("")
            self.say(f"  {title}: " + (", ".join(items) if items else "(none)"))
            options = [("add", "Add one")] + ([("remove", "Remove one"), ("clear", "Remove all")] if items else [])
            what = self.choose(options, "What next")
            if what is None:
                return items if items != before else None
            if what == "add":
                if suggestions:
                    left = [s for s in suggestions if s not in items]
                    if left:
                        self.say("  Found here: " + ", ".join(left))
                self.say("  Examples: " + ", ".join(examples))
                value = self.ask(f"{add_prompt}: ")
                problem = check(value) if check and value else None
                if problem:
                    self.say(f"  {problem}")
                elif value and value not in items:
                    items.append(value)
            elif what == "remove":
                gone = self.choose([(i, i) for i in items], "Remove which")
                if gone:
                    items.remove(gone)
            else:
                items.clear()

    def ask_command(self, prompt: str, example: str) -> list[str] | None:
        self.say(f"  Type the command as you would in a terminal. Example: {example}")
        line = self.ask(f"{prompt}: ")
        if not line:
            return None
        try:
            command = shlex.split(line)
        except ValueError as e:
            self.say(f"  Couldn't read that command ({e}); nothing changed.")
            return None
        # The command runs without a shell, so nothing would turn ~ into your home folder. Do it here, once,
        # and show the real path below, so what is saved is what will run.
        command = [str(self.home) + c[1:] if c == "~" or c.startswith("~/") else c for c in command]
        self.say(f"  It will run: {' '.join(shlex.quote(c) for c in command)}")
        return command

    def edit_commands(self, table: str, title: str, names: tuple[str, ...], example: dict[str, str]) -> None:
        current = dict(getattr(self.cfg, table))
        while True:
            self.say("")
            for name in names:
                cmd = current.get(name)
                self.say(f"  {name:<9} {' '.join(shlex.quote(c) for c in cmd) if cmd else self.dim('built-in')}")
            what = self.choose([(n, f"{n}: set your own command" + (" / go back to built-in" if n in current else ""))
                                for n in names], title)
            if what is None:
                return
            if what in current and self.yes(f"Go back to the built-in command for {what}?"):
                del current[what]
            else:
                command = self.ask_command(f"Command for {what}", example.get(what, example["*"]))
                if not command:
                    continue
                current[what] = command
            self.change(tomledit.set_table(self.text, table, current))

    # ---------- the settings ----------

    def source(self) -> None:
        self.explain("Source tool", "The tool you set things up in. The other two copy its skills, roles, MCP",
                     "servers and (if you switch it on) its global instructions.",
                     f"Now: {TOOLS[self.cfg.source].name}")
        pick = self.choose([(t, TOOLS[t].name) for t in self.cfg.agents])
        if pick and pick != self.cfg.source:
            if pick == "cursor":
                self.say("  Note: Cursor can't be the source of global instructions (that part will be skipped).")
            self.set("source", pick)

    def packages(self) -> None:
        self.explain("Skill packages", "GitHub repositories (owner/repo) whose skills are installed in all three",
                     "tools. A package with an agents/ folder also gets its agents copied to each tool.",
                     "Only add packages you trust: their skills run with your agents' permissions.")
        new = self.edit_list("Packages", self.cfg.skill_packages, "Package (owner/repo)",
                             ["backnotprop/pstack", "your-name/your-skills"],
                             lambda v: None if re.fullmatch(r"[\w.-]+/[\w.-]+", v) else
                             "That should look like owner/repo, for example backnotprop/pstack.")
        if new is not None:
            self.set("skill_packages", new)

    def parts(self) -> None:
        self.explain("What to keep in step", "Switch the parts of the daily upkeep on or off.")
        while True:
            options = [(k, f"[{'x' if getattr(self.cfg, k) else ' '}] {label}") for k, label in PARTS]
            pick = self.choose(options, "Switch which on/off")
            if pick is None:
                return
            if pick == "launcher" and not self.cfg.launcher:
                self.say("  The next upkeep puts it on the Desktop (or run: agent-sync launcher).")
            if pick == "mirror_instructions" and not self.cfg.mirror_instructions:
                self.say("  This writes a marked block into the other tools' instruction files; their own text stays.")
            self.set(pick, not getattr(self.cfg, pick))

    def skip(self) -> None:
        self.explain("Skills that stay in the source tool",
                     "Your own skills are copied to the other tools. Name the ones that should not be.")
        mine = sorted(skills.own(self.home, self.cfg.source, []))
        new = self.edit_list("Kept in the source only", self.cfg.skip_skills, "Skill name",
                             ["only-for-claude", "import-to-claude-code"], suggestions=mine)
        if new is not None:
            self.set("skip_skills", new)

    def models(self) -> None:
        m = self.cfg.agent_models
        self.explain("Agent models", "The model each tool's copy of a package agent (like poteto-agent) runs on.",
                     "Codex can only run its own (OpenAI) models.")
        fields = [("claude", "Claude Code", "opus, sonnet, haiku, fable"),
                  ("cursor", "Cursor", "claude-opus-5-5-high-fast, gpt-6.2-sol"),
                  ("codex", "Codex", "gpt-6.2-sol"),
                  ("codex_effort", "Codex reasoning effort", "low, medium, high, xhigh"),
                  ("gemini", TOOLS["gemini"].name, "inherit")]
        while True:
            pick = self.choose([(k, f"{label:<24} {getattr(m, k)}") for k, label, _ in fields], "Change which")
            if pick is None:
                return
            label, examples = next((lab, ex) for k, lab, ex in fields if k == pick)
            self.say(f"  Examples: {examples}. Default: {getattr(AgentModels(), pick)}")
            value = self.ask(f"{label} (Enter keeps {getattr(m, pick)}): ")
            if value:
                self.set(pick, "" if pick == "gemini" and value == "inherit" else value, "agent_models")
                m = self.cfg.agent_models

    def notes(self) -> None:
        self.explain("Notes on agents", "Your own text added to the end of an agent's prompt, in every tool's copy.",
                     'Example: poteto-agent = "On this machine pstack lives in ~/.agents/skills."')
        current = dict(self.cfg.agent_notes)
        while True:
            for name, note in current.items():
                self.say(f"  {name}: {note}")
            if not current:
                self.say("  (no notes yet)")
            options = [("add", "Add or change a note")] + [(n, f"Remove the note on {n}") for n in current]
            pick = self.choose(options, "What next")
            if pick is None:
                return
            if pick == "add":
                name = self.ask("Agent name (example: poteto-agent): ")
                text = self.ask("The note: ") if name else ""
                if not (name and text):
                    continue
                current[name] = text
            else:
                del current[pick]
            self.change(tomledit.set_table(self.text, "agent_notes", current))

    def warnings(self) -> None:
        self.explain("Model warnings", "Warn in the log when a model name containing one of these shows up in a",
                     "tool's settings, for models you don't have or never want.")
        new = self.edit_list("Warn about", self.cfg.warn_models, "Part of a model name",
                             ["gemini-", "preview-", "-mini"])
        if new is not None:
            self.set("warn_models", new)

    def duplicates(self) -> None:
        self.explain("Skills a tool may load twice",
                     "The upkeep warns when a tool loads the same skill name from two places (a plugin and a",
                     "package, say). Name the ones you keep twice on purpose.")
        new = self.edit_list("Allowed twice", self.cfg.allow_duplicate_skills, "Skill name", ["pdf", "skill-creator"])
        if new is not None:
            self.set("allow_duplicate_skills", new)

    def cli_updates(self) -> None:
        now = self.cfg.update_clis
        self.explain("CLI updates", "Update Claude Code, Codex and Cursor Agent during the upkeep.",
                     "Now: " + ("all three" if now is True else "none" if now is False else ", ".join(now)))
        pick = self.choose([("all", "Update all three"), ("none", "Update none"), ("some", "Choose which")])
        if pick == "all":
            self.set("update_clis", True)
        elif pick == "none":
            self.set("update_clis", False)
        elif pick == "some":
            chosen = [t for t in self.cfg.agents if self.cfg.wants_cli(t)]
            while True:
                t = self.choose([(t, f"[{'x' if t in chosen else ' '}] {TOOLS[t].name}") for t in self.cfg.agents],
                                "Switch which on/off")
                if t is None:
                    break
                chosen = [c for c in self.cfg.agents if (c in chosen) != (c == t)]
            self.set("update_clis", chosen if len(chosen) < len(self.cfg.agents) else True)

    def update_commands(self) -> None:
        self.explain("Your own update commands", "Replace how agent-sync updates a CLI (normally Homebrew or the",
                     "tool's own updater).")
        self.edit_commands("cli_commands", "Change which", tuple(TOOLS),
                           {"codex": "npm install -g @openai/codex@latest", "claude": "claude update",
                            "*": "npm install -g some-package@latest"})

    def install_commands(self) -> None:
        self.explain("Your own install commands", "Replace how `agent-sync setup` installs something that is missing.")
        self.edit_commands("install_commands", "Change which", INSTALL_ITEMS,
                           {"codex": "npm install -g @openai/codex", "*": "brew install the-thing"})

    def steps(self) -> None:
        self.explain("Extra steps", "Your own commands, run in order at the end of every upkeep. A dry run only",
                     "prints them.")
        current = list(self.cfg.extra_steps)
        while True:
            for n, s in enumerate(current, 1):
                self.say(f"  {n}. {s.get('name', '?')}: {' '.join(shlex.quote(c) for c in s['command'])}")
            if not current:
                self.say("  (no extra steps yet)")
            options = [("add", "Add a step")] + [(str(i), f"Remove step {i + 1} ({s.get('name', '?')})")
                                                 for i, s in enumerate(current)]
            pick = self.choose(options, "What next")
            if pick is None:
                return
            if pick == "add":
                name = self.ask("A short name (example: refresh my dotfiles): ")
                command = self.ask_command("Command", "git -C ~/dotfiles pull --ff-only") if name else None
                if not command:
                    continue
                step = {"name": name, "command": command}
                timeout = self.ask("Stop it after how many seconds? (Enter for 900, which is 15 minutes): ")
                if timeout.isdigit():
                    step["timeout"] = int(timeout)
                current.append(step)
            else:
                current.pop(int(pick))
            self.change(tomledit.set_array_tables(self.text, "extra_steps", current))

    def advanced(self) -> None:
        self.explain("Advanced")
        pick = self.choose([("skills_cli", f"skills CLI version      {self.cfg.skills_cli}"),
                            ("log_days", f"Days of log to keep     {self.cfg.log_days}")])
        if pick == "skills_cli":
            self.say("  skills@latest always uses the newest release; pin one to hold it (example: skills@1.2.3).")
            value = self.ask("skills CLI (Enter keeps it): ")
            if value:
                self.set("skills_cli", value)
        elif pick == "log_days":
            value = self.ask("Days (example: 14): ")
            if value.isdigit() and int(value) > 0:
                self.set("log_days", int(value))
            elif value:
                self.say("  Please type a whole number, like 7.")

    def model_roles(self) -> None:
        path = self.roles_path
        self.explain("pstack model roles", f"Which model each pstack role uses, in {self.short(path)}.",
                     "The other tools get the same role lines at the next upkeep.")
        if self.roles_text is None:
            self.say("  There is no roles file yet. Run /setup-pstack in the source tool to make one.")
            return
        while True:
            _, lines, _ = roles.split(self.roles_text)
            if not lines:
                self.say("  No role lines found in that file.")
                return
            pick = self.choose([(line, line) for line in lines], "Change which role")
            if pick is None:
                return
            role, _, old = pick.partition(":")
            self.say("  Examples: opus | gpt-6.2-sol | inherit-parent | several, comma-separated: opus, gpt-6.2-sol")
            value = self.ask(f"Model(s) for {role.strip()} (Enter keeps {old.strip()}): ")
            if value:
                lines = self.roles_text.split("\n")
                lines[lines.index(pick)] = f"{role}: {value}"
                self.roles_text = "\n".join(lines)
                self.say("  " + self.dim("Changed. (Not saved yet: choose s to save.)"))

    def instructions_file(self) -> None:
        path = instructions.files(self.home, self.cfg.agents)[self.cfg.source]
        state = "on" if self.cfg.mirror_instructions else "off (switch it on under 'What to keep in step')"
        self.explain("Global instructions", f"{self.short(path)} opens in your editor.",
                     f"Copying it to the other tools is {state}.")
        if self.yes("Open it now?", True):
            open_in_editor(path, self.say)

    def settings_file(self) -> None:
        if self.unsaved() and self.yes("Save your changes first?", True):
            self.save()
        open_in_editor(self.path, self.say)
        self.text = self.saved = self.path.read_text() if self.path.exists() else ""
        try:
            self.cfg = parse(self.text, self.path, self.home)
            self.refresh_roles()                        # the source tool may have been changed in the editor
        except ValueError as e:
            self.say(f"  The file has a problem now: {e}")
            self.say("  Fix it in the editor (choose e again) before agent-sync runs.")

    # ---------- saving ----------

    def unsaved(self) -> bool:
        return self.text != self.saved or self.roles_text != self.roles_saved

    def save(self) -> None:
        for path, text, old in ((self.path, self.text, self.saved), (self.roles_path, self.roles_text,
                                                                     self.roles_saved)):
            if text is None or text == old:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                shutil.copy2(path, path.with_name(path.name + ".bak"))
            tmp = path.with_name(path.name + ".agent-sync-new")
            tmp.write_text(text if text.endswith("\n") or not text else text + "\n")
            tmp.replace(path)
            self.say(f"  Saved {self.short(path)}" + (f" (the old one is {path.name}.bak)" if old else ""))
        self.saved, self.roles_saved, self.did_save = self.text, self.roles_text, True

    # ---------- the main menu ----------

    def summary(self) -> list[tuple[str, str, str, Callable[[], None]]]:
        c = self.cfg
        on = [label.split(" (")[0].lower() for k, label in PARTS if getattr(c, k)]
        clis = "all three" if c.update_clis is True else "none" if c.update_clis is False else ", ".join(c.update_clis)
        m = c.agent_models
        def few(items):
            return ", ".join(items) if items else "none"
        return [
            ("1", "Source tool", TOOLS[c.source].name, self.source),
            ("2", "Skill packages", few(c.skill_packages), self.packages),
            ("3", "What to keep in step", f"{len(on)} of {len(PARTS)} parts on", self.parts),
            ("4", "Skills kept in the source", few(c.skip_skills), self.skip),
            ("5", "Agent models", f"{m.claude} / {m.cursor} / {m.codex} ({m.codex_effort})", self.models),
            ("6", "Notes on agents", few(list(c.agent_notes)), self.notes),
            ("7", "Model warnings", few(c.warn_models), self.warnings),
            ("8", "CLI updates", clis, self.cli_updates),
            ("9", "Your own update commands", few(list(c.cli_commands)), self.update_commands),
            ("10", "Your own install commands", few(list(c.install_commands)), self.install_commands),
            ("11", "Extra steps", few([s.get("name", "?") for s in c.extra_steps]), self.steps),
            ("12", "Advanced", "skills CLI version, days of log", self.advanced),
            ("13", "Skills allowed twice", few(c.allow_duplicate_skills), self.duplicates),
            ("r", "pstack model roles", self.roles_path.name if self.roles_text is not None else "none yet",
             self.model_roles),
            ("i", "Global instructions", "opens your editor", self.instructions_file),
            ("e", "The settings file itself", "opens your editor", self.settings_file),
        ]

    def menu(self) -> None:
        while True:
            items = self.summary()
            self.say("")
            self.say(self.bold("agent-sync settings") + "  " + self.dim(self.short(self.path)))
            for key, label, value, _ in items:
                if key == "r":
                    self.say("")
                self.say(f"  {key:>2}  {label} {'.' * (31 - len(label))} {value}")
            self.say("")
            self.say("   s  Save" + ("  " + self.bold("(you have unsaved changes)") if self.unsaved() else "")
                     + "      q  Quit")
            pick = self.ask("Choose: ").lower()
            if pick in ("q", "quit", "exit"):
                if self.unsaved() and self.yes("Save your changes before you go?", True):
                    self.save()
                elif self.unsaved():
                    self.say("  Nothing was saved.")
                break
            if pick in ("s", "save"):
                if self.unsaved():
                    self.save()
                else:
                    self.say("  Nothing to save.")
                continue
            action = next((fn for key, _, _, fn in items if key == pick), None)
            if action:
                action()
            elif pick:
                self.say("  Type one of the numbers or letters on the left.")
        if self.dry_run and self.did_save and self.yes("See what the next upkeep would change (a dry run)?"):
            self.dry_run()


def run(home: Path, dry_run: Callable[[], object] | None = None) -> int:
    path = config_path(home)
    if not sys.stdin.isatty():
        print(f"agent-sync config asks questions, so it needs a terminal. Or edit {path} yourself "
              "(see config.example.toml).")
        return 2
    try:
        editor = Editor(home, dry_run=dry_run)
    except ValueError as e:
        print(f"The settings file has a problem: {e}")
        if input(f"Open {path} in your editor to fix it? [Y/n] ").strip().lower() in ("", "y", "yes"):
            open_in_editor(path)
        return 1
    try:
        editor.menu()
    except (KeyboardInterrupt, Quit):
        print("\n  Stopped." + (" Nothing was saved." if editor.unsaved() else ""))
        return 130
    return 0
