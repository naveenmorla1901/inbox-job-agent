from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from .config import Settings, get_settings

log = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

JSON_BLOCK = re.compile(r"\{.*\}", re.S)


class LLM:
    """Thin wrapper over free-tier chat endpoints. Provider 'none' disables it entirely."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.provider = (self.settings.llm_provider or "none").lower()

    @property
    def enabled(self) -> bool:
        s = self.settings
        if self.provider == "gemini":
            return bool(s.gemini_api_key)
        if self.provider == "groq":
            return bool(s.groq_api_key)
        if self.provider == "ollama":
            return True
        return False

    def complete(self, prompt: str, system: str = "", timeout: int = 45) -> str:
        if not self.enabled:
            return ""
        try:
            if self.provider == "gemini":
                return self._gemini(prompt, system, timeout)
            if self.provider == "groq":
                return self._openai_compatible(
                    GROQ_URL,
                    self.settings.groq_api_key,
                    self.settings.groq_model,
                    prompt,
                    system,
                    timeout,
                )
            if self.provider == "ollama":
                return self._ollama(prompt, system, timeout)
        except Exception as exc:
            log.warning("LLM call failed (%s): %s", self.provider, exc)
        return ""

    def json(self, prompt: str, system: str = "", timeout: int = 45) -> dict[str, Any]:
        raw = self.complete(prompt, system, timeout)
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

    def _gemini(self, prompt: str, system: str, timeout: int) -> str:
        url = GEMINI_URL.format(model=self.settings.gemini_model)
        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        resp = httpx.post(
            url,
            params={"key": self.settings.gemini_api_key},
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
        self, url: str, api_key: str, model: str, prompt: str, system: str, timeout: int
    ) -> str:
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": messages,
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def _ollama(self, prompt: str, system: str, timeout: int) -> str:
        resp = httpx.post(
            f"{self.settings.ollama_host.rstrip('/')}/api/chat",
            json={
                "model": self.settings.ollama_model,
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
