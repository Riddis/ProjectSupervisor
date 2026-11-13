"""F13 (T5-21) and F21 (T5-40): the README's configuration bullet and `Settings` agree in both
directions - every key the README documents exists on `Settings` (direction A, T5-21's own), and
every field of `Settings` is documented in the README (direction B, new here) - so a build that
drops a key, or adds one and forgets the README, fails a test rather than waiting for a reviewer
to notice.

`profile` is a config key (`__init__.py`'s `llm_endpoint_config()`, F12) but not a `Settings`
field - nothing ever constructs `Settings(profile=...)`, so neither direction below needs it to
pass or fail anything. It is documented in the README in its own bullet, right after the
"Configure it under" bullet both directions read, rather than inside it:
`_readme_documented_keys()` stops collecting at the next line that starts with "- ", so a key
named in a later bullet is simply never seen by either direction - it is owner-facing
documentation, not something either direction is supposed to catch."""
from __future__ import annotations

import re
from importlib import metadata
from pathlib import Path

from supervisor_plugin_llm_endpoint.config import Settings

DISTRIBUTION = "supervisor-plugin-llm-endpoint"
PKG = Path(__file__).resolve().parents[1]
README = PKG / "README.md"


def _readme_text() -> str:
    # The source tree ships beside tests/ in a worktree or fresh clone; once ci_package.py step 4 has copied
    # only tests/ next to an installed wheel, README.md is gone from beside tests/ too, but pyproject.toml's
    # `readme = "README.md"` means the wheel's own METADATA carries the same body.
    if README.is_file():
        return README.read_text(encoding="utf-8")
    return metadata.metadata(DISTRIBUTION).get_payload()


def _readme_documented_keys(text: str) -> list[str]:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if "Configure it under" in line)
    block = [lines[start]]
    for line in lines[start + 1:]:
        if line.startswith("- "):
            break
        block.append(line)
    sentence = " ".join(block)
    return [tok for tok in re.findall(r"`([^`]+)`", sentence) if re.fullmatch(r"[a-z_][a-z0-9_]*", tok)]


def _missing_from_settings(documented_keys: list[str], settings_fields: set[str]) -> list[str]:
    """Direction A: a key the README documents that `Settings` does not have."""
    return [key for key in documented_keys if key not in settings_fields]


def _missing_from_readme(settings_fields: set[str], documented_keys: list[str]) -> list[str]:
    """Direction B: a `Settings` field the README's configuration bullet never names."""
    return [field for field in sorted(settings_fields) if field not in documented_keys]


def test_every_readme_documented_settings_key_exists_on_settings():
    keys = _readme_documented_keys(_readme_text())
    assert keys, "no settings keys found in the README's configuration bullet"
    missing = _missing_from_settings(keys, set(Settings.model_fields))
    assert missing == [], f"README documents key(s) not on Settings: {missing}"


def test_every_settings_field_is_documented_in_the_readme():
    keys = _readme_documented_keys(_readme_text())
    missing = _missing_from_readme(set(Settings.model_fields), keys)
    assert missing == [], f"Settings field(s) the README's configuration bullet never documents: {missing}"


def test_a_readme_key_not_on_settings_fails_the_check():
    # Its own case, over a fixture of the two inputs each direction reads (the README's documented-key list and
    # the Settings field set) rather than the real README or the real Settings: "ghost_key" stands in for a key
    # someone typos or leaves behind in the README after a field is renamed or removed; "base_url" stands in for
    # an ordinary key that must not be flagged. If this function compared one list with itself it could not tell
    # the two apart.
    documented = ["base_url", "ghost_key"]
    fields = {"base_url", "model"}
    assert _missing_from_settings(documented, fields) == ["ghost_key"]


def test_a_settings_field_missing_from_the_readme_fails_the_check():
    # The companion case for direction B, over its own fixture: "secret_key" stands in for a field someone adds
    # to Settings and forgets to document (T5-27's defer_budget_s, T5-37's hand_over_grace_s); "base_url" stands
    # in for a field that is documented and must not be flagged.
    documented = ["base_url"]
    fields = {"base_url", "secret_key"}
    assert _missing_from_readme(fields, documented) == ["secret_key"]
