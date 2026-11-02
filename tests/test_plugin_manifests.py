"""The plugin manifests, which nothing else in the test suite would catch.

The repository is both a Python package and a plugin for Claude Code and
Codex. Codex reads `.codex-plugin/plugin.json` first and falls back to
`.claude-plugin/plugin.json`, so the two manifests have to agree: a plugin
that installs under one agent and silently misbehaves under the other is
exactly the failure this file exists to prevent. The same goes for adding a
skill directory and forgetting to list it in the marketplace entry -- the
plugin still installs, and the new skill is simply missing from the listing
a user browses.

Nothing here launches an agent; these are file-shape assertions.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CLAUDE_MANIFEST = ROOT / ".claude-plugin" / "plugin.json"
CODEX_MANIFEST = ROOT / ".codex-plugin" / "plugin.json"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
MCP_MANIFEST = ROOT / ".mcp.json"
SKILLS_DIR = ROOT / "skills"

# Every field that describes the same plugin to either agent. `description`
# is included deliberately: it is what the user reads before installing.
SHARED_MANIFEST_KEYS = (
    "name",
    "version",
    "description",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
    "skills",
    "mcpServers",
)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _skill_dirs() -> list[Path]:
    return sorted(p for p in SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file())


def test_manifests_are_valid_json():
    for path in (CLAUDE_MANIFEST, CODEX_MANIFEST, MARKETPLACE, MCP_MANIFEST):
        assert isinstance(_load(path), dict), path


def test_claude_and_codex_manifests_agree():
    claude, codex = _load(CLAUDE_MANIFEST), _load(CODEX_MANIFEST)
    for key in SHARED_MANIFEST_KEYS:
        assert claude.get(key) == codex.get(key), f"{key} differs between the two manifests"


def test_the_codex_manifest_carries_the_interface_block_codex_renders():
    """Codex installs a manifest without `interface`, then has nothing to show.

    `displayName`, `shortDescription`, `category` and `defaultPrompt` are
    what Codex puts on the plugin card and in the composer; with the block
    absent the plugin still installs and still works, but appears under its
    bare package name with no starter prompts. Every plugin in Codex's own
    curated marketplace carries this block, which is why it is asserted here
    rather than left to whoever next edits the manifest. It is deliberately
    Codex-only -- Claude Code has no use for it -- so it sits outside
    SHARED_MANIFEST_KEYS and the agreement test above.
    """
    interface = _load(CODEX_MANIFEST).get("interface")
    assert isinstance(interface, dict), "the Codex manifest needs an `interface` block"
    for key in ("displayName", "shortDescription", "category", "defaultPrompt"):
        assert interface.get(key), f"interface.{key} is what Codex renders; it cannot be blank"
    prompts = interface["defaultPrompt"]
    # Codex keeps the first three and truncates each at 128 characters, so
    # anything beyond that is written but never seen.
    assert isinstance(prompts, list) and 1 <= len(prompts) <= 3
    assert all(isinstance(p, str) and 0 < len(p) <= 128 for p in prompts)


@pytest.mark.parametrize("skill_dir", _skill_dirs(), ids=lambda p: p.name)
def test_every_skill_ships_codex_display_metadata(skill_dir: Path):
    """Codex reads per-skill presentation from `agents/openai.yaml`.

    A skill without it is listed by its raw directory name. Three of the four
    skills here shipped without one, so Claude Code showed all four and Codex
    showed one properly -- exactly the kind of one-agent-only drift the rest
    of this file exists to catch.
    """
    path = skill_dir / "agents" / "openai.yaml"
    assert path.is_file(), f"{skill_dir.name} has no agents/openai.yaml for Codex"
    interface = yaml.safe_load(path.read_text(encoding="utf-8"))["interface"]
    for key in ("display_name", "short_description", "default_prompt"):
        assert str(interface.get(key, "")).strip(), f"{skill_dir.name}: interface.{key} is blank"


def test_plugin_version_matches_the_package():
    # Read rather than parse: `tomllib` only exists from 3.11 and this
    # project still supports 3.10.
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE)
    assert match is not None, "pyproject.toml has no top-level version"
    version = match.group(1)
    assert _load(CLAUDE_MANIFEST)["version"] == version
    entry = _load(MARKETPLACE)["plugins"][0]
    assert entry["version"] == version


def test_marketplace_lists_every_skill_and_nothing_else():
    entry = _load(MARKETPLACE)["plugins"][0]
    assert entry["name"] == _load(CLAUDE_MANIFEST)["name"]
    listed = sorted(entry["skills"])
    expected = sorted(f"./skills/{p.name}" for p in _skill_dirs())
    assert listed == expected


@pytest.mark.parametrize("skill_dir", _skill_dirs(), ids=lambda p: p.name)
def test_skill_frontmatter_names_its_own_directory(skill_dir: Path):
    lines = (skill_dir / "SKILL.md").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "---", "SKILL.md must open with YAML frontmatter"
    end = lines.index("---", 1)
    fields = dict(
        line.split(":", 1) for line in lines[1:end] if ":" in line and not line.startswith(" ")
    )
    # A skill whose frontmatter name disagrees with its directory is invoked
    # under one name and found on disk under another.
    assert fields["name"].strip() == skill_dir.name
    assert fields["description"].strip(), "a skill with no description is never selected"


def test_mcp_server_command_is_plugin_root_relative():
    server = _load(MCP_MANIFEST)["mcpServers"]["endnotex9"]
    # `${CLAUDE_PLUGIN_ROOT}` is the variable *both* agents expand; Codex
    # ships the same name. An absolute or relative path here would work on
    # the author's machine and nowhere else.
    assert "${CLAUDE_PLUGIN_ROOT}" in server["args"]
    assert server["command"] == "uv"
    assert "--locked" in server["args"]
    assert (ROOT / "uv.lock").is_file()
