"""Review 1.12.0 R1 (P1): `supervisor migrate` applies the loader's import-free approval before it moves a plugin's
configuration or imports a plugin's migration module. A manifest the loader refuses is skipped:
- one naming a framework-owned legacy section (`limits`);
- one of two manifests claiming the same legacy section;
- one whose requirement is refused.

Before this, such a manifest could move the framework's `limits` under `plugins.bad` and run its migration code
during the very command meant to repair a project. A manifest whose plugin only fails in setup() is not refused:
migrate never runs setup(), so it still migrates."""
from __future__ import annotations

import pytest
import yaml

from supervisor import plugins, migrations, testing


def manifest(name: str, extra: str = "", framework: str = ">=1.12") -> str:
    return f'identity = "{name}"\nversion = "1.0.0"\nframework = "{framework}"\n' + extra


def migration_module(marker) -> str:
    return f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\nMIGRATIONS = []\n"


@pytest.fixture(autouse=True)
def _clean():
    testing.reset()
    yield
    testing.reset()


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "apply"])
def test_a_manifest_the_loader_refuses_moves_nothing_and_runs_no_migration_code(tmp_path, dry_run):
    """Astra's probe: legacy section `limits` belongs to the framework."""
    marker = tmp_path / "migration-ran"
    extra = ('migrations = "bad_migrations"\n[config]\nsection = "plugins.bad"\nschema = "bad:Settings"\n'
             'legacy_sections = ["limits"]\n')
    layout = testing.project(tmp_path / "p", modules={"bad": "def setup(r): pass\n",
                                                      "bad_migrations": migration_module(marker)},
                             plugins=["bad"], files={"plugins/bad.toml": manifest("bad", extra)},
                             routing={"limits": {"max_agent_jobs": 1}})
    before = (layout.config / "routing.yaml").read_bytes()
    assert any("framework owns" in e for e in plugins.load(layout).errors)
    notes = migrations.run(layout, dry_run=dry_run)
    assert not any("limits" in n for n in notes)
    assert (layout.config / "routing.yaml").read_bytes() == before and not marker.exists()


def test_a_legacy_section_two_manifests_claim_is_moved_for_neither(tmp_path):
    def claims(name):
        return manifest(name, f'[config]\nsection = "plugins.{name}"\nschema = "{name}:Settings"\n'
                              'legacy_sections = ["shared"]\n')
    layout = testing.project(tmp_path, modules={"one": "def setup(r): pass\n", "two": "def setup(r): pass\n"},
                             files={"plugins/one.toml": claims("one"), "plugins/two.toml": claims("two")},
                             routing={"shared": {"a": 1}})
    notes = migrations.run(layout)
    doc = yaml.safe_load((layout.config / "routing.yaml").read_text(encoding="utf-8"))
    assert doc == {"shared": {"a": 1}} and not any("shared" in n for n in notes)


def test_a_plugin_whose_requirement_is_refused_does_not_migrate(tmp_path):
    marker = tmp_path / "migration-ran"
    layout = testing.project(tmp_path / "p", modules={"base": "def setup(r): pass\n",
                                                      "user": "def setup(r): pass\n",
                                                      "user_migrations": migration_module(marker)},
                             plugins=["base", "user"],
                             files={"plugins/base.toml": manifest("base", framework=">=99"),
                                    "plugins/user.toml": manifest("user", 'migrations = "user_migrations"\n'
                                                                             '[requires]\nplugins = ["base"]\n')})
    migrations.run(layout)
    assert not marker.exists()


def test_a_plugin_that_only_fails_in_setup_still_migrates(tmp_path):
    """Static refusal is not setup failure: migrate needs no setup()."""
    marker = tmp_path / "migration-ran"
    layout = testing.project(tmp_path / "p", modules={"flaky": "def setup(r):\n    raise RuntimeError('down')\n",
                                                      "flaky_migrations": migration_module(marker)},
                             plugins=["flaky"],
                             files={"plugins/flaky.toml": manifest("flaky", 'migrations = "flaky_migrations"\n')})
    assert any("down" in e for e in plugins.load(layout).errors)
    migrations.run(layout)
    assert marker.exists()
