"""Resolve alias versions, check our configured ids still exist, and smoke-test candidates."""
import httpx

from app.config import get_settings
from app.llm import PROVIDERS, LLM

s = get_settings()

with httpx.Client(timeout=60) as c:
    r = c.get(
        "https://generativelanguage.googleapis.com/v1beta/models",
        params={"key": s.gemini_api_key, "pageSize": 200},
    )
    print("=== gemini alias resolution ===")
    for m in r.json().get("models", []):
        name = m["name"].removeprefix("models/")
        if name in ("gemini-flash-latest", "gemini-pro-latest", "gemini-3.8-flash",
                    "gemini-3.7-flash", "gemini-3.5-flash"):
            print(f"  {name:<24} version={m.get('version','?'):<18} "
                  f"in={m.get('inputTokenLimit')} out={m.get('outputTokenLimit')} "
                  f"| {m.get('displayName')}")

    r = c.get("https://openrouter.ai/api/v1/models")
    data = {m["id"]: m for m in r.json().get("data", [])}
    print("\n=== openrouter: is our configured id still there? ===")
    for want in ("openai/gpt-oss-20b:free", "openai/gpt-oss-120b:free"):
        m = data.get(want)
        print(f"  {want:<30} {'PRESENT ctx=' + str(m.get('context_length')) if m else 'GONE'}")
    print("\n=== openrouter free candidates by context ===")
    free = [m for m in data.values() if m["id"].endswith(":free")]
    for m in sorted(free, key=lambda m: -int(m.get("context_length") or 0))[:8]:
        print(f"  {m['id']:<50} ctx={m.get('context_length')}")

print("\n=== live smoke test: can each candidate answer our JSON style? ===")
prompt = 'Reply with only JSON: {"roles": ["a", "b"]} -- list two job titles from: Data Scientist, ML Engineer'
for pname, model in [
    ("gemini", "gemini-3.8-flash"),
    ("gemini", "gemini-flash-latest"),
    ("groq", "openai/gpt-oss-120b"),
    ("nvidia", "nvidia/nemotron-3-ultra-550b-a55b"),
    ("openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free"),
    ("openrouter", "thinkingmachines/inkling:free"),
]:
    llm = LLM()
    provider = PROVIDERS[pname]
    try:
        out = llm._call(provider, model, prompt, "You output JSON only.", 60)
        ok = "roles" in (out or "")
        print(f"  {pname:<11} {model:<46} {'OK ' if ok else 'ODD'} {(out or '')[:70]!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"  {pname:<11} {model:<46} FAIL {type(exc).__name__}: {str(exc)[:90]}")
