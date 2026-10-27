"""Deprecation of internal imports (phase 2 step 8, gate G6 with the warning): an extension that imports anything from
`supervisor` besides `supervisor.ext` and `supervisor.testing` still loads, the loader issues a DeprecationWarning
at that file and line, and `supervisor doctor` shows a WARN naming every such import; the reference extension shows
none. The scan reads source (AST), so the framework's own imports never count, and covers every module in the
project's extensions/ folder, helpers included. A FAIL comes only with a later major release."""
from __future__ import annotations

import shutil
import warnings
from pathlib import Path

import pytest

from supervisor import doctor, extensions, testing

REPO = Path(__file__).resolve().parents[1]

OLD_STYLE = '''from supervisor.ext import Check
from supervisor import inbox
import supervisor.tasks as tasks_mod

def setup(registry):
    from supervisor.ui.deps import render       # inside a function: still an import of the internals
    registry.doctor_check(lambda layout: Check("old", True, "ok"))
'''
HELPER = "from supervisor.adapters.python import job\n"
CLEAN = "from supervisor import ext, testing\nfrom supervisor.ext import ui\nfrom . import helper_clean\n\ndef setup(registry):\n    pass\n"


def findings(layout):
    return [(f.file, f.line, f.module) for f in extensions.load(layout).internal_imports]


def test_an_extension_with_internal_imports_loads_and_warns_at_file_and_line(layout):
    testing.add_extensions(layout, {"old": OLD_STYLE, "helper": HELPER}, entries=["old"])
    with pytest.warns(DeprecationWarning) as caught:
        reg = extensions.load(layout)
    assert reg.loaded == ["old"] and reg.errors == []                       # still loads: a warning, not a failure
    assert findings(layout) == [
        ("extensions/helper.py", 1, "supervisor.adapters.python"),          # a helper module is scanned too
        ("extensions/old.py", 2, "supervisor.inbox"),
        ("extensions/old.py", 3, "supervisor.tasks"),
        ("extensions/old.py", 6, "supervisor.ui.deps"),
    ]
    places = {(Path(w.filename).name, w.lineno) for w in caught if issubclass(w.category, DeprecationWarning)}
    assert {("old.py", 2), ("old.py", 3), ("old.py", 6), ("helper.py", 1)} <= places
    assert all("supervisor.ext" in str(w.message) for w in caught)
    testing.reset()


def test_doctor_shows_a_warning_not_a_failure(layout):
    testing.add_extensions(layout, {"old": OLD_STYLE}, entries=["old"])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        check = next(c for c in doctor.run(layout) if c.name == "extension_imports")
    assert check.ok and check.warn
    assert "extensions/old.py:2 supervisor.inbox" in check.detail and "1.11.0" in check.detail
    testing.reset()


def test_the_doctor_command_prints_warn_and_exits_by_the_failures_only(layout, monkeypatch):
    from typer.testing import CliRunner

    from supervisor.cli import app
    monkeypatch.setattr(doctor, "CHECKS", [lambda lay: doctor.Check("ok_one", True, "fine"),
                                           lambda lay: doctor.Check("warned", True, "look at this", warn=True)])
    result = CliRunner().invoke(app, ["doctor"])
    assert "WARN warned: look at this" in result.output and "OK   ok_one: fine" in result.output
    assert result.exit_code == 0


def test_public_imports_only_give_no_warning(layout):
    testing.add_extensions(layout, {"clean": CLEAN, "helper_clean": "from supervisor.ext import Layout\n"},
                           entries=["clean"])
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert extensions.load(layout).errors == []
    assert findings(layout) == []
    check = next(c for c in doctor.run(layout) if c.name == "extension_imports")
    assert check.ok and not check.warn
    testing.reset()


def test_the_reference_extension_shows_none(layout):
    shutil.copytree(REPO / "examples" / "reference_extension" / "reference", layout.extensions / "reference",
                    ignore=shutil.ignore_patterns("__pycache__"))
    testing.add_extensions(layout, {}, entries=["reference"])
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert extensions.load(layout).errors == []
    assert findings(layout) == []
    testing.reset()


def test_the_scan_reads_installed_extension_modules_too(layout, tmp_path, monkeypatch):
    pkg = tmp_path / "site" / "acme_ext"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("from supervisor.ext import Check\n\ndef setup(registry):\n    pass\n",
                                     encoding="utf-8")
    (pkg / "internals.py").write_text("\n\nfrom supervisor import store\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path / "site"))
    testing.add_extensions(layout, {}, entries=["acme_ext"])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert extensions.load(layout).loaded == ["acme_ext"]
    found = findings(layout)
    assert len(found) == 1 and found[0][1:] == (3, "supervisor.store") and found[0][0].endswith("internals.py")
    testing.reset()


def test_an_extension_that_does_not_parse_is_left_to_the_loader(layout):
    testing.add_extensions(layout, {"broken": "def setup(registry):\n    pass\n", "garbled": "def (:\n"},
                           entries=["broken"])
    assert extensions.load(layout).errors == [] and findings(layout) == []
    testing.reset()


def test_each_import_warns_once_per_process(layout):
    """The CLI, the loop and the Control Center each load the registry; one process warns once per import."""
    testing.add_extensions(layout, {"old": OLD_STYLE}, entries=["old"])
    with pytest.warns(DeprecationWarning):
        extensions.load(layout)
    extensions._registries.clear()                        # loaded again in the same process (not a fresh one)
    with warnings.catch_warnings(record=True) as again:
        warnings.simplefilter("always")
        assert extensions.load(layout).internal_imports  # still found and reported by doctor ...
    assert not [w for w in again if issubclass(w.category, DeprecationWarning)]   # ... but not warned twice
    testing.reset()
