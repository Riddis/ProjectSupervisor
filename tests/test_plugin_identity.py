"""The registry carries the identity (phase 3 step 5): during setup(), `registry.identity` is the identity of the
plugin being set up. That is its manifest's `identity`, which discovery holds equal to the name project.yaml uses,
or that name for a plugin without a manifest. A plugin builds its namespaced identifiers from it, instead of
repeating its own name. Outside setup() there is no plugin being set up, and the identity is None."""
from __future__ import annotations

import pytest

from supervisor import plugin, plugins, testing
from supervisor.adapters.python import JOBS

NAMES_ITSELF = '''
SEEN = []

def job(root, run_dir, **params):
    return {"status": "COMPLETE", "artifacts": [], "notes": ""}

def setup(registry):
    SEEN.append(registry.identity)
    registry.python_job(registry.identity + "_sync", job)
'''

MANIFEST = 'identity = "{name}"\nversion = "1.0.0"\nframework = ">=1.11"\n[provides]\npython_jobs = ["{name}_sync"]\n'


@pytest.fixture(autouse=True)
def _clean():
    testing.reset()
    yield
    testing.reset()


@pytest.mark.parametrize("with_manifest", [True, False], ids=["manifest", "unmanifested"])
def test_setup_sees_its_own_identity_and_names_its_registrations_with_it(tmp_path, with_manifest):
    files = {"plugins/notes.toml": MANIFEST.format(name="notes")} if with_manifest else {}
    layout = testing.project(tmp_path, modules={"notes": NAMES_ITSELF}, files=files)
    reg = plugins.load(layout)
    assert reg.loaded == ["notes"] and "notes_sync" in JOBS
    module = next(m for n, m in __import__("sys").modules.items() if n.endswith(".notes"))
    assert module.SEEN == ["notes"]


def test_two_plugins_each_see_their_own(tmp_path):
    layout = testing.project(tmp_path, modules={"notes": NAMES_ITSELF, "diary": NAMES_ITSELF},
                             files={"plugins/notes.toml": MANIFEST.format(name="notes"),
                                    "plugins/diary.toml": MANIFEST.format(name="diary")})
    reg = plugins.load(layout)
    assert reg.loaded == ["notes", "diary"] and {"notes_sync", "diary_sync"} <= set(JOBS)


def test_outside_setup_there_is_no_identity(tmp_path):
    layout = testing.project(tmp_path, modules={"notes": NAMES_ITSELF})
    assert plugins.load(layout).identity is None


def test_the_documented_registry_declares_the_identity():
    assert "identity" in plugin.Registry.__annotations__
