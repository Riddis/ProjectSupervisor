"""The Ollama doctor check (the tag is part of the model, the store's drive is a project's own optional rule).
Ported from a project-local extension's tests, onto supervisor.plugin and supervisor.testing only - no framework
internal: every test here calls this package's own functions directly."""
from __future__ import annotations

import pytest
import requests

import supervisor_plugin_local_llm as llm
from supervisor_plugin_local_llm.config import Settings


class _AlwaysThere:
    """Stands in for a manifest path that must be found, without claiming any real drive exists on disk."""

    def exists(self):
        return True


def serve_tags(monkeypatch, names):
    class Resp:
        def json(self):
            return {"models": [{"name": n} for n in names]}

        def raise_for_status(self):
            pass
    monkeypatch.setattr(llm.requests, "get", lambda url, timeout=None: Resp())


def fake_store(layout, monkeypatch, *, model="pixnote:4b", layout_v1=True, layout_v2=False,
               source=("process environment", True)):
    """A real manifest tree under `layout.root` and `llm._resolve_models_dir` wired to report it, so the
    location/manifest checks run for real instead of touching the actual Windows registry or `OLLAMA_MODELS`."""
    store = layout.root / "models" / "ollama"
    name, _, tag = model.partition(":")
    tag = tag or "latest"
    if layout_v1:
        (store / "manifests" / "registry.ollama.ai" / "library" / name).mkdir(parents=True, exist_ok=True)
        (store / "manifests" / "registry.ollama.ai" / "library" / name / tag).write_text("{}", encoding="utf-8")
    if layout_v2:
        (store / "manifests-v2" / "ollama.com" / "library" / name).mkdir(parents=True, exist_ok=True)
        (store / "manifests-v2" / "ollama.com" / "library" / name / tag).write_text("{}", encoding="utf-8")
    label, present = source
    monkeypatch.setattr(llm, "_resolve_models_dir", lambda lay: (str(store) if present else None, label))
    return store


def test_settings_default_to_no_model_and_no_drive():
    settings = Settings()
    assert settings.model is None
    assert settings.models_store_drive is None


def test_models_dir_is_declared_in_settings_with_the_root_relative_default(layout):
    """F6 (RELEASE_REVIEW_packages_1.0.0.md): __init__.py filled models_dir in with cfg.setdefault() alone, so
    Settings - the one place a reader checks for every key this package fills in - did not list it. Declaring the
    field changes nothing a project sees: unset, local_llm_config() still resolves it to the same root-relative
    path under the project root."""
    assert "models_dir" in Settings.model_fields
    cfg = llm.local_llm_config(layout)
    assert cfg["models_dir"] == str(layout.root / "models" / "ollama")


def test_local_llm_config_resolves_to_no_model_and_no_drive_on_a_real_project(layout):
    cfg = llm.local_llm_config(layout)
    assert cfg["model"] is None
    assert cfg.get("models_store_drive") is None


def test_ollama_requires_the_configured_tag(layout, monkeypatch):
    monkeypatch.setattr(llm, "local_llm_config", lambda lay: {"base_url": "http://x", "model": "pixnote:4b", "models_dir": "E:\\x"})
    fake_store(layout, monkeypatch, model="pixnote:4b")
    serve_tags(monkeypatch, ["pixnote:8b"])                  # same family, wrong tag: the adapter would get a 404
    check = llm.check_ollama(layout)
    assert not check.ok and "pixnote:4b not pulled" in check.detail
    serve_tags(monkeypatch, ["pixnote:8b", "pixnote:4b"])
    assert llm.check_ollama(layout).ok


def test_ollama_accepts_only_the_latest_normalisation(layout, monkeypatch):
    monkeypatch.setattr(llm, "local_llm_config", lambda lay: {"base_url": "http://x", "model": "glance", "models_dir": "E:\\x"})
    fake_store(layout, monkeypatch, model="glance:latest")
    serve_tags(monkeypatch, ["glance:latest"])                # Ollama's implicit tag for an untagged pull
    assert llm.check_ollama(layout).ok
    serve_tags(monkeypatch, ["glance:13b"])
    assert not llm.check_ollama(layout).ok


def test_ollama_fails_cleanly_when_unreachable(layout, monkeypatch):
    monkeypatch.setattr(llm, "local_llm_config", lambda lay: {"base_url": "http://x", "model": "pixnote:4b", "models_dir": "E:\\x"})
    monkeypatch.setattr(llm.requests, "get", lambda url, timeout=None: (_ for _ in ()).throw(requests.ConnectionError("down")))
    check = llm.check_ollama(layout)
    assert not check.ok and "unreachable" in check.detail


def test_ollama_fails_when_models_dir_is_unset_everywhere(layout, monkeypatch):
    monkeypatch.setattr(llm, "local_llm_config", lambda lay: {"base_url": "http://x", "model": "pixnote:4b", "models_dir": "E:\\expected"})
    serve_tags(monkeypatch, ["pixnote:4b"])
    monkeypatch.setattr(llm, "_resolve_models_dir", lambda lay: (None, "unset"))
    check = llm.check_ollama(layout)
    assert not check.ok and "not set" in check.detail and "E:\\expected" in check.detail


def test_ollama_fails_when_the_store_is_off_the_configured_drive(layout, monkeypatch):
    monkeypatch.setattr(llm, "local_llm_config", lambda lay: {"base_url": "http://x", "model": "pixnote:4b",
                                                               "models_dir": "E:\\expected", "models_store_drive": "Q:"})
    serve_tags(monkeypatch, ["pixnote:4b"])
    monkeypatch.setattr(llm, "_resolve_models_dir",
                        lambda lay: (r"C:\Users\someone\.ollama\models", "User registry"))
    check = llm.check_ollama(layout)
    assert not check.ok and "not on Q:" in check.detail


def test_ollama_skips_the_drive_check_when_models_store_drive_is_unset(layout, monkeypatch):
    """No project sets `models_store_drive` by default - the rule is a project's own choice, not this package's,
    so a store reported on a drive nobody configured as expected never fails doctor on that account."""
    monkeypatch.setattr(llm, "local_llm_config", lambda lay: {"base_url": "http://x", "model": "pixnote:4b",
                                                               "models_dir": "C:\\expected"})
    serve_tags(monkeypatch, ["pixnote:4b"])
    monkeypatch.setattr(llm, "_resolve_models_dir", lambda lay: (r"Z:\off-drive\ollama", "User registry"))
    monkeypatch.setattr(llm, "_manifest_paths", lambda store, alias: [_AlwaysThere()])
    check = llm.check_ollama(layout)
    assert check.ok and "store at" in check.detail


def test_ollama_fails_when_no_model_is_configured(layout, monkeypatch):
    monkeypatch.setattr(llm, "local_llm_config", lambda lay: {"base_url": "http://x", "model": None, "models_dir": "E:\\x"})
    check = llm.check_ollama(layout)
    assert not check.ok and "no model configured" in check.detail


def test_ollama_fails_when_the_manifest_is_missing_from_the_configured_store(layout, monkeypatch):
    monkeypatch.setattr(llm, "local_llm_config", lambda lay: {"base_url": "http://x", "model": "pixnote:4b", "models_dir": "E:\\x"})
    serve_tags(monkeypatch, ["pixnote:4b"])
    fake_store(layout, monkeypatch, model="pixnote:4b", layout_v1=False, layout_v2=False)   # dir reported, nothing under it
    check = llm.check_ollama(layout)
    assert not check.ok and "manifest" in check.detail and "not found" in check.detail


def test_ollama_accepts_either_manifest_layout(layout, monkeypatch):
    monkeypatch.setattr(llm, "local_llm_config", lambda lay: {"base_url": "http://x", "model": "pixnote:4b", "models_dir": "E:\\x"})
    serve_tags(monkeypatch, ["pixnote:4b"])
    fake_store(layout, monkeypatch, model="pixnote:4b", layout_v1=False, layout_v2=True)   # only the newer layout present
    assert llm.check_ollama(layout).ok


def test_ollama_resolves_models_dir_from_registry_when_process_env_is_unset(layout, monkeypatch):
    """The serving process may predate OLLAMA_MODELS existing at all: doctor falls back to the persisted
    User/Machine registry value rather than trusting only its own process environment."""
    monkeypatch.setattr(llm, "local_llm_config", lambda lay: {"base_url": "http://x", "model": "pixnote:4b", "models_dir": "E:\\x"})
    serve_tags(monkeypatch, ["pixnote:4b"])
    store = layout.root / "models" / "ollama"
    (store / "manifests" / "registry.ollama.ai" / "library" / "pixnote").mkdir(parents=True, exist_ok=True)
    (store / "manifests" / "registry.ollama.ai" / "library" / "pixnote" / "4b").write_text("{}", encoding="utf-8")
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)
    monkeypatch.setattr(llm, "_registry_env", lambda name: (str(store), "User"))
    check = llm.check_ollama(layout)
    assert check.ok and "User registry" in check.detail


def test_resolve_models_dir_prefers_process_env_over_registry(monkeypatch, layout):
    monkeypatch.setenv("OLLAMA_MODELS", r"E:\from-env")
    monkeypatch.setattr(llm, "_registry_env", lambda name: (r"F:\from-registry", "User"))
    assert llm._resolve_models_dir(layout) == (r"E:\from-env", "process environment")


def test_resolve_models_dir_falls_back_to_registry(monkeypatch, layout):
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)
    monkeypatch.setattr(llm, "_registry_env", lambda name: (r"F:\from-registry", "Machine"))
    assert llm._resolve_models_dir(layout) == (r"F:\from-registry", "Machine registry")


def test_resolve_models_dir_unset_when_neither_source_has_it(monkeypatch, layout):
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)
    monkeypatch.setattr(llm, "_registry_env", lambda name: (None, None))
    assert llm._resolve_models_dir(layout) == (None, "unset")


@pytest.mark.parametrize("model,names,ok", [("pixnote:4b", ["pixnote:4b"], True),
                                            ("pixnote:4b", [], False),
                                            ("pixnote:4b", ["pixnote:4b-instruct"], False)])
def test_model_alias_matching(model, names, ok):
    assert bool(llm._model_aliases(model) & set(names)) is ok
