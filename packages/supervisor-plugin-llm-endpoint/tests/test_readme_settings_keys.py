"""F13: every configuration key the README documents must exist on `Settings` - a build that drops a key and
forgets the README should fail a test, not wait for a reviewer to notice."""
from __future__ import annotations

import re
from pathlib import Path

from supervisor_plugin_llm_endpoint.config import Settings

PKG = Path(__file__).resolve().parents[1]


def _readme_documented_keys() -> list[str]:
    lines = (PKG / "README.md").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if "Configure it under" in line)
    block = [lines[start]]
    for line in lines[start + 1:]:
        if line.startswith("- "):
            break
        block.append(line)
    sentence = " ".join(block)
    return [tok for tok in re.findall(r"`([^`]+)`", sentence) if re.fullmatch(r"[a-z_][a-z0-9_]*", tok)]


def test_every_readme_documented_settings_key_exists_on_settings():
    keys = _readme_documented_keys()
    assert keys, "no settings keys found in the README's configuration bullet"
    missing = [key for key in keys if key not in Settings.model_fields]
    assert missing == [], f"README documents key(s) not on Settings: {missing}"
