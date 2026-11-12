"""Settings (plugins.llm_endpoint): every key a neutral default or none at all (D-013 section 6) - no model, no
argv, no project path; `base_url` defaults to llama.cpp's own documented loopback port, not a project's choice."""
from __future__ import annotations

from supervisor_plugin_llm_endpoint.config import Settings


def test_defaults_name_no_model_no_argv_and_the_tools_own_loopback_port():
    settings = Settings()
    assert settings.base_url == "http://127.0.0.1:8080"
    assert settings.model is None
    assert settings.argv is None
    assert "vram_device" not in Settings.model_fields     # every configured card is checked; none is "the" device


def test_timeouts_and_budgets_have_the_designed_defaults():
    settings = Settings()
    assert settings.start_timeout_s == 300
    assert settings.idle_stop_s == 900
    assert settings.timeout_s == 1800
    assert settings.context_tokens == 8192
    assert settings.max_tokens == 2048
    assert settings.chars_per_token == 3           # F7: conservative for code, diffs, YAML, non-English text


def test_every_field_accepts_an_override():
    settings = Settings(base_url="http://127.0.0.1:8799", model="demo-model", argv=["demo-server", "--port", "8799"],
                        start_timeout_s=60, idle_stop_s=120, timeout_s=90, context_tokens=4096, max_tokens=512)
    assert settings.base_url == "http://127.0.0.1:8799"
    assert settings.model == "demo-model"
    assert settings.argv == ["demo-server", "--port", "8799"]
