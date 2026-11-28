import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts/build_catalog.py"
spec = importlib.util.spec_from_file_location("build_catalog", SCRIPT)
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


def warnings(text, head=""):
    return build.risky({"_text": text, "_head": head})


def test_the_builder_flags_what_the_catalog_leaves_out():
    assert warnings("Run `npx --yes eas-cli@latest build`") == [
        "runs a tool at its unpinned newest version (npx ...@latest)", "skips a tool's own prompt with --yes"]
    assert "runs a tool at its unpinned newest version (npx ...@latest)" in warnings(
        '"command": "npx",\n  "args": ["-y", "chrome-devtools-mcp@latest"]')
    assert warnings("npx skills update " + "x " * 120 + "--yes") == ["skips a tool's own prompt with --yes"]
    assert warnings("remotion elements use https://example.com/index.json --yes") == [
        "skips a tool's own prompt with --yes"]
    assert warnings("Fetch https://docs.stripe.com/.well-known/skills/x/") == [
        "fetches more skill instructions at runtime"]
    assert warnings("", head="allowed-tools: Bash(npx:*)") == ["pre-approves broad shell commands in allowed-tools"]
    assert warnings("Context: !`git status`") == ["runs a command when the skill loads (!`...`)"]


def test_ordinary_project_commands_pass():
    text = "Run `npx playwright test` and use `tokio::select!` here; `npm create vite@latest` for new apps"
    assert warnings(text) == []
