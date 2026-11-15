"""R-09: store.read() prefers libyaml's C loader when the interpreter has it, and runs on the pure-Python one
when it does not — the same documents either way, over task-store and job-record fixtures."""
import pytest
import yaml

from supervisor import store

FIXTURES = [
    "id: T1-01\nphase: 0\ntitle: a task\nhistory:\n- {at: '2026-01-01T00:00:00+00:00', by: loop, from: NOT_STARTED, "
    "to: READY, note: 'a note with unicode: café and \"quotes\"'}\nattempts: []\nartifacts: [a.txt, b/*.json]\n",
    "id: T1-02\nphase: 1\ntitle: \"quoted: title\"\ndepends_on: [T1-01]\nreviewer: []\nchecks: []\n"
    "notes:\n- {at: x, by: owner, note: \"multi\\nline\\ntext\"}\n",
    "{}\n",                                                            # empty-ish document (store.read's `or {}`)
    "a: 1\nb:\n  - 1\n  - 2\n  - {c: 3, d: null}\ne: 3.14\nf: true\n",  # mixed scalar types, the C/Python loaders
                                                                        # disagree about a float's repr but agree
                                                                        # about the parsed value — `==` holds
]


@pytest.fixture
def fixture_paths(tmp_path):
    paths = []
    for i, text in enumerate(FIXTURES):
        p = tmp_path / f"fixture_{i}.yaml"
        p.write_text(text, encoding="utf-8")
        paths.append(p)
    return paths


def test_default_loader_prefers_the_c_loader_when_present():
    class FakeYamlWithC:
        CSafeLoader = yaml.CSafeLoader if hasattr(yaml, "CSafeLoader") else object()
        SafeLoader = yaml.SafeLoader
    if not hasattr(yaml, "CSafeLoader"):
        pytest.skip("libyaml is not installed on this machine")
    assert store._default_loader(FakeYamlWithC) is yaml.CSafeLoader


def test_default_loader_falls_back_without_the_c_loader():
    class FakeYamlWithoutC:
        SafeLoader = yaml.SafeLoader
    assert store._default_loader(FakeYamlWithoutC) is yaml.SafeLoader


@pytest.mark.skipif(not hasattr(yaml, "CSafeLoader"), reason="libyaml is not installed on this machine")
def test_c_and_python_loader_agree_on_every_fixture(fixture_paths):
    for p in fixture_paths:
        text = p.read_text(encoding="utf-8")
        c_doc = yaml.load(text, Loader=yaml.CSafeLoader)
        py_doc = yaml.load(text, Loader=yaml.SafeLoader)
        assert c_doc == py_doc, p.name


def test_store_read_runs_without_the_c_loader(tmp_path, monkeypatch):
    """store.read() must still produce the right document when the interpreter's yaml has no CSafeLoader at
    all (a machine without libyaml) — not merely when CSafeLoader exists but is unused."""
    monkeypatch.setattr(store, "_LOADER", store._default_loader(type("NoC", (), {"SafeLoader": yaml.SafeLoader})))
    p = tmp_path / "doc.yaml"
    p.write_text(FIXTURES[1], encoding="utf-8")
    assert store.read(p) == yaml.safe_load(FIXTURES[1])


def test_store_read_matches_safe_load_on_every_fixture(fixture_paths):
    for p in fixture_paths:
        assert store.read(p) == (yaml.safe_load(p.read_text(encoding="utf-8")) or {})
