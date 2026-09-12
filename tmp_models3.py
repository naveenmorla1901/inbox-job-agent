"""Find which NVIDIA / OpenRouter / Gemini models our client can actually call."""
import time

from app.llm import PROVIDERS, LLM

PROMPT = 'Reply with only JSON: {"roles": ["a", "b"]} -- list two job titles from: Data Scientist, ML Engineer'


def probe(pname: str, model: str, tries: int = 1) -> None:
    provider = PROVIDERS[pname]
    for attempt in range(1, tries + 1):
        llm = LLM()
        try:
            out = llm._call(provider, model, PROMPT, "You output JSON only.", 90)
            tag = "OK " if "roles" in (out or "") else "ODD"
            print(f"  {pname:<11} {model:<48} {tag} try{attempt} {(out or '')[:60]!r}")
            return
        except Exception as exc:  # noqa: BLE001
            code = getattr(getattr(exc, "response", None), "status_code", "")
            print(f"  {pname:<11} {model:<48} FAIL try{attempt} {code} {type(exc).__name__}")
            time.sleep(3)


print("=== nvidia candidates ===")
for m in (
    "nvidia/nemotron-3-ultra-550b-a55b",
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
    "nvidia/nemotron-nano-3-30b-a3b",
    "nvidia/llama-3.1-nemotron-70b-instruct",
    "deepseek-ai/deepseek-v4-flash-0731",
):
    probe("nvidia", m)

print("\n=== openrouter free candidates ===")
for m in (
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3.5-lightning:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemma-4-31b-it:free",
):
    probe("openrouter", m)

print("\n=== gemini candidates (retrying past 503 overload) ===")
for m in ("gemini-flash-latest", "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash"):
    probe("gemini", m, tries=4)
