import pytest

from app import config
from app.llm import LLM


@pytest.fixture()
def fresh_config():
    config.get_settings.cache_clear()
    config.get_profile.cache_clear()
    yield
    config.get_settings.cache_clear()
    config.get_profile.cache_clear()


def test_profile_can_come_from_an_env_var(monkeypatch, fresh_config):
    monkeypatch.setenv(
        "PROFILE_YAML",
        "name: Hosted User\ntarget_titles: [ai engineer]\nresume_text: python and llm work\n",
    )
    profile = config.get_profile()
    assert profile.name == "Hosted User"
    assert profile.target_titles == ["ai engineer"]


def test_gemini_needs_a_key_to_be_considered_enabled(monkeypatch, fresh_config):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY_2", "")
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("NVIDIA_API_KEY", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("LLM_CHAIN", "")
    monkeypatch.setenv("LLM_CHAIN_CLASSIFY", "")
    monkeypatch.setenv("LLM_CHAIN_EXTRACT", "")
    assert not LLM(config.get_settings()).enabled

    config.get_settings.cache_clear()
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    assert LLM(config.get_settings()).enabled


def test_json_helper_survives_markdown_fences(monkeypatch, fresh_config):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    llm = LLM(config.get_settings())
    monkeypatch.setattr(
        LLM, "complete", lambda *_, **__: '```json\n{"category": "offer", "confidence": 0.9}\n```'
    )
    assert llm.json("prompt") == {"category": "offer", "confidence": 0.9}


def test_json_helper_returns_empty_dict_on_garbage(monkeypatch, fresh_config):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    llm = LLM(config.get_settings())
    monkeypatch.setattr(LLM, "complete", lambda *_, **__: "sorry, I cannot help with that")
    assert llm.json("prompt") == {}


def test_disabled_provider_never_calls_out(monkeypatch, fresh_config):
    monkeypatch.setenv("LLM_PROVIDER", "none")
    llm = LLM(config.get_settings())
    assert llm.complete("anything") == ""


def test_enabled_provider_walks_every_key_you_have(monkeypatch, fresh_config):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setenv("GEMINI_API_KEY_2", "")
    monkeypatch.setenv("GROQ_API_KEY", "q-key")
    monkeypatch.setenv("NVIDIA_API_KEY", "n-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("LLM_CHAIN", "")
    monkeypatch.setenv("LLM_CHAIN_CLASSIFY", "")
    monkeypatch.setenv("LLM_CHAIN_EXTRACT", "")
    llm = LLM(config.get_settings())
    assert [p.name for p, _ in llm.chain("classify")] == ["groq", "gemini", "nvidia"]
    assert [p.name for p, _ in llm.chain("extract")] == ["nvidia", "gemini", "groq"]


def test_explicit_chain_overrides_the_default_walk(monkeypatch, fresh_config):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setenv("GEMINI_API_KEY_2", "")
    monkeypatch.setenv("GROQ_API_KEY", "q-key")
    monkeypatch.setenv("LLM_CHAIN_CLASSIFY", "gemini:gemini-2.0-flash,groq")
    llm = LLM(config.get_settings())
    names = [(p.name, model) for p, model in llm.chain("classify")]
    assert names[0] == ("gemini", "gemini-2.0-flash")
    assert names[1][0] == "groq"


def test_second_gemini_key_is_inserted_next_to_the_first(monkeypatch, fresh_config):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "key-a")
    monkeypatch.setenv("GEMINI_API_KEY_2", "key-b")
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("NVIDIA_API_KEY", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("LLM_CHAIN", "")
    monkeypatch.setenv("LLM_CHAIN_CLASSIFY", "")
    monkeypatch.setenv("LLM_CHAIN_EXTRACT", "")
    llm = LLM(config.get_settings())
    assert [p.name for p, _ in llm.chain("classify")] == ["gemini", "gemini2"]


def test_gemini_keys_alternate_after_each_success(monkeypatch, fresh_config):
    from app.llm import reset_cooldowns

    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "key-a")
    monkeypatch.setenv("GEMINI_API_KEY_2", "key-b")
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("NVIDIA_API_KEY", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("LLM_CHAIN_CLASSIFY", "gemini,gemini2")
    monkeypatch.setenv("LLM_GEMINI_GAP_SECONDS", "60")
    reset_cooldowns()
    llm = LLM(config.get_settings())
    used: list[str] = []

    def fake_call(self, provider, model, prompt, system, timeout):
        used.append(provider.name)
        return '{"ok": true}'

    monkeypatch.setattr(LLM, "_call", fake_call)
    assert llm.complete("one")
    assert llm.complete("two")
    assert used == ["gemini", "gemini2"]
