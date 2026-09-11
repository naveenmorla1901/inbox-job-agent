import httpx

from app.llm import LLM, PROVIDERS, reset_cooldowns


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.com/v1")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"{status}", request=request, response=response)


def test_failover_logs_which_model_answered(monkeypatch):
    reset_cooldowns()
    captured = []
    monkeypatch.setattr(
        "app.issues.record_issue",
        lambda source, title, detail="", severity="error", message_id="": captured.append(
            {"title": title, "detail": detail, "severity": severity}
        ),
    )
    llm = LLM()
    monkeypatch.setattr(
        llm,
        "chain",
        lambda task="classify": [(PROVIDERS["groq"], "oss"), (PROVIDERS["gemini"], "flash")],
    )

    def fake_call(provider, model, prompt, system, timeout):
        if provider.name == "groq":
            raise _http_error(429)
        return '{"category":"other"}'

    monkeypatch.setattr(llm, "_call", fake_call)
    assert llm.complete("ping", task="classify") == '{"category":"other"}'
    assert llm.last_used == "gemini:flash"
    assert captured
    assert captured[0]["severity"] == "info"
    assert "gemini:flash" in captured[0]["title"]
    assert "rate limited" in captured[0]["detail"]
    assert "answered" in captured[0]["detail"]


def test_all_models_fail_is_an_error(monkeypatch):
    reset_cooldowns()
    captured = []
    monkeypatch.setattr(
        "app.issues.record_issue",
        lambda source, title, detail="", severity="error", message_id="": captured.append(
            {"title": title, "severity": severity, "detail": detail}
        ),
    )
    llm = LLM()
    monkeypatch.setattr(llm, "chain", lambda task="classify": [(PROVIDERS["groq"], "oss")])
    monkeypatch.setattr(llm, "_call", lambda *args, **kwargs: (_ for _ in ()).throw(_http_error(429)))
    assert llm.complete("ping") == ""
    assert captured[0]["severity"] == "error"
    assert "No LLM answered" in captured[0]["title"]
    assert "rate limited" in captured[0]["detail"]
