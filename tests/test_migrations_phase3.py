"""The 1.12.0 configuration move (phase 3 step 7; the migration-author contract), and extension migrations.

Two framework migrations move routing.yaml values to their owners. Both run from what discovery reads: manifests,
never extension code.
- **`gpu` to `resources.gpu`:** the shared resource policy the framework owns.
- **Each manifest's `config.legacy_sections` into `extensions.<identity>`:** the section named like the identity at
  the top, any other nested under its own name (an extension `render`'s `encoder` becomes
  `extensions.render.encoder`).

A moved section keeps its comments, the comment on its key line included. Everything else stays byte for byte, in
the file's own line endings, and the result is read back before it is written.

An extension whose manifest names `migrations` (a module exposing MIGRATIONS) has them run by `supervisor migrate`
after the framework's. Only that module is imported; setup() never runs.

Rollback: the window's snapshot (migration-author, "Rollback"). v1.11.0 does not read the new places."""
from __future__ import annotations

import pytest
import yaml

from supervisor import migrations, testing, yaml_edit

ROUTING = (
    "# Routing policy v1. gpu limits below are the owner's (this comment mentions gpu, it is not moved)\n"
    "version: 1\n"
    "capacity:\n"
    "  claude: {reserve_percent: 20, weekly_gate_percent: 20}    # D-901\n"
    "gpu:\n"
    "  usable_vram_floor_mb: 6500          # assumed with desktop apps running\n"
    "  single_job: true                    # one gpu-lock holder at a time\n"
    "render:                                # the render server adapter (T0-13)\n"
    "  base_url: http://127.0.0.1:8188\n"
    "  timeout_s: 1800\n"
    "text_model:                            # Textgen adapter (T0-13)\n"
    "  base_url: http://127.0.0.1:11434\n"
    "encoder:\n"
    "  root: D:\\Repos\\encoder        # encoder.exe lives in <root>\\bin\n"
    "qa:                                    # QA v0, engine/qa\n"
    "  min_width: 512\n"
    "# roles below; an older writer wrapped this note and left card empty\n"
    "roles:\n"
    "  architect:\n"
    "    card:\n"
    "    note: a long note that an older writer wrapped at eighty columns, which a round trip would\n"
    "      unwrap\n"
)

MANIFESTS = {
    "render": ('identity = "render"\nversion = "1.0.0"\nframework = ">=1.11"\n[config]\nsection = "extensions.render"\n'
              'schema = "render:Settings"\nlegacy_sections = ["render", "encoder"]\n'),
    "text_model": ('identity = "text_model"\nversion = "1.0.0"\nframework = ">=1.11"\n[config]\n'
                   'section = "extensions.text_model"\nschema = "text_model:Settings"\n'
                   'legacy_sections = ["text_model"]\n'),
}
MODULE = "def setup(registry):\n    raise RuntimeError('setup must not run during migrate')\n"


def project(tmp_path, *, newline: str = "\r\n", manifests: dict | None = None, routing: str = ROUTING):
    manifests = MANIFESTS if manifests is None else manifests
    files = {f"extensions/{name}.toml": text for name, text in manifests.items()}
    layout = testing.project(tmp_path, modules={name: MODULE for name in manifests} or {"plain": MODULE},
                             files=files)
    (layout.config).mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_bytes(routing.replace("\n", newline).encode("utf-8"))
    return layout


def routing_of(layout) -> bytes:
    return (layout.config / "routing.yaml").read_bytes()


@pytest.fixture(autouse=True)
def _clean():
    testing.reset()
    yield
    testing.reset()


@pytest.mark.parametrize("newline", ["\r\n", "\n"], ids=["crlf", "lf"])
def test_the_dry_run_names_each_move_and_writes_nothing(tmp_path, newline):
    layout = project(tmp_path, newline=newline)
    before = routing_of(layout)
    notes = [n for n in migrations.run(layout, dry_run=True) if "framework_version" not in n]
    assert notes == ["config/routing.yaml: gpu moved to resources.gpu (shared resource policy)",
                     "config/routing.yaml: render, encoder moved to extensions.render (extension render)",
                     "config/routing.yaml: text_model moved to extensions.text_model (extension text_model)"]
    assert routing_of(layout) == before


@pytest.mark.parametrize("newline", ["\r\n", "\n"], ids=["crlf", "lf"])
def test_the_move_keeps_values_comments_line_endings_and_everything_else(tmp_path, newline):
    layout = project(tmp_path, newline=newline)
    before = yaml.safe_load(ROUTING)
    migrations.run(layout)
    raw = routing_of(layout)
    text = raw.decode("utf-8")
    assert (b"\r\n" in raw) is (newline == "\r\n") and (newline == "\n" or b"\n" not in raw.replace(b"\r\n", b""))
    doc = yaml.safe_load(text)
    assert doc["resources"] == {"gpu": before["gpu"]}
    assert doc["extensions"] == {"render": {**before["render"], "encoder": before["encoder"]},
                                 "text_model": before["text_model"]}
    assert doc["qa"] == before["qa"] and doc["capacity"] == before["capacity"] and doc["version"] == 1
    assert not {"gpu", "render", "text_model", "encoder"} & set(doc)
    for comment in ("# assumed with desktop apps running", "# one gpu-lock holder at a time",
                    "# the render server adapter", "# Textgen adapter", "# encoder.exe lives in", "# QA v0, engine/qa",
                    "# D-901",
                    "this comment mentions gpu, it is not moved"):
        assert comment in text, comment
    assert text.replace("\r\n", "\n").startswith(ROUTING.split("gpu:\n", 1)[0])    # untouched head, byte for byte
    tail = ROUTING[ROUTING.index("qa:"):]                    # after the moved sections: byte for byte, not reformatted
    assert tail in text.replace("\r\n", "\n")
    assert "    card:\n" in text.replace("\r\n", "\n") and "card: null" not in text


def test_a_second_run_changes_nothing(tmp_path):
    layout = project(tmp_path)
    migrations.run(layout)
    after = routing_of(layout)
    assert [n for n in migrations.run(layout) if "framework_version" not in n] == []
    assert routing_of(layout) == after


def test_the_values_an_extension_reads_are_the_same_before_and_after(tmp_path):
    """ext.config's legacy reading (step 6) and its reading of the migrated file give the same section."""
    from supervisor.ext import _config
    from supervisor.manifest import parse
    layout = project(tmp_path)
    m = parse(MANIFESTS["render"], source="render.toml")
    with pytest.warns(DeprecationWarning):
        legacy = _config._assembled(layout, m)
    migrations.run(layout)
    assert _config._assembled(layout, m) == legacy


def test_an_extension_already_moved_is_left_alone(tmp_path):
    routing = ROUTING + "extensions:\n  render:\n    base_url: http://127.0.0.1:9999\n"
    layout = project(tmp_path, routing=routing)
    notes = migrations.run(layout, dry_run=True)
    assert not any("extensions.render" in n for n in notes) and any("extensions.text_model" in n for n in notes)


def test_an_extension_without_a_manifest_keeps_its_sections(tmp_path):
    layout = project(tmp_path, manifests={})
    migrations.run(layout)
    doc = yaml.safe_load(routing_of(layout))
    assert {"render", "text_model", "encoder"} <= set(doc) and "extensions" not in doc and doc["resources"]["gpu"]


def test_a_flow_style_parent_falls_back_to_the_round_trip_and_still_moves_the_values():
    """`extensions: {}` cannot take block lines under it: the round trip moves the section instead, comments kept."""
    out = yaml_edit.move_sections("extensions: {}\nrender:   # lane\n  a: 1\n", [("render", ("extensions", "render"))])
    assert yaml.safe_load(out) == {"extensions": {"render": {"a": 1}}} and "# lane" in out


def test_the_move_refuses_a_destination_that_exists_and_checks_its_result():
    with pytest.raises(yaml_edit.EditRefused):
        yaml_edit.move_sections("a: {x: 1}\nb: {y: 2}\n", [("a", ("b",))])
    out = yaml_edit.move_sections("a:  # note\n  x: 1\nb: 2\n", [("a", ("c", "a"))])
    assert yaml.safe_load(out) == {"b": 2, "c": {"a": {"x": 1}}} and "# note" in out


# ---------------------------------------------------------------- extension migrations

EXT_MIGRATIONS = '''import pathlib
RAN = pathlib.Path({marker!r})

def rename_flag(layout, dry_run):
    RAN.write_text(f"dry_run={{dry_run}}")
    return ["state/notes.yaml: flag renamed"]

MIGRATIONS = [rename_flag]
'''


def test_an_extensions_migrations_run_without_setup(tmp_path):
    marker = tmp_path / "ran.txt"
    manifest = 'identity = "notes"\nversion = "1.0.0"\nframework = ">=1.11"\nmigrations = "notes_migrations"\n'
    layout = testing.project(tmp_path, modules={"notes": MODULE,
                                                "notes_migrations": EXT_MIGRATIONS.format(marker=str(marker))},
                             files={"extensions/notes.toml": manifest}, extensions=["notes"])
    notes = migrations.run(layout, dry_run=True)
    assert "notes: state/notes.yaml: flag renamed" in notes and marker.read_text() == "dry_run=True"
    migrations.run(layout)
    assert marker.read_text() == "dry_run=False"


def test_an_incompatible_extensions_migrations_do_not_run(tmp_path):
    marker = tmp_path / "ran.txt"
    manifest = 'identity = "notes"\nversion = "1.0.0"\nframework = ">=99"\nmigrations = "notes_migrations"\n'
    layout = testing.project(tmp_path, modules={"notes": MODULE,
                                                "notes_migrations": EXT_MIGRATIONS.format(marker=str(marker))},
                             files={"extensions/notes.toml": manifest}, extensions=["notes"])
    migrations.run(layout)
    assert not marker.exists()


def test_a_failing_extension_migration_stops_migrate_with_its_error(tmp_path):
    broken = "def boom(layout, dry_run):\n    raise ValueError('cannot read state/notes.yaml')\nMIGRATIONS = [boom]\n"
    manifest = 'identity = "notes"\nversion = "1.0.0"\nframework = ">=1.11"\nmigrations = "notes_migrations"\n'
    layout = testing.project(tmp_path, modules={"notes": MODULE, "notes_migrations": broken},
                             files={"extensions/notes.toml": manifest}, extensions=["notes"])
    with pytest.raises(migrations.MigrationError, match="notes.*cannot read state/notes.yaml"):
        migrations.run(layout)
