"""One-shot live pings for the Issues page Probe button."""

from __future__ import annotations

from .gmail_client import GmailClient, gmail_token_status
from .issues import record_issue
from .llm import LLM, reset_cooldowns


def probe_apis() -> list[dict]:
    """Hit Gmail and each configured LLM once. Records failures on the Issues log."""
    rows: list[dict] = []
    gmail = gmail_token_status()
    if not gmail["ok"]:
        record_issue("gmail", "Gmail probe failed", gmail["detail"], severity="error")
        rows.append({"name": "Gmail", "ok": False, "detail": gmail["detail"]})
    else:
        try:
            me = GmailClient().me_email()
            rows.append({"name": "Gmail", "ok": True, "detail": f"inbox {me}" if me else "token accepted"})
        except Exception as exc:
            detail = str(exc)[:240]
            record_issue("gmail", "Gmail probe failed", detail, severity="error")
            rows.append({"name": "Gmail", "ok": False, "detail": detail})

    llm = LLM()
    if not llm.enabled:
        rows.append({"name": "LLM", "ok": True, "detail": "LLM_PROVIDER=none — skipped"})
        return rows

    seen: set[str] = set()
    reset_cooldowns()
    for provider, model in llm.chain("extract") + llm.chain("classify"):
        if provider.name in seen:
            continue
        seen.add(provider.name)
        label = f"LLM · {provider.name}"
        try:
            answer, why = llm._try_provider(
                provider,
                model,
                'Reply with the single word pong and nothing else.',
                "One word only.",
                20,
            )
        except Exception as exc:
            answer, why = "", str(exc)[:240]
            record_issue("llm", f"LLM {provider.name} probe failed", why, severity="error")
            rows.append({"name": label, "ok": False, "detail": why})
            continue
        if answer and "pong" in answer.lower():
            rows.append({"name": label, "ok": True, "detail": f"{model} answered"})
        elif answer:
            rows.append({"name": label, "ok": True, "detail": f"{model} answered ({answer[:40]!r})"})
        else:
            record_issue("llm", f"LLM {provider.name} probe failed", why or "empty reply", severity="error")
            rows.append({"name": label, "ok": False, "detail": f"{model} {why or 'returned nothing'}"})
    if not seen:
        rows.append({"name": "LLM", "ok": False, "detail": "LLM is on but no provider has a key"})
    return rows
