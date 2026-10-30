"""2.0.0: "extension" is renamed "plugin", a clean break (owner decision 2026-10-29). `supervisor migrate` moves a
project from the 1.13.0 names to the new ones, and nothing else:

- project.yaml `extensions:` -> `plugins:`, config/routing.yaml `extensions:` -> `plugins:` (the key line only;
  comments, values and line endings stay);
- the project's code folder `extensions/` -> `plugins/`;
- each local manifest: `extensions/<name>.toml` and `supervisor-extension.toml` keep their content apart from
  `[requires] extensions` -> `plugins` and the config section `extensions.<identity>` -> `plugins.<identity>`, and a
  package manifest's file name becomes `supervisor-plugin.toml`.

Code that imports `supervisor.ext` or the old folder is the project's to change, and so is a local manifest's
`framework` range that leaves out the new major version; migrate names both. The words
here are invented."""
from __future__ import annotations

from pathlib import Path

import pytest

from supervisor import migrations, plugins, project, store, testing

PROJECT = ("name: Demo\r\nframework_version: 1.13.0\r\n# what the loop loads\r\nextensions:\r\n- alpha\r\n"
           "- beta\r\n")
ROUTING = ("limits:\r\n  max_agent_jobs: 1\r\nextensions:          # each plugin's own section\r\n  alpha:\r\n"
           "    colour: teal\r\n")
ALPHA = ("from supervisor import plugin\r\n\r\n\r\ndef check(layout):\r\n"
         "    return plugin.Check('alpha', True, plugin.config(layout, 'alpha')['colour'])\r\n\r\n\r\n"
         "def setup(registry):\r\n    registry.doctor_check(check)\r\n")
ALPHA_MANIFEST = ('identity = "alpha"\r\nversion = "1.0.0"\r\nframework = ">=1.12,<3"\r\n\r\n[provides]\r\n'
                  'doctor_checks = ["alpha"]\r\n\r\n[config]\r\nsection = "extensions.alpha"\r\n'
                  'schema = "alpha_settings:Settings"\r\n\r\n[requires]\r\nextensions = ["beta"]\r\n')
SETTINGS = "from pydantic import BaseModel\r\n\r\n\r\nclass Settings(BaseModel):\r\n    colour: str\r\n"
BETA_INIT = "def setup(registry):\r\n    pass\r\n"
BETA_MANIFEST = 'identity = "beta"\r\nversion = "1.0.0"\r\nframework = ">=1.12,<3"\r\n'
OLD_CODE = "from supervisor import ext\r\nfrom extensions import alpha\r\n"


def old_project(tmp_path):
    lay = testing.project(tmp_path / "p")
    (lay.root / "project.yaml").write_bytes(PROJECT.encode())
    lay.config.mkdir(parents=True, exist_ok=True)
    (lay.config / "routing.yaml").write_bytes(ROUTING.encode())
    old = lay.root / "extensions"
    (old / "beta").mkdir(parents=True)
    (old / "__init__.py").write_bytes(b"")
    (old / "alpha.py").write_bytes(ALPHA.encode())
    (old / "alpha.toml").write_bytes(ALPHA_MANIFEST.encode())
    (old / "alpha_settings.py").write_bytes(SETTINGS.encode())
    (old / "beta" / "__init__.py").write_bytes(BETA_INIT.encode())
    (old / "beta" / "supervisor-extension.toml").write_bytes(BETA_MANIFEST.encode())
    (lay.root / "tools").mkdir()
    (lay.root / "tools" / "report.py").write_bytes(OLD_CODE.encode())
    return lay


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_a_dry_run_lists_the_rename_and_changes_nothing(tmp_path):
    lay = old_project(tmp_path)
    before = snapshot(lay.root)
    notes = migrations.run(lay, dry_run=True)
    text = "\n".join(notes)
    assert "project.yaml: extensions renamed plugins" in text
    assert "config/routing.yaml: extensions renamed plugins" in text
    assert "extensions/ moved to plugins/" in text
    assert snapshot(lay.root) == before


def test_migrate_renames_the_keys_the_folder_and_the_manifests_and_the_plugins_load(tmp_path):
    lay = old_project(tmp_path)
    notes = migrations.run(lay)
    root = lay.root
    assert (root / "project.yaml").read_bytes() == PROJECT.replace("1.13.0", migrations.__version__).replace(
        "extensions:", "plugins:").encode()
    assert (root / "config" / "routing.yaml").read_bytes() == ROUTING.replace("extensions:", "plugins:").encode()
    assert not (root / "extensions").exists()
    assert (root / "plugins" / "alpha.py").read_bytes() == ALPHA.encode()
    assert (root / "plugins" / "alpha.toml").read_bytes() == ALPHA_MANIFEST.replace(
        '"extensions.alpha"', '"plugins.alpha"').replace("extensions = [", "plugins = [").encode()
    assert not (root / "plugins" / "beta" / "supervisor-extension.toml").exists()
    assert (root / "plugins" / "beta" / "supervisor-plugin.toml").read_bytes() == BETA_MANIFEST.encode()
    assert any("tools/report.py" in n and "supervisor.plugin" in n for n in notes)      # code to change, named
    reg = plugins.load(lay)
    assert sorted(reg.loaded) == ["alpha", "beta"] and reg.errors == []
    testing.reset()
    again = migrations.run(lay)
    assert not any("renamed" in n or "moved" in n for n in again)


def test_both_names_at_once_stop_the_migration(tmp_path):
    lay = old_project(tmp_path)
    (lay.root / "project.yaml").write_bytes((PROJECT + "plugins:\r\n- gamma\r\n").encode())
    before = snapshot(lay.root)
    with pytest.raises(migrations.MigrationError, match="both extensions and plugins"):
        migrations.run(lay)
    assert snapshot(lay.root) == before


def test_both_folders_at_once_stop_the_migration(tmp_path):
    lay = old_project(tmp_path)
    (lay.root / "plugins").mkdir()
    (lay.root / "plugins" / "gamma.py").write_bytes(b"x = 1\n")
    before = snapshot(lay.root)
    with pytest.raises(migrations.MigrationError, match="both extensions/ and plugins/"):
        migrations.run(lay)
    assert snapshot(lay.root) == before


def test_an_empty_plugins_folder_is_replaced_by_the_old_one(tmp_path):
    lay = old_project(tmp_path)
    (lay.root / "plugins").mkdir()
    migrations.run(lay)
    assert (lay.root / "plugins" / "alpha.py").is_file() and not (lay.root / "extensions").exists()


def test_a_project_without_the_old_names_is_left_alone(tmp_path):
    lay = testing.project(tmp_path / "p", name="Demo", routing={"plugins": {}})
    notes = migrations.run(lay, dry_run=True)
    assert not any("renamed" in n or "moved" in n for n in notes)


def test_a_local_manifest_whose_range_leaves_out_this_framework_is_named(tmp_path):
    """A clean break is a major version: a local plugin declaring `<2` no longer loads. Its range is its author's
    promise, so migrate names it rather than widening it."""
    lay = old_project(tmp_path)
    old = (lay.root / "extensions" / "alpha.toml").read_bytes()
    (lay.root / "extensions" / "alpha.toml").write_bytes(old.replace(b'">=1.12,<3"', b'">=1.12,<1.13"'))
    notes = migrations.run(lay, dry_run=True)
    assert any("plugins/alpha.toml" in n and "framework" in n and ">=1.12,<1.13" in n for n in notes)


def test_a_package_with_both_manifest_names_stops_the_migration_before_any_write(tmp_path):
    """Review 2.0.0 R1-2 (partial report): the clash surfaced at the manifest's rename, after project.yaml,
    routing.yaml and the folder had already moved."""
    lay = old_project(tmp_path)
    (lay.root / "extensions" / "beta" / "supervisor-plugin.toml").write_bytes(BETA_MANIFEST.encode())
    before = snapshot(lay.root)
    with pytest.raises(migrations.MigrationError, match="both supervisor-extension.toml and supervisor-plugin.toml"):
        migrations.run(lay)
    assert snapshot(lay.root) == before


# Review 2.0.0 R3 (partial report): valid TOML in another form (a quoted key, an inline table, a dotted key) was left
# with the old names; migrate still recorded 2.0.0 and the plugin then refused to load. Every form a manifest may
# take is renamed, and a manifest that still reads the old names afterwards stops migrate before anything is written.
HEAD_LINES = 'identity = "alpha"\r\nversion = "1.0.0"\r\nframework = ">=1.12,<3"\r\n'
PROVIDES = '\r\n[provides]\r\ndoctor_checks = ["alpha"]\r\n'
CONFIG_TABLE = '\r\n[config]\r\nsection = "extensions.alpha"\r\nschema = "alpha_settings:Settings"\r\n'
REQUIRES_TABLE = '\r\n[requires]\r\nextensions = ["beta"]\r\n'

FORMS = {
    "quoted key": HEAD_LINES + PROVIDES + CONFIG_TABLE + '\r\n[requires]\r\n"extensions" = ["beta"]\r\n',
    "literal-quoted key": HEAD_LINES + PROVIDES + CONFIG_TABLE + "\r\n[requires]\r\n'extensions' = [\"beta\"]\r\n",
    "inline requires": HEAD_LINES + 'requires = { extensions = ["beta"] }\r\n' + PROVIDES + CONFIG_TABLE,
    "dotted requires": HEAD_LINES + 'requires.extensions = ["beta"]\r\n' + PROVIDES + CONFIG_TABLE,
    "inline config": (HEAD_LINES + "config = { section = 'extensions.alpha', schema = \"alpha_settings:Settings\" }\r\n"
                      + PROVIDES + REQUIRES_TABLE),
    "spaced table header": HEAD_LINES + PROVIDES + CONFIG_TABLE + '\r\n[ requires ]\r\n  extensions   = ["beta"]\r\n',
}


@pytest.mark.parametrize("form", sorted(FORMS))
def test_every_form_of_the_old_names_in_a_manifest_is_renamed_and_the_plugin_loads(tmp_path, form):
    lay = old_project(tmp_path)
    (lay.root / "extensions" / "alpha.toml").write_bytes(FORMS[form].encode())
    migrations.run(lay)
    text = (lay.root / "plugins" / "alpha.toml").read_bytes().decode()
    assert "extensions" not in text and "\r\n" in text
    reg = plugins.load(lay)
    assert sorted(reg.loaded) == ["alpha", "beta"] and reg.errors == []
    testing.reset()


# Review 2.0.0 F1: `_OLD_SECTION` matched every quoted string beginning `extensions.`, wherever it occurred in the
# manifest, not just `config.section`'s value. A legacy `extensions.<name>` command identifier (kept in
# `legacy_ids` on purpose, for an identifier older than the namespace rule) and syntax-looking text inside an
# unrelated string were silently rewritten too. Only `requires.extensions` (the key) and `config.section` (the
# value's `extensions.` prefix) may change; every other value, every comment and the line endings stay exactly as
# they were.
F1_MANIFEST = (HEAD_LINES
              + '\r\n# a plugin that kept its old command name on purpose\r\n'
                '[provides]\r\ndoctor_checks = ["alpha"]\r\ncommands = ["extensions.submit"]\r\n'
              + '\r\n[legacy_ids]\r\ncommands = ["extensions.submit"]\r\n'
              + CONFIG_TABLE
              + '\r\n[requires]\r\nservices = ["remote, extensions = [x]"]\r\nextensions = ["beta"]\r\n')
F1_EXPECTED = F1_MANIFEST.replace('section = "extensions.alpha"', 'section = "plugins.alpha"').replace(
    'extensions = ["beta"]', 'plugins = ["beta"]')


def test_f1_a_legacy_command_identifier_and_a_syntax_looking_string_are_kept(tmp_path):
    lay = old_project(tmp_path)
    (lay.root / "extensions" / "alpha.toml").write_bytes(F1_MANIFEST.encode())
    migrations.run(lay)
    assert (lay.root / "plugins" / "alpha.toml").read_bytes() == F1_EXPECTED.encode()
    reg = plugins.load(lay)
    assert sorted(reg.loaded) == ["alpha", "beta"] and reg.errors == []
    testing.reset()


def test_f1_the_same_manifest_with_lf_line_endings(tmp_path):
    lay = old_project(tmp_path)
    (lay.root / "extensions" / "alpha.toml").write_bytes(F1_MANIFEST.replace("\r\n", "\n").encode())
    migrations.run(lay)
    text = (lay.root / "plugins" / "alpha.toml").read_bytes()
    assert text == F1_EXPECTED.replace("\r\n", "\n").encode()
    assert b"\r" not in text
    reg = plugins.load(lay)
    assert sorted(reg.loaded) == ["alpha", "beta"] and reg.errors == []
    testing.reset()


# The same finding's other half, in the form the review demonstrated: one `requires = { ... }` inline table whose
# `services` value is a string that reads like TOML syntax. The old inline rule matched `extensions =` after any `,`
# in the captured line, so the comma inside that string made the service itself `"remote, plugins = [x]"`. Only the
# `extensions` key of that inline table may change; the string, the legacy command identifiers and every other byte
# stay as they are.
F1_INLINE = (HEAD_LINES
             + 'requires = { services = ["remote, extensions = [x]"], extensions = ["beta"] }\r\n'
             + '\r\n# a plugin that kept its old command name on purpose\r\n'
               '[provides]\r\ndoctor_checks = ["alpha"]\r\ncommands = ["extensions.submit"]\r\n'
             + '\r\n[legacy_ids]\r\ncommands = ["extensions.submit"]\r\n'
             + CONFIG_TABLE)
F1_INLINE_EXPECTED = F1_INLINE.replace('section = "extensions.alpha"', 'section = "plugins.alpha"').replace(
    'extensions = ["beta"]', 'plugins = ["beta"]')


@pytest.mark.parametrize("nl", ["\r\n", "\n"])
def test_f1_a_syntax_looking_string_in_a_requires_inline_table_is_kept(tmp_path, nl):
    lay = old_project(tmp_path)
    (lay.root / "extensions" / "alpha.toml").write_bytes(F1_INLINE.replace("\r\n", nl).encode())
    migrations.run(lay)
    assert (lay.root / "plugins" / "alpha.toml").read_bytes() == F1_INLINE_EXPECTED.replace("\r\n", nl).encode()
    reg = plugins.load(lay)
    assert sorted(reg.loaded) == ["alpha", "beta"] and reg.errors == []
    testing.reset()


# The same defect class in one more position: a key literally named `extensions` where the migration never names one
# (here a stray top-level key, which the loader's strict parse is the one to report). The rename is read inside the
# `[requires]` table's own span, so such a key is left exactly as it is, rather than renamed and then refused with
# advice about a field migrate does not touch.
F1_STRAY = HEAD_LINES + 'extensions = ["alpha"]\r\n' + PROVIDES + CONFIG_TABLE + REQUIRES_TABLE
F1_STRAY_EXPECTED = F1_STRAY.replace('section = "extensions.alpha"', 'section = "plugins.alpha"').replace(
    'extensions = ["beta"]', 'plugins = ["beta"]')


def test_f1_a_key_named_extensions_outside_requires_is_left_as_it_is(tmp_path):
    lay = old_project(tmp_path)
    (lay.root / "extensions" / "alpha.toml").write_bytes(F1_STRAY.encode())
    migrations.run(lay)
    assert (lay.root / "plugins" / "alpha.toml").read_bytes() == F1_STRAY_EXPECTED.encode()


def test_a_manifest_that_still_reads_the_old_names_stops_migrate_before_any_write(tmp_path):
    lay = old_project(tmp_path)
    escaped = HEAD_LINES + PROVIDES + CONFIG_TABLE + '\r\n[requires]\r\n"\\u0065xtensions" = ["beta"]\r\n'
    (lay.root / "extensions" / "alpha.toml").write_bytes(escaped.encode())
    before = snapshot(lay.root)
    with pytest.raises(migrations.MigrationError, match="alpha.toml"):
        migrations.run(lay)
    assert snapshot(lay.root) == before


# The same class as R3: project.yaml may list a plugin by the old folder's dotted name (`extensions.alpha`, imported
# with the project folder on the path in 1.13.0). After the move that import fails, and which new name it takes
# (`alpha` or `plugins.alpha`) changes how the plugin is identified, so migrate refuses it, naming the entry, before
# anything is written. A dotted name the old folder does not hold is an installed module and is left as it is.

def test_a_plugin_listed_by_the_old_folders_dotted_name_stops_migrate_before_any_write(tmp_path):
    lay = old_project(tmp_path)
    (lay.root / "project.yaml").write_bytes(PROJECT.replace("- alpha\r\n", "- {name: extensions.alpha}\r\n").encode())
    before = snapshot(lay.root)
    with pytest.raises(migrations.MigrationError, match="extensions.alpha"):
        migrations.run(lay)
    assert snapshot(lay.root) == before


def test_a_dotted_name_the_old_folder_does_not_hold_is_left_alone(tmp_path):
    lay = old_project(tmp_path)
    (lay.root / "project.yaml").write_bytes(PROJECT.replace("- beta\r\n", "- beta\r\n- extensions.elsewhere\r\n").encode())
    notes = migrations.run(lay, dry_run=True)
    assert "project.yaml: extensions renamed plugins" in notes


# Review 2.0.0 F3: `[requires] extensions = []` and `plugins = []` together is valid TOML. The rename produced two
# `plugins` keys; parsing that result failed, but `_still_old` caught the TOMLDecodeError its own rewrite raised and
# returned False, so preflight read "not old any more" and accepted the rewrite, which the apply path then wrote as
# invalid text after project.yaml, routing.yaml and the folder had already moved. The conflict is refused before
# any write, and the dry run reports that refusal rather than announcing the migration.
BOTH_KEYS_MANIFEST = HEAD_LINES + PROVIDES + CONFIG_TABLE + '\r\n[requires]\r\nextensions = []\r\nplugins = []\r\n'


def test_a_manifest_with_both_requires_keys_stops_migrate_before_any_write(tmp_path):
    lay = old_project(tmp_path)
    (lay.root / "extensions" / "alpha.toml").write_bytes(BOTH_KEYS_MANIFEST.encode())
    before = snapshot(lay.root)
    with pytest.raises(migrations.MigrationError, match="alpha.toml"):
        migrations.run(lay)
    assert snapshot(lay.root) == before


def test_a_dry_run_of_the_both_keys_manifest_refuses_rather_than_announcing_the_migration(tmp_path):
    lay = old_project(tmp_path)
    (lay.root / "extensions" / "alpha.toml").write_bytes(BOTH_KEYS_MANIFEST.encode())
    before = snapshot(lay.root)
    with pytest.raises(migrations.MigrationError, match="alpha.toml"):
        migrations.run(lay, dry_run=True)
    assert snapshot(lay.root) == before


# The same guard has to hold for a rewrite the F3 case did not name, not just the one manifest form the review
# demonstrated: whatever made parseable TOML unparsable, fail closed, and never because the input itself was already
# unreadable (that TOMLDecodeError is doctor's to report, not this check's — the two are kept distinct).
def test_a_rewrite_that_breaks_parsing_is_refused_fail_closed():
    before = "a = 1\r\n"
    after = "a = 1\r\na = 2\r\n"                                # a generic duplicate key, nothing to do with plugins
    assert migrations._rewrite_unparsable(before, after)


def test_an_already_invalid_input_is_not_blamed_on_the_rewrite():
    before = "not [ valid toml"
    after = "also not valid, but a different error"
    assert not migrations._rewrite_unparsable(before, after)


# Review 2.0.0 ("Migration safety"): the review could not get a successful apply, a second run or a halfway-failure
# recovery past its own sandbox (docs/releases/2.0.0/EXECUTION_CONTEXT.md §2). Each test below crashes `migrations.
# run` at one of the three points the migration itself is not atomic across — the key rename, the folder move, the
# manifest rewrite — by making exactly the next write raise, then undoes the injected failure and reruns the same
# `Layout` to show the remaining steps complete and `framework_version` is recorded only once everything else is.


def test_a_failure_after_the_key_rename_and_before_the_folder_move_is_completed_by_a_rerun(tmp_path, monkeypatch):
    lay = old_project(tmp_path)
    root = lay.root
    real_rename = Path.rename

    def failing_rename(self, target):
        if self == root / "extensions":
            raise OSError("simulated crash between the key rename and the folder move")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", failing_rename)
    with pytest.raises(OSError, match="simulated crash"):
        migrations.run(lay)
    monkeypatch.undo()

    assert (root / "project.yaml").read_bytes() == PROJECT.replace("extensions:", "plugins:").encode()
    assert (root / "config" / "routing.yaml").read_bytes() == ROUTING.replace("extensions:", "plugins:").encode()
    assert (root / "extensions").is_dir() and not (root / "plugins").exists()

    notes = migrations.run(lay)
    assert "extensions/ moved to plugins/" in notes
    assert any("framework_version" in n for n in notes)
    assert (root / "project.yaml").read_bytes() == PROJECT.replace("1.13.0", migrations.__version__).replace(
        "extensions:", "plugins:").encode()
    assert not (root / "extensions").exists()
    assert (root / "plugins" / "alpha.py").read_bytes() == ALPHA.encode()
    reg = plugins.load(lay)
    assert sorted(reg.loaded) == ["alpha", "beta"] and reg.errors == []
    testing.reset()

    again = migrations.run(lay)
    assert not any("renamed" in n or "moved" in n or "framework_version" in n for n in again)


def test_a_failure_after_the_folder_move_and_before_the_manifest_rewrite_is_completed_by_a_rerun(tmp_path, monkeypatch):
    lay = old_project(tmp_path)
    root = lay.root
    real_rewrite = store.rewrite_text

    def failing_rewrite(path, text):
        if path.suffix == ".toml":
            raise OSError("simulated crash between the folder move and the manifest rewrite")
        return real_rewrite(path, text)

    monkeypatch.setattr(store, "rewrite_text", failing_rewrite)
    with pytest.raises(OSError, match="simulated crash"):
        migrations.run(lay)
    monkeypatch.undo()

    assert not (root / "extensions").exists()
    assert (root / "plugins" / "alpha.toml").read_bytes() == ALPHA_MANIFEST.encode()          # not yet renamed
    assert (root / "plugins" / "beta" / "supervisor-extension.toml").exists()                 # not yet renamed
    assert (root / "project.yaml").read_bytes() == PROJECT.replace("extensions:", "plugins:").encode()

    notes = migrations.run(lay)
    assert any("plugins/alpha.toml" in n for n in notes)
    assert any("framework_version" in n for n in notes)
    assert (root / "plugins" / "alpha.toml").read_bytes() == ALPHA_MANIFEST.replace(
        '"extensions.alpha"', '"plugins.alpha"').replace("extensions = [", "plugins = [").encode()
    assert not (root / "plugins" / "beta" / "supervisor-extension.toml").exists()
    assert (root / "plugins" / "beta" / "supervisor-plugin.toml").read_bytes() == BETA_MANIFEST.encode()
    reg = plugins.load(lay)
    assert sorted(reg.loaded) == ["alpha", "beta"] and reg.errors == []
    testing.reset()

    again = migrations.run(lay)
    assert not any("renamed" in n or "moved" in n or "framework_version" in n for n in again)


def test_a_failure_after_the_manifest_rewrite_and_before_the_version_is_recorded_is_completed_by_a_rerun(
        tmp_path, monkeypatch):
    lay = old_project(tmp_path)
    root = lay.root

    def failing_write(layout, raw):
        raise OSError("simulated crash between the manifest rewrite and the version record")

    monkeypatch.setattr(project, "write", failing_write)
    with pytest.raises(OSError, match="simulated crash"):
        migrations.run(lay)
    monkeypatch.undo()

    assert not (root / "extensions").exists()
    assert (root / "plugins" / "alpha.toml").read_bytes() == ALPHA_MANIFEST.replace(
        '"extensions.alpha"', '"plugins.alpha"').replace("extensions = [", "plugins = [").encode()
    assert (root / "plugins" / "beta" / "supervisor-plugin.toml").read_bytes() == BETA_MANIFEST.encode()
    assert (root / "project.yaml").read_bytes() == PROJECT.replace("extensions:", "plugins:").encode()  # not recorded

    notes = migrations.run(lay)
    assert any("framework_version" in n for n in notes)
    assert not any("renamed" in n or "moved" in n for n in notes)       # already done; only the version is left
    assert (root / "project.yaml").read_bytes() == PROJECT.replace("1.13.0", migrations.__version__).replace(
        "extensions:", "plugins:").encode()
    reg = plugins.load(lay)
    assert sorted(reg.loaded) == ["alpha", "beta"] and reg.errors == []
    testing.reset()

    again = migrations.run(lay)
    assert not any("renamed" in n or "moved" in n or "framework_version" in n for n in again)
