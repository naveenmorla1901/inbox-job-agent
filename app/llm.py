"""Chat completion over whichever free tier is still answering.

Each task runs down an ordered chain of `provider:model` pairs. A provider that
rate limits is parked for a cooldown so the rest of the run stops paying for its
timeouts, and the next provider in the chain answers instead.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings, get_settings

log = logging.getLogger(__name__)

JSON_BLOCK = re.compile(r"\{.*\}", re.S)

# Free tiers answer 429/503 under load often enough that one attempt is not enough.
RETRY_STATUS = {408, 500, 502, 503, 504}
QUOTA_STATUS = {429}
AUTH_STATUS = {401, 403}
MAX_ATTEMPTS = 2
BACKOFF_SECONDS = 1.5

CLASSIFY = "classify"
EXTRACT = "extract"


@dataclass(frozen=True)
class Provider:
    name: str
    url: str
    key_field: str
    default_model: str
    style: str = "openai"  # openai | gemini | ollama
    json_mode: bool = True


PROVIDERS: dict[str, Provider] = {
    "gemini": Provider(
        "gemini",
        "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        "gemini_api_key",
        "gemini-2.0-flash",
        style="gemini",
    ),
    "groq": Provider(
        "groq",
        "https://api.groq.com/openai/v1/chat/completions",
        "groq_api_key",
        "llama-3.3-70b-versatile",
    ),
    "deepseek": Provider(
        "deepseek",
        "https://api.deepseek.com/v1/chat/completions",
        "deepseek_api_key",
        "deepseek-chat",
    ),
    "nvidia": Provider(
        "nvidia",
        "https://integrate.api.nvidia.com/v1/chat/completions",
        "nvidia_api_key",
        "meta/llama-3.3-70b-instruct",
        json_mode=False,  # NIM rejects response_format on several hosted models
    ),
    "openrouter": Provider(
        "openrouter",
        "https://openrouter.ai/api/v1/chat/completions",
        "openrouter_api_key",
        "meta-llama/llama-3.3-70b-instruct:free",
    ),
    "ollama": Provider(
        "ollama",
        "{host}/api/chat",
        "",
        "llama3.1:8b",
        style="ollama",
    ),
}

# Cheap/fast first for high-volume triage; stronger models first for rare long extracts.
CLASSIFY_ORDER = ("groq", "gemini", "nvidia", "deepseek", "openrouter")
EXTRACT_ORDER = ("nvidia", "deepseek", "gemini", "groq", "openrouter")
TASK_ORDER = {CLASSIFY: CLASSIFY_ORDER, EXTRACT: EXTRACT_ORDER}

# provider name -> unix time it may be tried again
_cooldowns: dict[str, float] = {}


def reset_cooldowns() -> None:
    _cooldowns.clear()


def _cooling(name: str) -> bool:
    until = _cooldowns.get(name, 0.0)
    if until and until > time.time():
        return True
    _cooldowns.pop(name, None)
    return False


def parse_chain(spec: str) -> list[tuple[Provider, str]]:
    """'gemini:gemini-2.0-flash, groq' -> [(gemini, gemini-2.0-flash), (groq, default)]"""
    chain: list[tuple[Provider, str]] = []
    for part in (spec or "").replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        name, _, model = part.partition(":")
        provider = PROVIDERS.get(name.strip().lower())
        if provider:
            chain.append((provider, model.strip() or provider.default_model))
    return chain


class LLM:
    """Thin wrapper over free-tier chat endpoints. An empty chain disables it entirely."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def key_for(self, provider: Provider) -> str:
        if provider.style == "ollama":
            # Only try a local server when it was asked for — otherwise every poll
            # would hang on localhost:11434.
            if (self.settings.llm_provider or "").lower() == "ollama":
                return "local"
            return ""
        return str(getattr(self.settings, provider.key_field, "") or "")

    def _model_for(self, provider: Provider) -> str:
        return {
            "gemini": self.settings.gemini_model,
            "groq": self.settings.groq_model,
            "ollama": self.settings.ollama_model,
        }.get(provider.name, provider.default_model) or provider.default_model

    def chain(self, task: str = CLASSIFY) -> list[tuple[Provider, str]]:
        settings = self.settings
        spec = (getattr(settings, f"llm_chain_{task}", "") or settings.llm_chain).strip()
        if spec:
            return [(p, m) for p, m in parse_chain(spec) if self.key_for(p)]

        # LLM_PROVIDER=none means rules only, even if keys sit in .env.
        if (settings.llm_provider or "none").lower() == "none":
            return []

        # Any other provider value turns LLMs on and walks every key you have,
        # cheapest-first for classify and strongest-first for extract.
        names = list(TASK_ORDER.get(task, CLASSIFY_ORDER))
        if (settings.llm_provider or "").lower() == "ollama":
            names.append("ollama")
        out: list[tuple[Provider, str]] = []
        seen: set[str] = set()
        for name in names:
            provider = PROVIDERS.get(name)
            if not provider or name in seen or not self.key_for(provider):
                continue
            out.append((provider, self._model_for(provider)))
            seen.add(name)
        return out

    def describe(self, task: str = CLASSIFY) -> str:
        parts = [f"{p.name}:{model}" for p, model in self.chain(task)]
        return " → ".join(parts) if parts else "none"

    @property
    def enabled(self) -> bool:
        return bool(self.chain(CLASSIFY) or self.chain(EXTRACT))

    def complete(self, prompt: str, system: str = "", task: str = CLASSIFY, timeout: int = 45) -> str:
        chain = self.chain(task)
        skipped: list[str] = []
        for provider, model in chain:
            if _cooling(provider.name):
                skipped.append(provider.name)
                continue
            answer = self._try_provider(provider, model, prompt, system, timeout)
            if answer:
                if skipped:
                    log.info("%s answered %s after skipping %s", provider.name, task, ", ".join(skipped))
                return answer
        if skipped:
            log.warning("every provider for %s is cooling down: %s", task, ", ".join(skipped))
        return ""

    def json(self, prompt: str, system: str = "", task: str = CLASSIFY, timeout: int = 45) -> dict[str, Any]:
        raw = self.complete(prompt, system, task=task, timeout=timeout)
        if not raw:
            return {}
        match = JSON_BLOCK.search(raw)
        if not match:
            return {}
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            log.info("LLM returned non-JSON: %s", raw[:200])
            return {}

    def _try_provider(
        self, provider: Provider, model: str, prompt: str, system: str, timeout: int
    ) -> str:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return self._call(provider, model, prompt, system, timeout)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in QUOTA_STATUS:
                    self._park(provider, self.settings.llm_cooldown_seconds, "rate limited")
                    return ""
                if status in AUTH_STATUS:
                    self._park(provider, 3600, f"rejected the key ({status})")
                    return ""
                if status in RETRY_STATUS and attempt < MAX_ATTEMPTS:
                    time.sleep(BACKOFF_SECONDS * attempt)
                    continue
                log.warning("%s failed (%s): %s", provider.name, model, self._redact(str(exc)))
                return ""
            except Exception as exc:
                log.warning("%s failed (%s): %s", provider.name, model, self._redact(str(exc)))
                return ""
        return ""

    def _park(self, provider: Provider, seconds: int, why: str) -> None:
        _cooldowns[provider.name] = time.time() + seconds
        log.warning("%s %s - skipping it for %ds", provider.name, why, seconds)

    def _redact(self, text: str) -> str:
        """Provider errors quote the request URL, which can carry the API key."""
        for provider in PROVIDERS.values():
            secret = self.key_for(provider)
            if secret and secret != "local":
                text = text.replace(secret, "***")
        return text

    def _call(self, provider: Provider, model: str, prompt: str, system: str, timeout: int) -> str:
        if provider.style == "gemini":
            return self._gemini(provider, model, prompt, system, timeout)
        if provider.style == "ollama":
            return self._ollama(model, prompt, system, timeout)
        return self._openai_compatible(provider, model, prompt, system, timeout)

    def _gemini(self, provider: Provider, model: str, prompt: str, system: str, timeout: int) -> str:
        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        resp = httpx.post(
            provider.url.format(model=model),
            params={"key": self.key_for(provider)},
            json=body,
            timeout=timeout,
        )
        resp.raise_for_status()
        candidates = resp.json().get("candidates", [])
        if not candidates:
            return ""
        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts)

    def _openai_compatible(
        self, provider: Provider, model: str, prompt: str, system: str, timeout: int
    ) -> str:
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        payload: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0.1}
        if provider.json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.key_for(provider)}"}
        if provider.name == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/naveenmorla1901/inbox-job-agent"
            headers["X-Title"] = "Inbox Job Agent"
        resp = httpx.post(provider.url, headers=headers, json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def _ollama(self, model: str, prompt: str, system: str, timeout: int) -> str:
        resp = httpx.post(
            f"{self.settings.ollama_host.rstrip('/')}/api/chat",
            json={
                "model": model,
                "messages": ([{"role": "system", "content": system}] if system else [])
                + [{"role": "user", "content": prompt}],
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.1},
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "")
