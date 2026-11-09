"""tools/catalog_check.py: the validator for plugins-catalog/index.yaml (docs/releases/2.5.0/CONTRIBUTOR_RULING.md
section 2). It fetches nothing and runs no package's code; it refuses what is malformed, never what is unsafe -
that half is the catalog-review skill's. Each in-repo (packages/<distribution>) case is built against a scaffolded
package folder in a disposable git repository, never a real one under this repository's own packages/."""
from __future__ import annotations

import datetime
import importlib.util
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def catalog():
    spec = importlib.util.spec_from_file_location("catalog_check_under_test", REPO / "tools" / "catalog_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def fw(tmp_path) -> Path:
    """A disposable framework checkout with two released versions, v1.0.0 and v2.0.0."""
    root = tmp_path / "fw"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    (root / "README.md").write_text("fw\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "first")
    git(root, "tag", "v1.0.0")
    git(root, "tag", "v2.0.0")
    return root


def add_package(root: Path, identity: str, *, distribution: str | None = None, version: str = "1.0.0",
                framework: str = ">=1.0,<3", licence: str | None = None, tag: bool = True) -> str:
    """A scaffolded packages/<distribution> folder, committed (and, by default, tagged <identity>-vX.Y.Z)."""
    distribution = distribution or f"supervisor-plugin-{identity}"
    folder = root / "packages" / distribution
    pkg = folder / f"supervisor_plugin_{identity}"
    pkg.mkdir(parents=True)
    lic = f'\nlicense = "{licence}"' if licence else ""
    (folder / "pyproject.toml").write_text(f'[project]\nname = "{distribution}"\nversion = "{version}"{lic}\n',
                                           encoding="utf-8")
    (pkg / "supervisor-plugin.toml").write_text(
        f'identity = "{identity}"\nversion = "{version}"\nframework = "{framework}"\n', encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", f"add {identity}")
    if tag:
        git(root, "tag", f"{identity}-v{version}")
    return distribution


def write_index(root: Path, entries: list[dict], *, version=1) -> Path:
    path = root / "plugins-catalog" / "index.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"version": version, "plugins": entries}, sort_keys=False), encoding="utf-8")
    return path


def packages_entry(identity="sound_model", distribution="supervisor-plugin-soundgen", version="1.0.0",
                   framework=">=1.0,<3", licence="Apache-2.0 WITH Commons-Clause", g5_framework="v2.0.0",
                   g5_date=None, artifact_name=None, sha256="0" * 64) -> dict:
    artifact_name = artifact_name or f"{distribution.replace('-', '_')}-{version}-py3-none-any.whl"
    g5_date = g5_date or datetime.date.today().isoformat()      # on or after the scaffolded package's own tag date
    return {
        "identity": identity, "distribution": distribution,
        "summary": "a lane that renders audio from a prompt on a local service",
        "home": f"packages/{distribution}", "version": version, "framework": framework, "licence": licence,
        "tag": f"{identity}-v{version}",
        "artifact": {"name": artifact_name, "sha256": sha256},
        "g5": {"date": g5_date, "framework": g5_framework},
    }


def foreign_entry(identity="text_model", distribution="supervisor-plugin-textgen", version="2.1.0",
                  framework=">=1.0,<3", licence="MIT", g5_framework="v2.0.0", g5_date="2026-01-02",
                  artifact_name=None, sha256="1" * 64) -> dict:
    artifact_name = artifact_name or f"{distribution.replace('-', '_')}-{version}-py3-none-any.whl"
    return {
        "identity": identity, "distribution": distribution,
        "summary": "a lane that completes text on a model server the project runs itself",
        "home": "https://example.org/maintainer/supervisor-plugin-textgen", "version": version,
        "framework": framework, "licence": licence, "tag": f"v{version}",
        "artifact": {"name": artifact_name, "sha256": sha256},
        "g5": {"date": g5_date, "framework": g5_framework},
    }


# ------------------------------------------------------------------------------------- both homes, each accepted


def test_a_packages_home_entry_is_accepted(catalog, fw):
    add_package(fw, "sound_model", distribution="supervisor-plugin-soundgen")
    index = write_index(fw, [packages_entry()])
    assert catalog.check(index, fw) == []


def test_a_repository_home_entry_is_accepted(catalog, fw):
    index = write_index(fw, [foreign_entry()])
    assert catalog.check(index, fw) == []


def test_both_homes_in_one_file_are_each_accepted(catalog, fw):
    add_package(fw, "sound_model", distribution="supervisor-plugin-soundgen")
    index = write_index(fw, [packages_entry(), foreign_entry()])
    assert catalog.check(index, fw) == []


def test_the_real_index_validates(catalog):
    assert catalog.check(catalog.INDEX, catalog.ROOT) == []


def test_the_cli_passes_clean_and_fails_on_a_hit(fw, tmp_path):
    import sys
    good = write_index(fw, [foreign_entry()])
    assert subprocess.run([sys.executable, str(REPO / "tools" / "catalog_check.py"), str(good), "--root",
                           str(fw)]).returncode == 0
    bad = write_index(fw, [{**foreign_entry(), "artifact": {"name": "x.whl"}}])
    assert subprocess.run([sys.executable, str(REPO / "tools" / "catalog_check.py"), str(bad), "--root",
                           str(fw)]).returncode == 1


# ------------------------------------------------------------------------------------- each numbered refusal (2.5)


def test_a_missing_digest_is_refused(catalog, fw):
    entry = foreign_entry()
    del entry["artifact"]["sha256"]
    problems = catalog.check(write_index(fw, [entry]), fw)
    assert any("artifact.sha256" in p for p in problems), problems


def test_an_unknown_key_is_refused(catalog, fw):
    problems = catalog.check(write_index(fw, [{**foreign_entry(), "homepage": "https://example.org"}]), fw)
    assert any("unknown key" in p for p in problems), problems


def test_a_file_without_version_1_is_refused(catalog, fw):
    index = fw / "plugins-catalog" / "index.yaml"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text("version: 2\nplugins: []\n", encoding="utf-8")
    assert catalog.check(index, fw) == ["index: version: must be 1, found 2"]


def test_plugins_not_a_list_is_refused(catalog, fw):
    index = fw / "plugins-catalog" / "index.yaml"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text("version: 1\nplugins: {}\n", encoding="utf-8")
    assert catalog.check(index, fw) == ["index: plugins: must be a list"]


def test_entries_not_sorted_by_identity_are_refused(catalog, fw):
    problems = catalog.check(write_index(fw, [foreign_entry(identity="text_model"),
                                              foreign_entry(identity="alpha_model", distribution="supervisor-plugin-alpha")]),
                             fw)
    assert any("not sorted" in p for p in problems), problems


def test_a_malformed_identity_is_refused(catalog, fw):
    problems = catalog.check(write_index(fw, [foreign_entry(identity="Text-Model")]), fw)
    assert any("identity" in p and "fullmatch" not in p for p in problems), problems


def test_a_duplicate_identity_is_refused(catalog, fw):
    a = foreign_entry(identity="text_model", distribution="supervisor-plugin-textgen")
    b = foreign_entry(identity="text_model", distribution="supervisor-plugin-textgen-two")
    problems = catalog.check(write_index(fw, [a, b]), fw)
    assert any("duplicate identity" in p for p in problems), problems


def test_an_entry_naming_neither_home_is_refused(catalog, fw):
    entry = foreign_entry()
    del entry["home"]
    problems = catalog.check(write_index(fw, [entry]), fw)
    assert any(p.startswith("text_model: home:") for p in problems), problems


@pytest.mark.parametrize("home", ["http://example.org/maintainer/pkg", "C:/local/path", "just-a-name"])
def test_a_malshaped_home_is_refused(catalog, fw, home):
    problems = catalog.check(write_index(fw, [{**foreign_entry(), "home": home}]), fw)
    assert any(p.startswith("text_model: home:") for p in problems), problems


def test_a_packages_home_with_no_such_folder_is_refused(catalog, fw):
    problems = catalog.check(write_index(fw, [packages_entry()]), fw)
    assert any("no such folder" in p for p in problems), problems


def test_a_packages_home_pyproject_name_mismatch_is_refused(catalog, fw):
    add_package(fw, "sound_model", distribution="supervisor-plugin-soundgen")
    (fw / "packages" / "supervisor-plugin-soundgen" / "pyproject.toml").write_text(
        '[project]\nname = "supervisor-plugin-other"\nversion = "1.0.0"\n', encoding="utf-8")
    problems = catalog.check(write_index(fw, [packages_entry()]), fw)
    assert any("[project].name" in p for p in problems), problems


def test_a_shipped_manifest_mismatch_is_refused(catalog, fw):
    add_package(fw, "sound_model", distribution="supervisor-plugin-soundgen", framework=">=1.0,<2")
    problems = catalog.check(write_index(fw, [packages_entry(framework=">=1.0,<3")]), fw)
    assert any("framework range does not match" in p for p in problems), problems


def test_a_licence_mismatch_is_refused(catalog, fw):
    add_package(fw, "sound_model", distribution="supervisor-plugin-soundgen", licence="MIT")
    problems = catalog.check(write_index(fw, [packages_entry(licence="Apache-2.0")]), fw)
    assert any("declared licence" in p for p in problems), problems


def test_a_tag_that_does_not_exist_is_refused(catalog, fw):
    add_package(fw, "sound_model", distribution="supervisor-plugin-soundgen", tag=False)
    problems = catalog.check(write_index(fw, [packages_entry()]), fw)
    assert any(p.startswith("sound_model: tag:") and "exist" in p for p in problems), problems


def test_a_sha256_that_is_not_64_lowercase_hex_is_refused(catalog, fw):
    problems = catalog.check(write_index(fw, [{**foreign_entry(), "artifact": {**foreign_entry()["artifact"],
                                                                                "sha256": "ABC123"}}]), fw)
    assert any("sha256" in p for p in problems), problems


def test_an_artifact_name_with_the_wrong_distribution_is_refused(catalog, fw):
    entry = foreign_entry()
    entry["artifact"]["name"] = "supervisor_plugin_other-2.1.0-py3-none-any.whl"
    problems = catalog.check(write_index(fw, [entry]), fw)
    assert any("distribution does not match" in p for p in problems), problems


def test_an_artifact_name_with_the_wrong_version_is_refused(catalog, fw):
    """The check that catches an entry claiming one version with another version's digest."""
    entry = foreign_entry()
    entry["artifact"]["name"] = "supervisor_plugin_textgen-9.9.9-py3-none-any.whl"
    problems = catalog.check(write_index(fw, [entry]), fw)
    assert any("version does not match" in p for p in problems), problems


def test_a_wrong_shaped_tag_is_refused(catalog, fw):
    problems = catalog.check(write_index(fw, [{**foreign_entry(), "tag": "2.1.0"}]), fw)
    assert any(p.startswith("text_model: tag:") for p in problems), problems


@pytest.mark.parametrize("date", ["not-a-date", "2099-01-01"])
def test_a_bad_g5_date_is_refused(catalog, fw, date):
    problems = catalog.check(write_index(fw, [{**foreign_entry(), "g5": {**foreign_entry()["g5"], "date": date}}]),
                             fw)
    assert any("g5.date" in p for p in problems), problems


def test_a_g5_date_before_its_own_tag_is_refused(catalog, fw):
    add_package(fw, "sound_model", distribution="supervisor-plugin-soundgen")
    problems = catalog.check(write_index(fw, [packages_entry(g5_date="2000-01-01")]), fw)
    assert any("precedes its own tag" in p for p in problems), problems


def test_a_g5_framework_that_is_not_released_is_refused(catalog, fw):
    problems = catalog.check(write_index(fw, [{**foreign_entry(), "g5": {**foreign_entry()["g5"],
                                                                           "framework": "v9.0.0"}}]), fw)
    assert any("g5.framework" in p for p in problems), problems


def test_an_unsatisfiable_framework_range_is_refused(catalog, fw):
    problems = catalog.check(write_index(fw, [foreign_entry(framework=">=9.0,<10")]), fw)
    assert any(p.startswith("text_model: framework:") for p in problems), problems


@pytest.mark.parametrize("summary", ["", "x" * 121, "two\nlines", "see https://example.org/docs"])
def test_a_bad_summary_is_refused(catalog, fw, summary):
    problems = catalog.check(write_index(fw, [{**foreign_entry(), "summary": summary}]), fw)
    assert any("summary" in p for p in problems), problems
