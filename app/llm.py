"""Chat completion over free keys first, then paid DeepSeek as last resort.

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
        "gemini-flash-latest",
        style="gemini",
    ),
    # Same Gemini endpoint, second Google account. Cooldown and the post-call gap
    # are per provider name, so a 429 on one key does not park the other.
    "gemini2": Provider(
        "gemini2",
        "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        "gemini_api_key_2",
        "gemini-flash-latest",
        style="gemini",
    ),
    "groq": Provider(
        "groq",
        "https://api.groq.com/openai/v1/chat/completions",
        "groq_api_key",
        "openai/gpt-oss-120b",
    ),
    "deepseek": Provider(
        "deepseek",
        "https://api.deepseek.com/v1/chat/completions",
        "deepseek_api_key",
        "deepseek-flash",
    ),
    "nvidia": Provider(
        "nvidia",
        "https://integrate.api.nvidia.com/v1/chat/completions",
        "nvidia_api_key",
        "nvidia/nemotron-3-ultra-550b-a55b",
        json_mode=False,  # NIM rejects response_format on several hosted models
    ),
    "openrouter": Provider(
        "openrouter",
        "https://openrouter.ai/api/v1/chat/completions",
        "openrouter_api_key",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
    ),
    "ollama": Provider(
        "ollama",
        "{host}/api/chat",
        "",
        "llama3.1:8b",
        style="ollama",
    ),
}

# Free keys first. DeepSeek is paid, so it is last when the free tiers 429 or fail.
CLASSIFY_ORDER = ("groq", "gemini", "gemini2", "openrouter", "nvidia", "deepseek")
EXTRACT_ORDER = ("gemini", "gemini2", "groq", "openrouter", "nvidia", "deepseek")
TASK_ORDER = {CLASSIFY: CLASSIFY_ORDER, EXTRACT: EXTRACT_ORDER}
GEMINI_NAMES = frozenset({"gemini", "gemini2"})

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


def _cooldown_label(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        hours, rem = divmod(seconds, 3600)
        minutes = rem // 60
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


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
            chain.append((provider, model.strip()))
    return chain


class LLM:
    """Thin wrapper over free-tier chat endpoints. An empty chain disables it entirely."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.last_used: str = ""  # "gemini:gemini-2.0-flash" after a successful call

    def key_for(self, provider: Provider) -> str:
        if provider.style == "ollama":
            # Only try a local server when it was asked for — otherwise every poll
            # would hang on localhost:11434.
            if (self.settings.llm_provider or "").lower() == "ollama":
                return "local"
            return ""
        return str(getattr(self.settings, provider.key_field, "") or "")

    def _model_for(self, provider: Provider) -> str:
        if provider.style == "gemini":
            return self.settings.gemini_model or provider.default_model
        return {
            "groq": self.settings.groq_model,
            "deepseek": self.settings.deepseek_model,
            "nvidia": self.settings.nvidia_model,
            "openrouter": self.settings.openrouter_model,
            "ollama": self.settings.ollama_model,
        }.get(provider.name, provider.default_model) or provider.default_model

    def chain(self, task: str = CLASSIFY) -> list[tuple[Provider, str]]:
        settings = self.settings
        # LLM_PROVIDER=none means rules only, even if keys and chains sit in .env.
        if (settings.llm_provider or "none").lower() == "none":
            return []

        spec = (getattr(settings, f"llm_chain_{task}", "") or settings.llm_chain).strip()
        if spec:
            return self._with_remaining_keys(
                self._with_second_gemini(
                    [(p, m or self._model_for(p)) for p, m in parse_chain(spec) if self.key_for(p)]
                ),
                task,
            )

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

    def _with_remaining_keys(
        self, chain: list[tuple[Provider, str]], task: str
    ) -> list[tuple[Provider, str]]:
        """Keep the preferred order, then append every other key so a 429 cannot stall the run."""
        seen = {provider.name for provider, _ in chain}
        extra: list[tuple[Provider, str]] = []
        for name in TASK_ORDER.get(task, CLASSIFY_ORDER):
            if name in seen:
                continue
            provider = PROVIDERS.get(name)
            if not provider or not self.key_for(provider):
                continue
            extra.append((provider, self._model_for(provider)))
            seen.add(name)
        return chain + extra

    def _with_second_gemini(
        self, chain: list[tuple[Provider, str]]
    ) -> list[tuple[Provider, str]]:
        """If the chain names Gemini once and a second Google key exists, insert it next."""
        names = {provider.name for provider, _ in chain}
        extra = PROVIDERS["gemini2"]
        if "gemini" not in names or "gemini2" in names or not self.key_for(extra):
            return chain
        out: list[tuple[Provider, str]] = []
        for provider, model in chain:
            out.append((provider, model))
            if provider.name == "gemini":
                out.append((extra, model))
        return out

    def describe(self, task: str = CLASSIFY) -> str:
        parts = [f"{p.name}:{model}" for p, model in self.chain(task)]
        return " → ".join(parts) if parts else "none"

    @property
    def enabled(self) -> bool:
        return bool(self.chain(CLASSIFY) or self.chain(EXTRACT))

    def complete(self, prompt: str, system: str = "", task: str = CLASSIFY, timeout: int = 45) -> str:
        chain = self.chain(task)
        attempts: list[str] = []
        for provider, model in chain:
            label = f"{provider.name}:{model}"
            if _cooling(provider.name):
                attempts.append(f"{label} skipped (cooling)")
                continue
            answer, why = self._try_provider(provider, model, prompt, system, timeout)
            if answer:
                self.last_used = label
                self._rest_gemini(provider)
                if attempts:
                    log.info("%s answered %s after %s", label, task, "; ".join(attempts))
                    self._record_llm_outcome(
                        task,
                        f"{task} used {label}",
                        "Failed or skipped first, then a later model answered.\n"
                        + "\n".join(attempts)
                        + f"\n{label} answered",
                        severity="info",
                    )
                return answer
            attempts.append(f"{label} failed ({why or 'empty response'})")
        if attempts:
            log.warning("no LLM answered %s: %s", task, "; ".join(attempts))
        if chain:
            self._record_llm_outcome(
                task,
                f"No LLM answered {task}",
                "\n".join(attempts) or "chain was empty after keys",
                severity="error",
            )
        return ""

    def _record_llm_outcome(self, task: str, title: str, detail: str, severity: str) -> None:
        try:
            from .issues import record_issue

            record_issue("llm", title, detail, severity=severity)
        except Exception:
            pass

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
    ) -> tuple[str, str]:
        last_why = "empty response"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                answer = self._call(provider, model, prompt, system, timeout)
                return (answer, "") if answer else ("", "empty response")
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in QUOTA_STATUS:
                    self._park(provider, self.settings.llm_cooldown_seconds, "rate limited")
                    return "", "rate limited"
                if status in AUTH_STATUS:
                    self._park(provider, 3600, f"rejected the key ({status})")
                    return "", f"rejected the key ({status})"
                if status in (404, 410):
                    self._park(provider, 3600, f"endpoint or model gone ({status})")
                    return "", f"endpoint or model gone ({status})"
                if status in RETRY_STATUS and attempt < MAX_ATTEMPTS:
                    time.sleep(BACKOFF_SECONDS * attempt)
                    last_why = f"unavailable ({status})"
                    continue
                if status in RETRY_STATUS:
                    self._park(provider, 180, f"unavailable ({status})")
                    return "", f"unavailable ({status})"
                last_why = self._redact(str(exc))[:200]
                log.warning("%s failed (%s): %s", provider.name, model, last_why)
                return "", last_why
            except Exception as exc:
                last_why = self._redact(str(exc))[:200] or "error"
                log.warning("%s failed (%s): %s", provider.name, model, last_why)
                return "", last_why
        return "", last_why

    def _park(self, provider: Provider, seconds: int, why: str) -> None:
        _cooldowns[provider.name] = time.time() + seconds
        log.warning("%s %s - skipping it for %ds", provider.name, why, seconds)

    def _rest_gemini(self, provider: Provider) -> None:
        """After a successful Gemini call, sit that key out so the other account is used next."""
        if provider.name not in GEMINI_NAMES:
            return
        gap = int(self.settings.llm_gemini_gap_seconds or 0)
        if gap <= 0:
            return
        _cooldowns[provider.name] = time.time() + gap
        log.info("%s resting %ds so the other Gemini key can take the next call", provider.name, gap)

    def health(self) -> list[dict]:
        """Live status for every provider we know about — Issues page uses this."""
        now = time.time()
        llm_off = (self.settings.llm_provider or "none").lower() == "none"
        rows = []
        for name, provider in PROVIDERS.items():
            key = bool(self.key_for(provider))
            until = float(_cooldowns.get(name) or 0)
            cooling = key and until > now
            remaining = max(0, int(until - now)) if cooling else 0
            in_chain = any(p.name == name for p, _ in self.chain(EXTRACT) + self.chain(CLASSIFY))
            model = self._model_for(provider) if key else ""
            if not key:
                status, detail = "off", "no API key"
            elif cooling:
                status, detail = "warn", f"cooling {_cooldown_label(remaining)} — last call failed or hit quota"
            elif llm_off:
                status, detail = "idle", "key present · LLM_PROVIDER=none"
            elif in_chain:
                status, detail = "ok", f"in chain · {model}"
            else:
                status, detail = "idle", f"key present, not in current chain · {model}"
            rows.append(
                {
                    "name": name,
                    "configured": key,
                    "cooling": cooling,
                    "cooling_seconds": remaining,
                    "until": until if cooling else 0,
                    "model": model,
                    "in_chain": in_chain,
                    "status": status,
                    "detail": detail,
                }
            )
        return rows

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
        if provider.name == "deepseek":
            # V4.1 Flash defaults to thinking mode; disable it for short JSON triage.
            payload["thinking"] = {"type": "disabled"}
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
