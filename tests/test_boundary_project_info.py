"""Gate G1d (supervisor.boundary): no project information anywhere in the framework repository. The owner's rule
(2026-10-28): the framework repo never contains any project's files or information. A project's words are listed in
a local file outside the repository (`SUPERVISOR_PROJECT_TERMS`, else ~/.config/supervisor/project-terms.toml),
never in boundary.toml, so the list itself is not project information in the repo. Every tracked text file is
scanned, not only supervisor/, and a hit is a violation. Without the local file (CI) the rule reports that it did not
run, and CI keeps the generic rules G1a to G1c.

The words here are invented: a real project's word in this file would itself be a violation."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from supervisor import boundary

CONFIG = """
[imports]
allowed = []
dynamic_loaders = []
[literals]
allowed_url_hosts = []
user_names = []
[vendor]
terms = []
files = []
"""

_REAL_ENV = dict(os.environ)                   # read at import, before the autouse fixture replaces it

TERMS = """
[projects.zorblax]
terms = ["zorblax", "quuxify", "Wobble Works"]
"""


@pytest.fixture(autouse=True)
def _no_real_terms(tmp_path, monkeypatch):
    monkeypatch.setenv(boundary.PROJECT_TERMS_ENV, str(tmp_path / "no-such-terms.toml"))


def framework(tmp_path, files: dict[str, str], config: str = CONFIG, git: bool = True) -> Path:
    root = tmp_path / "fw"
    (root / "supervisor").mkdir(parents=True)
    (root / "supervisor" / "core.py").write_text("x = 1\n", encoding="utf-8")
    (root / "boundary.toml").write_text(config, encoding="utf-8")
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    if git:
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    return root


def terms_file(tmp_path, monkeypatch, text: str = TERMS) -> Path:
    p = tmp_path / "project-terms.toml"
    p.write_text(text, encoding="utf-8")
    monkeypatch.setenv(boundary.PROJECT_TERMS_ENV, str(p))
    return p


def g1d(report) -> list[tuple[str, int, str]]:
    return sorted((f.path, f.line, f.text) for f in report.violations if f.rule == "G1d")


def test_a_project_word_anywhere_in_the_repository_is_a_violation(tmp_path, monkeypatch):
    terms_file(tmp_path, monkeypatch)
    root = framework(tmp_path, {
        "docs/GUIDE.md": "# Guide\nThe zorblax lane is an example.\n",
        "tests/test_x.py": "def test_x():\n    assert quuxify()  # noqa\n",
        "CHANGELOG.md": "## 1.0\n- fixed for Wobble Works\n",
        "supervisor/mod.py": "# nothing here\n",
    })
    report = boundary.check(root, user_names=[])
    assert not report.ok
    assert g1d(report) == [("CHANGELOG.md", 2, "Wobble Works"), ("docs/GUIDE.md", 2, "zorblax"),
                           ("tests/test_x.py", 2, "quuxify")]


def test_words_match_whole_words_only(tmp_path, monkeypatch):
    terms_file(tmp_path, monkeypatch, '[projects.p]\nterms = ["blorp"]\n')
    root = framework(tmp_path, {"docs/A.md": "Ask blorpishly, not by blorps_list or xblorp.\n"})
    assert g1d(boundary.check(root, user_names=[])) == []


def test_ignored_files_are_not_scanned(tmp_path, monkeypatch):
    """A contributor's scratch file in an ignored folder never reaches the repository."""
    terms_file(tmp_path, monkeypatch)
    root = framework(tmp_path, {".gitignore": "runs/\n"})
    (root / "runs").mkdir()
    (root / "runs" / "notes.txt").write_text("zorblax\n", encoding="utf-8")
    assert g1d(boundary.check(root, user_names=[])) == []


def test_a_new_file_not_yet_added_is_scanned(tmp_path, monkeypatch):
    """1.13.0, a skill-trial finding (boundary-review, round 2): G1d read only what git tracks, so a new file in
    uncommitted work was never checked, and a clean G1d says nothing. Untracked files that are not ignored count
    too: they are what the next commit adds."""
    terms_file(tmp_path, monkeypatch)
    root = framework(tmp_path, {})
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "NEW.md").write_text("A zorblax example.\n", encoding="utf-8")
    assert g1d(boundary.check(root, user_names=[])) == [("docs/NEW.md", 1, "zorblax")]


def test_without_git_every_file_but_caches_and_runs_is_scanned(tmp_path, monkeypatch):
    terms_file(tmp_path, monkeypatch)
    root = framework(tmp_path, {"docs/A.md": "zorblax\n"}, git=False)
    (root / "runs").mkdir()
    (root / "runs" / "log.txt").write_text("zorblax\n", encoding="utf-8")
    assert g1d(boundary.check(root, user_names=[])) == [("docs/A.md", 1, "zorblax")]


def test_project_words_in_boundary_toml_are_a_violation(tmp_path):
    """The old [projects.<name>] list put a project's vocabulary into the framework repo itself."""
    root = framework(tmp_path, {}, config=CONFIG + '[projects.zorblax]\nterms = ["zorblax"]\n')
    report = boundary.check(root, user_names=[])
    assert [(f.rule, f.path) for f in report.violations] == [("G1d", "boundary.toml")]
    assert "project-terms.toml" in report.violations[0].detail or boundary.PROJECT_TERMS_ENV in \
        report.violations[0].detail


def test_without_the_local_file_g1d_says_it_did_not_run(tmp_path):
    root = framework(tmp_path, {"docs/A.md": "zorblax\n"})
    report = boundary.check(root, user_names=[])
    assert report.ok and report.notes == []
    assert report.skipped and "G1d" in report.skipped[0]


def test_an_invalid_local_file_stops_the_check(tmp_path, monkeypatch):
    terms_file(tmp_path, monkeypatch, "[projects.p\n")
    with pytest.raises(boundary.ConfigError, match="project-terms"):
        boundary.check(framework(tmp_path, {}), user_names=[])


def test_the_cli_prints_g1d_hits_and_fails(tmp_path, monkeypatch):
    terms_file(tmp_path, monkeypatch)
    root = framework(tmp_path, {"docs/A.md": "zorblax\n"})
    out = []
    assert boundary.run(root, echo=out.append) == 1
    assert any("docs/A.md:1: G1d" in line for line in out)


def test_the_framework_repository_holds_no_project_information():
    """Runs against the contributor's own local word list; in CI there is none, and the generic rules run instead."""
    terms = boundary.load_project_terms(boundary.project_terms_path(_REAL_ENV))
    if not terms:
        pytest.skip("no local project-terms file: G1d runs on a contributor's machine, not in CI")
    report = boundary.check(Path(__file__).resolve().parents[1], user_names=[], terms=terms)
    assert g1d(report) == [], "\n".join(str(f) for f in report.violations if f.rule == "G1d")


# ---------------------------------------------------------------- the project-words scan for any repository (2.0.0)
# A plugin package's repository (or its folder under packages/) and its built wheel must hold no project
# information either: `python -m supervisor.boundary --words <target>` runs G1d's word list over any folder or built
# archive, file names included. A release check that cannot run fails instead of passing.

def package(tmp_path, files: dict[str, str], git: bool = True) -> Path:
    root = tmp_path / "pkg"
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    if git:
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    return root


def hits(findings) -> list[tuple[str, int, str]]:
    return sorted((f.path, f.line, f.text) for f in findings)


def test_any_folder_is_scanned_contents_and_file_names(tmp_path, monkeypatch):
    root = package(tmp_path, {
        ".gitignore": "build/\n",
        "README.md": "# A lane\nBuilt for Wobble Works.\n",
        "src/supervisor_plugin_quuxify/__init__.py": "x = 1\n",
        "src/supervisor_plugin_quuxify/lane.py": "NAME = 'neutral'\n",
        "build/notes.txt": "zorblax\n",
    })
    terms = boundary.load_project_terms(terms_file(tmp_path, monkeypatch))
    assert hits(boundary.scan_words(root, terms)) == [
        ("README.md", 2, "Wobble Works"),
        ("src/supervisor_plugin_quuxify/__init__.py", 0, "quuxify"),
        ("src/supervisor_plugin_quuxify/lane.py", 0, "quuxify"),
    ]


def test_a_folder_inside_a_repository_is_scanned_alone(tmp_path, monkeypatch):
    root = package(tmp_path, {"packages/one/README.md": "neutral\n", "packages/two/README.md": "zorblax\n"})
    terms = boundary.load_project_terms(terms_file(tmp_path, monkeypatch))
    assert hits(boundary.scan_words(root / "packages" / "one", terms)) == []
    assert hits(boundary.scan_words(root / "packages" / "two", terms)) == [("README.md", 1, "zorblax")]


def test_a_built_wheel_is_scanned_metadata_and_member_names_included(tmp_path, monkeypatch):
    import zipfile
    wheel = tmp_path / "supervisor_plugin_neutral-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as z:
        z.writestr("supervisor_plugin_neutral/__init__.py", "x = 1\n")
        z.writestr("supervisor_plugin_neutral/zorblax.json", "{}\n")
        z.writestr("supervisor_plugin_neutral-1.0.0.dist-info/METADATA", "Name: neutral\nSummary: for Wobble Works\n")
        z.writestr("supervisor_plugin_neutral/icon.png", bytes([0x89, 0x50, 0xff, 0xfe, 0x00]) + b"quuxify")
    terms = boundary.load_project_terms(terms_file(tmp_path, monkeypatch))
    assert hits(boundary.scan_words(wheel, terms)) == [
        (f"{wheel.name}!supervisor_plugin_neutral-1.0.0.dist-info/METADATA", 2, "Wobble Works"),
        (f"{wheel.name}!supervisor_plugin_neutral/zorblax.json", 0, "zorblax"),
    ]


def test_a_source_archive_is_scanned(tmp_path, monkeypatch):
    import io
    import tarfile
    sdist = tmp_path / "neutral-1.0.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as t:
        data = b"Summary: quuxify everything\n"
        info = tarfile.TarInfo("neutral-1.0.0/PKG-INFO")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    terms = boundary.load_project_terms(terms_file(tmp_path, monkeypatch))
    assert hits(boundary.scan_words(sdist, terms)) == [(f"{sdist.name}!neutral-1.0.0/PKG-INFO", 1, "quuxify")]


def test_the_words_cli_fails_on_a_hit_passes_clean_and_never_passes_unrun(tmp_path, monkeypatch):
    dirty = package(tmp_path, {"README.md": "zorblax\n"})
    clean = tmp_path / "clean"
    (clean / "src").mkdir(parents=True)
    (clean / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    out = []
    assert boundary.words([clean, tmp_path / "missing"], echo=out.append) == 2       # no word list: not a pass
    assert any("project-terms" in line for line in out)
    terms_file(tmp_path, monkeypatch)
    out.clear()
    assert boundary.words([clean], echo=out.append) == 0 and "OK" in out[-1]
    out.clear()
    assert boundary.words([clean, dirty], echo=out.append) == 1
    assert any("README.md:1: G1d" in line for line in out)
    assert boundary.words([tmp_path / "missing"], echo=out.append) == 2
    assert boundary.main(["--words", str(dirty)]) == 1


def test_g1d_also_reads_file_names_in_the_framework_repository(tmp_path, monkeypatch):
    terms_file(tmp_path, monkeypatch)
    root = framework(tmp_path, {"docs/zorblax_notes.md": "neutral\n"})
    assert g1d(boundary.check(root, user_names=[])) == [("docs/zorblax_notes.md", 0, "zorblax")]


def test_a_folder_an_enclosing_repository_ignores_is_still_scanned(tmp_path, monkeypatch):
    """git lists nothing there: an empty list must not read as a clean package."""
    root = package(tmp_path, {".gitignore": "runs/\n"})
    unpacked = root / "runs" / "unpacked"
    unpacked.mkdir(parents=True)
    (unpacked / "README.md").write_text("zorblax\n", encoding="utf-8")
    terms = boundary.load_project_terms(terms_file(tmp_path, monkeypatch))
    assert hits(boundary.scan_words(unpacked, terms)) == [("README.md", 1, "zorblax")]


# ---------------------------------------------------------------- a package's own name (2.0.0)
# A project word can become the public name of a plugin package kept under packages/ (an identity the project's
# saved state already uses). The local file may allow it there, and only there: inside packages/<distribution>/ (or
# that folder scanned on its own) and in that distribution's wheel or sdist. Everywhere else it is still a hit.

ALLOWING = TERMS + """
[projects.zorblax.packages]
supervisor-plugin-quuxify = ["quuxify"]
"""


def test_a_package_may_carry_its_own_name_and_nothing_else_may(tmp_path, monkeypatch):
    terms_file(tmp_path, monkeypatch, ALLOWING)
    root = framework(tmp_path, {
        "packages/supervisor-plugin-quuxify/README.md": "The quuxify lane, built for zorblax.\n",
        "packages/supervisor-plugin-quuxify/supervisor_plugin_quuxify/__init__.py": "NAME = 'quuxify'\n",
        "packages/supervisor-plugin-other/README.md": "Not the quuxify package.\n",
        "examples/supervisor-plugin-quuxify/README.md": "neutral\n",
        "docs/PACKAGES.md": "See supervisor-plugin-quuxify.\n",
    })
    assert g1d(boundary.check(root, user_names=[])) == [
        ("docs/PACKAGES.md", 1, "quuxify"),
        ("examples/supervisor-plugin-quuxify/README.md", 0, "quuxify"),
        ("packages/supervisor-plugin-other/README.md", 1, "quuxify"),
        ("packages/supervisor-plugin-quuxify/README.md", 1, "zorblax"),        # another word: still a hit
    ]


def test_the_allowance_holds_for_the_package_folder_scanned_alone_or_from_the_root(tmp_path, monkeypatch):
    terms_file(tmp_path, monkeypatch, ALLOWING)
    root = package(tmp_path, {"packages/supervisor-plugin-quuxify/supervisor_plugin_quuxify/lane.py": "X = 'quuxify'\n",
                              "docs/A.md": "quuxify\n"})
    terms = boundary.load_project_terms()
    allowed = boundary.load_package_allowances()
    assert hits(boundary.scan_words(root / "packages" / "supervisor-plugin-quuxify", terms, allowed=allowed)) == []
    assert hits(boundary.scan_words(root, terms, allowed=allowed)) == [("docs/A.md", 1, "quuxify")]
    assert hits(boundary.scan_words(root / "packages" / "supervisor-plugin-quuxify", terms)) != []   # none given


def test_the_allowance_holds_for_that_distributions_archives_only(tmp_path, monkeypatch):
    import io
    import tarfile
    import zipfile
    terms_file(tmp_path, monkeypatch, ALLOWING)
    terms, allowed = boundary.load_project_terms(), boundary.load_package_allowances()
    own = tmp_path / "supervisor_plugin_quuxify-1.0.0-py3-none-any.whl"
    other = tmp_path / "supervisor_plugin_other-1.0.0-py3-none-any.whl"
    for wheel, dist in ((own, "supervisor-plugin-quuxify"), (other, "supervisor-plugin-other")):
        with zipfile.ZipFile(wheel, "w") as z:
            z.writestr("supervisor_plugin_quuxify/__init__.py", "NAME = 'quuxify'  # zorblax\n")
            z.writestr(f"{wheel.name.split('-')[0]}-1.0.0.dist-info/METADATA", f"Name: {dist}\nVersion: 1.0.0\n")
    assert hits(boundary.scan_words(own, terms, allowed=allowed)) == [
        (f"{own.name}!supervisor_plugin_quuxify/__init__.py", 1, "zorblax")]
    assert len(hits(boundary.scan_words(other, terms, allowed=allowed))) == 3
    sdist = tmp_path / "supervisor-plugin-quuxify-1.0.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as t:
        data = b"Name: supervisor-plugin-quuxify\nSummary: the quuxify lane\n"
        info = tarfile.TarInfo("supervisor-plugin-quuxify-1.0.0/PKG-INFO")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    assert hits(boundary.scan_words(sdist, terms, allowed=allowed)) == []


def test_the_words_cli_reads_the_allowance_from_the_local_file(tmp_path, monkeypatch):
    terms_file(tmp_path, monkeypatch, ALLOWING)
    root = package(tmp_path, {"packages/supervisor-plugin-quuxify/README.md": "the quuxify lane\n"})
    assert boundary.words([root / "packages" / "supervisor-plugin-quuxify"], echo=lambda _l: None) == 0


BASE_TERMS = '[projects.zorblax]\nterms = ["zorblax", "quuxify"]\n'


@pytest.mark.parametrize("text", [
    BASE_TERMS + '[projects.zorblax.packages]\nsupervisor-plugin-quuxify = "quuxify"\n',        # not a list
    '[projects.zorblax]\nterms = ["quuxify"]\npackages = ["supervisor-plugin-quuxify"]\n',      # not a table
    BASE_TERMS + '[projects.zorblax.packages]\nsupervisor-plugin-quuxify = ["blorp"]\n',       # not the project's word
])
def test_a_malformed_allowance_stops_the_check(tmp_path, monkeypatch, text):
    terms_file(tmp_path, monkeypatch, text)
    with pytest.raises(boundary.ConfigError, match="packages"):
        boundary.load_package_allowances()


# ---------------------------------------------------------------- review 2.0.0 R1: the allowance stays in its package
# It matched any packages/<distribution> pair at any depth, any folder named after the distribution, and any archive
# whose file name began like it. It now holds for packages/<distribution>/ at the top of the git repository the files
# are in, and for an archive whose wheel or sdist file name AND its own metadata name that distribution.

def test_a_lookalike_path_gets_no_allowance(tmp_path, monkeypatch):
    terms_file(tmp_path, monkeypatch, ALLOWING)
    root = framework(tmp_path, {
        "docs/packages/supervisor-plugin-quuxify/README.md": "quuxify\n",
        "examples/supervisor-plugin-quuxify/README.md": "quuxify\n",
        "packages/supervisor-plugin-quuxify/README.md": "quuxify\n",
    })
    found = g1d(boundary.check(root, user_names=[]))
    assert ("docs/packages/supervisor-plugin-quuxify/README.md", 1, "quuxify") in found
    assert ("examples/supervisor-plugin-quuxify/README.md", 1, "quuxify") in found
    assert not any(path.startswith("packages/") for path, _l, _w in found)


def test_a_folder_named_like_the_package_outside_its_place_gets_no_allowance(tmp_path, monkeypatch):
    terms_file(tmp_path, monkeypatch, ALLOWING)
    terms, allowed = boundary.load_project_terms(), boundary.load_package_allowances()
    root = package(tmp_path, {"elsewhere/supervisor-plugin-quuxify/README.md": "quuxify\n",
                              "docs/packages/supervisor-plugin-quuxify/README.md": "quuxify\n"})
    for folder in (root / "elsewhere" / "supervisor-plugin-quuxify",
                   root / "docs" / "packages" / "supervisor-plugin-quuxify"):
        assert hits(boundary.scan_words(folder, terms, allowed=allowed)) == [("README.md", 1, "quuxify")], folder
    loose = tmp_path / "loose" / "packages" / "supervisor-plugin-quuxify"           # no git repository at all
    loose.mkdir(parents=True)
    (loose / "README.md").write_text("quuxify\n", encoding="utf-8")
    assert hits(boundary.scan_words(loose, terms, allowed=allowed)) == [("README.md", 1, "quuxify")]


BANG_ALLOWING = """
[projects.demo]
terms = ["cedar"]
[projects.demo.packages]
supervisor-plugin-cedar = ["cedar"]
"""


def test_a_bang_in_an_ordinary_folder_name_gets_no_allowance(tmp_path, monkeypatch):
    """Review 2.0.0 round 2, F2: `!` is legal in an ordinary directory name, so `outside!packages/...` must not
    read as an archive member inside packages/ and borrow that package's allowance. Same hits with or without it."""
    terms_file(tmp_path, monkeypatch, BANG_ALLOWING)
    root = package(tmp_path, {"outside!packages/supervisor-plugin-cedar/notes.txt": "cedar\n"})
    terms = boundary.load_project_terms()
    plain = hits(boundary.scan_words(root, terms))
    allowed = hits(boundary.scan_words(root, terms, allowed=boundary.load_package_allowances()))
    want = [("outside!packages/supervisor-plugin-cedar/notes.txt", 0, "cedar"),
            ("outside!packages/supervisor-plugin-cedar/notes.txt", 1, "cedar")]
    assert plain == want
    assert allowed == want


def test_the_g1d_entrypoint_also_refuses_the_bang_lookalike(tmp_path, monkeypatch):
    """The same case through the framework-repository scan, alongside a real packages/<distribution> member, which
    must keep its allowance."""
    terms_file(tmp_path, monkeypatch, BANG_ALLOWING)
    root = framework(tmp_path, {
        "outside!packages/supervisor-plugin-cedar/notes.txt": "cedar\n",
        "packages/supervisor-plugin-cedar/README.md": "cedar\n",
    })
    found = g1d(boundary.check(root, user_names=[]))
    assert ("outside!packages/supervisor-plugin-cedar/notes.txt", 0, "cedar") in found
    assert ("outside!packages/supervisor-plugin-cedar/notes.txt", 1, "cedar") in found
    assert not any(path.startswith("packages/") for path, _l, _w in found)


def test_the_words_entrypoint_also_refuses_the_bang_lookalike(tmp_path, monkeypatch):
    terms_file(tmp_path, monkeypatch, BANG_ALLOWING)
    root = package(tmp_path, {"outside!packages/supervisor-plugin-cedar/notes.txt": "cedar\n"})
    out = []
    assert boundary.words([root], echo=out.append) == 1
    assert any("outside!packages/supervisor-plugin-cedar/notes.txt" in line for line in out)


def test_a_lookalike_archive_gets_no_allowance(tmp_path, monkeypatch):
    import zipfile
    terms_file(tmp_path, monkeypatch, ALLOWING)
    terms, allowed = boundary.load_project_terms(), boundary.load_package_allowances()
    member = "supervisor_plugin_quuxify/__init__.py"
    cases = {
        "supervisor-plugin-quuxify-2-extra.zip": None,                         # not a wheel or sdist name
        "supervisor_plugin_quuxify-1.0.0-py3-none-any.whl": "supervisor-plugin-other",    # metadata says other
    }
    for name, meta in cases.items():
        archive = tmp_path / name
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr(member, "NAME = 'quuxify'\n")
            if meta:
                z.writestr("supervisor_plugin_quuxify-1.0.0.dist-info/METADATA", f"Name: {meta}\n")
        assert (f"{name}!{member}", 1, "quuxify") in hits(boundary.scan_words(archive, terms, allowed=allowed)), name
    nameless = tmp_path / "nameless" / "supervisor_plugin_quuxify-1.0.0-py3-none-any.whl"   # no metadata at all
    nameless.parent.mkdir()
    with zipfile.ZipFile(nameless, "w") as z:
        z.writestr(member, "NAME = 'quuxify'\n")
    assert (f"{nameless.name}!{member}", 1, "quuxify") in hits(boundary.scan_words(nameless, terms, allowed=allowed))


# ---------------------------------------------------------------- review 2.0.0 round 2, F4: tar link members
# The tar branch emitted only info.isfile() members: a symbolic- or hard-link member's name, and the target it
# points at, were never scanned, though the published archive exposes that path. The archives below point their
# links at a member absent from the archive, so a scan that followed or extracted the link would raise, not report
# zero or one hit; a directory member must still report nothing of its own.

def test_a_tar_symlink_members_name_is_scanned(tmp_path, monkeypatch):
    import tarfile
    sdist = tmp_path / "neutral-1.0.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as t:
        info = tarfile.TarInfo("neutral-1.0.0/quuxify-link")
        info.type = tarfile.SYMTYPE
        info.linkname = "target.txt"
        t.addfile(info)
    terms = boundary.load_project_terms(terms_file(tmp_path, monkeypatch))
    assert hits(boundary.scan_words(sdist, terms)) == [(f"{sdist.name}!neutral-1.0.0/quuxify-link", 0, "quuxify")]


def test_a_tar_hardlink_members_name_is_scanned(tmp_path, monkeypatch):
    import tarfile
    sdist = tmp_path / "neutral-1.0.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as t:
        info = tarfile.TarInfo("neutral-1.0.0/zorblax-hardlink")
        info.type = tarfile.LNKTYPE
        info.linkname = "neutral-1.0.0/target.txt"
        t.addfile(info)
    terms = boundary.load_project_terms(terms_file(tmp_path, monkeypatch))
    assert hits(boundary.scan_words(sdist, terms)) == [(f"{sdist.name}!neutral-1.0.0/zorblax-hardlink", 0, "zorblax")]


def test_a_tar_symlinks_target_is_scanned(tmp_path, monkeypatch):
    import tarfile
    sdist = tmp_path / "neutral-1.0.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as t:
        info = tarfile.TarInfo("neutral-1.0.0/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "quuxify.txt"
        t.addfile(info)
    terms = boundary.load_project_terms(terms_file(tmp_path, monkeypatch))
    assert hits(boundary.scan_words(sdist, terms)) == [(f"{sdist.name}!neutral-1.0.0/link", 1, "quuxify")]


def test_a_tar_directory_member_reports_nothing_of_its_own(tmp_path, monkeypatch):
    import tarfile
    sdist = tmp_path / "neutral-1.0.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as t:
        info = tarfile.TarInfo("neutral-1.0.0/quuxify")
        info.type = tarfile.DIRTYPE
        t.addfile(info)
    terms = boundary.load_project_terms(terms_file(tmp_path, monkeypatch))
    assert hits(boundary.scan_words(sdist, terms)) == []
