"""The Ollama doctor check (the tag is part of the model, the store must be on D:). Ported from a project-local
extension's tests, onto supervisor.plugin and supervisor.testing only - no framework internal: every test here
calls this package's own functions directly."""
from __future__ import annotations

import pytest
import requests

import supervisor_plugin_local_llm as llm


def serve_tags(monkeypatch, names):
    class Resp:
        def json(self):
            return {"models": [{"name": n} for n in names]}

        def raise_for_status(self):
            pass
    monkeypatch.setattr(llm.requests, "get", lambda url, timeout=None: Resp())


def fake_store(layout, monkeypatch, *, model="qwen3-vl:4b", layout_v1=True, layout_v2=False,
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


def test_ollama_requires_the_configured_tag(layout, monkeypatch):
    monkeypatch.setattr(llm, "local_llm_config", lambda lay: {"base_url": "http://x", "model": "qwen3-vl:4b", "models_dir": "D:\\x"})
    fake_store(layout, monkeypatch, model="qwen3-vl:4b")
    serve_tags(monkeypatch, ["qwen3-vl:8b"])                 # same family, wrong tag: the adapter would get a 404
    check = llm.check_ollama(layout)
    assert not check.ok and "qwen3-vl:4b not pulled" in check.detail
    serve_tags(monkeypatch, ["qwen3-vl:8b", "qwen3-vl:4b"])
    assert llm.check_ollama(layout).ok


def test_ollama_accepts_only_the_latest_normalisation(layout, monkeypatch):
    monkeypatch.setattr(llm, "local_llm_config", lambda lay: {"base_url": "http://x", "model": "llava", "models_dir": "D:\\x"})
    fake_store(layout, monkeypatch, model="llava:latest")
    serve_tags(monkeypatch, ["llava:latest"])                # Ollama's implicit tag for an untagged pull
    assert llm.check_ollama(layout).ok
    serve_tags(monkeypatch, ["llava:13b"])
    assert not llm.check_ollama(layout).ok


def test_ollama_fails_cleanly_when_unreachable(layout, monkeypatch):
    monkeypatch.setattr(llm, "local_llm_config", lambda lay: {"base_url": "http://x", "model": "qwen3-vl:4b", "models_dir": "D:\\x"})
    monkeypatch.setattr(llm.requests, "get", lambda url, timeout=None: (_ for _ in ()).throw(requests.ConnectionError("down")))
    check = llm.check_ollama(layout)
    assert not check.ok and "unreachable" in check.detail


def test_ollama_fails_when_models_dir_is_unset_everywhere(layout, monkeypatch):
    monkeypatch.setattr(llm, "local_llm_config", lambda lay: {"base_url": "http://x", "model": "qwen3-vl:4b", "models_dir": "D:\\expected"})
    serve_tags(monkeypatch, ["qwen3-vl:4b"])
    monkeypatch.setattr(llm, "_resolve_models_dir", lambda lay: (None, "unset"))
    check = llm.check_ollama(layout)
    assert not check.ok and "not set" in check.detail and "D:\\expected" in check.detail


def test_ollama_fails_when_the_store_is_off_d(layout, monkeypatch):
    monkeypatch.setattr(llm, "local_llm_config", lambda lay: {"base_url": "http://x", "model": "qwen3-vl:4b", "models_dir": "D:\\expected"})
    serve_tags(monkeypatch, ["qwen3-vl:4b"])
    monkeypatch.setattr(llm, "_resolve_models_dir",
                        lambda lay: (r"C:\Users\someone\.ollama\models", "User registry"))
    check = llm.check_ollama(layout)
    assert not check.ok and "not on D:" in check.detail


def test_ollama_fails_when_the_manifest_is_missing_from_the_configured_store(layout, monkeypatch):
    monkeypatch.setattr(llm, "local_llm_config", lambda lay: {"base_url": "http://x", "model": "qwen3-vl:4b", "models_dir": "D:\\x"})
    serve_tags(monkeypatch, ["qwen3-vl:4b"])
    fake_store(layout, monkeypatch, model="qwen3-vl:4b", layout_v1=False, layout_v2=False)   # dir reported, nothing under it
    check = llm.check_ollama(layout)
    assert not check.ok and "manifest" in check.detail and "not found" in check.detail


def test_ollama_accepts_either_manifest_layout(layout, monkeypatch):
    monkeypatch.setattr(llm, "local_llm_config", lambda lay: {"base_url": "http://x", "model": "qwen3-vl:4b", "models_dir": "D:\\x"})
    serve_tags(monkeypatch, ["qwen3-vl:4b"])
    fake_store(layout, monkeypatch, model="qwen3-vl:4b", layout_v1=False, layout_v2=True)   # only the newer layout present
    assert llm.check_ollama(layout).ok


def test_ollama_resolves_models_dir_from_registry_when_process_env_is_unset(layout, monkeypatch):
    """The serving process may predate OLLAMA_MODELS existing at all: doctor falls back to the persisted
    User/Machine registry value rather than trusting only its own process environment."""
    monkeypatch.setattr(llm, "local_llm_config", lambda lay: {"base_url": "http://x", "model": "qwen3-vl:4b", "models_dir": "D:\\x"})
    serve_tags(monkeypatch, ["qwen3-vl:4b"])
    store = layout.root / "models" / "ollama"
    (store / "manifests" / "registry.ollama.ai" / "library" / "qwen3-vl").mkdir(parents=True, exist_ok=True)
    (store / "manifests" / "registry.ollama.ai" / "library" / "qwen3-vl" / "4b").write_text("{}", encoding="utf-8")
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)
    monkeypatch.setattr(llm, "_registry_env", lambda name: (str(store), "User"))
    check = llm.check_ollama(layout)
    assert check.ok and "User registry" in check.detail


def test_resolve_models_dir_prefers_process_env_over_registry(monkeypatch, layout):
    monkeypatch.setenv("OLLAMA_MODELS", r"D:\from-env")
    monkeypatch.setattr(llm, "_registry_env", lambda name: (r"D:\from-registry", "User"))
    assert llm._resolve_models_dir(layout) == (r"D:\from-env", "process environment")


def test_resolve_models_dir_falls_back_to_registry(monkeypatch, layout):
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)
    monkeypatch.setattr(llm, "_registry_env", lambda name: (r"D:\from-registry", "Machine"))
    assert llm._resolve_models_dir(layout) == (r"D:\from-registry", "Machine registry")


def test_resolve_models_dir_unset_when_neither_source_has_it(monkeypatch, layout):
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)
    monkeypatch.setattr(llm, "_registry_env", lambda name: (None, None))
    assert llm._resolve_models_dir(layout) == (None, "unset")


@pytest.mark.parametrize("model,names,ok", [("qwen3-vl:4b", ["qwen3-vl:4b"], True),
                                            ("qwen3-vl:4b", [], False),
                                            ("qwen3-vl:4b", ["qwen3-vl:4b-instruct"], False)])
def test_model_alias_matching(model, names, ok):
    assert bool(llm._model_aliases(model) & set(names)) is ok
