"""Review 1.12.0 R3 (P2): a refused extension's code does not run by any import path the framework sets up. The
loader refused only its own import of a refused extension. An accepted sibling's `from . import bad`, the
extensions/ package initializer, or a schema module still executed the refused module's import-time code, while
the loader reported it "refused before import". A guard on the import system now refuses the modules of refused
configured extensions for the rest of the process, until reset(). Approval is decided before the first import, the
package initializer's included.

This is not a sandbox: an accepted extension is trusted code, and the guard closes the import paths, not every way
to run a file."""
from __future__ import annotations

import importlib

import pytest

from supervisor import extensions, migrations, testing


def manifest(name: str, extra: str = "", framework: str = ">=1.12") -> str:
    return f'identity = "{name}"\nversion = "1.0.0"\nframework = "{framework}"\n' + extra


def trap(marker) -> str:
    return f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\ndef setup(r):\n    pass\n"


@pytest.fixture(autouse=True)
def _clean():
    testing.reset()
    yield
    testing.reset()


def project(tmp_path, marker, *, good: str = "from . import bad  # noqa: F401\ndef setup(r):\n    pass\n",
            files: dict | None = None, extensions_list=("good", "bad")):
    files = {"extensions/good.toml": manifest("good"), "extensions/bad.toml": manifest("bad", framework=">=99"),
             **(files or {})}
    return testing.project(tmp_path / "p", modules={"good": good, "bad": trap(marker)}, files=files,
                           extensions=list(extensions_list))


def test_a_sibling_import_does_not_run_a_refused_extension(tmp_path):
    """Astra's probe: `good` imports the refused `bad` without declaring it."""
    marker = tmp_path / "refused-ran"
    reg = extensions.load(project(tmp_path, marker))
    assert not marker.exists()
    assert "good" not in reg.loaded and any(e.startswith("good:") and "refused" in e for e in reg.errors)


def test_the_package_initializer_does_not_run_a_refused_extension(tmp_path):
    marker = tmp_path / "refused-ran"
    layout = project(tmp_path, marker, good="def setup(r):\n    pass\n",
                     files={"extensions/__init__.py": "from . import bad  # noqa: F401\n"})
    extensions.load(layout)
    assert not marker.exists()


def test_a_schema_module_does_not_run_a_refused_extension(tmp_path):
    marker = tmp_path / "refused-ran"
    schema = ("from . import bad  # noqa: F401\nfrom pydantic import BaseModel\n\nclass Settings(BaseModel):\n"
              "    value: int = 1\n")
    extra = '[config]\nsection = "extensions.good"\nschema = "good_config:Settings"\n'
    layout = project(tmp_path, marker, good="def setup(r):\n    pass\n",
                     files={"extensions/good.toml": manifest("good", extra), "extensions/good_config.py": schema})
    extensions.load(layout)
    assert not marker.exists()


def test_a_later_import_of_a_refused_extension_is_refused_too(tmp_path):
    marker = tmp_path / "refused-ran"
    layout = project(tmp_path, marker, good="def setup(r):\n    pass\n")
    assert extensions.load(layout).loaded == ["good"]
    with pytest.raises(ImportError, match="refused"):
        importlib.import_module(f"{extensions._package_name(layout)}.bad")
    assert not marker.exists()


def test_migrate_does_not_run_a_refused_extension_through_an_approved_migration(tmp_path):
    marker = tmp_path / "refused-ran"
    good = manifest("good", 'migrations = "good_migrations"\n')
    layout = testing.project(tmp_path / "p", modules={"good": "def setup(r):\n    pass\n", "bad": trap(marker),
                                                      "good_migrations": "from . import bad  # noqa: F401\n"
                                                                         "MIGRATIONS = []\n"},
                             files={"extensions/good.toml": good,
                                    "extensions/bad.toml": manifest("bad", framework=">=99")},
                             extensions=["good", "bad"])
    with pytest.raises(migrations.MigrationError):
        migrations.run(layout)
    assert not marker.exists()


def test_after_a_reset_a_module_of_that_name_may_load_again(tmp_path):
    marker = tmp_path / "refused-ran"
    extensions.load(project(tmp_path, marker, good="def setup(r):\n    pass\n"))
    testing.reset()
    layout = testing.project(tmp_path / "q", modules={"bad": "def setup(r):\n    pass\n"},
                             files={"extensions/bad.toml": manifest("bad")})
    assert extensions.load(layout).loaded == ["bad"]
