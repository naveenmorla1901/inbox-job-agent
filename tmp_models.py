"""List what each provider actually serves today. Prints model ids only, never keys."""
import httpx

from app.config import get_settings

s = get_settings()


def show(name: str, ids: list[str], keep=lambda i: True) -> None:
    hits = sorted({i for i in ids if keep(i)})
    print(f"\n=== {name} ({len(ids)} total, {len(hits)} shown) ===")
    for i in hits:
        print("   ", i)


with httpx.Client(timeout=40) as c:
    if s.gemini_api_key:
        r = c.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            params={"key": s.gemini_api_key, "pageSize": 200},
        )
        data = r.json().get("models", [])
        ids = [
            m["name"].removeprefix("models/")
            for m in data
            if "generateContent" in m.get("supportedGenerationMethods", [])
        ]
        show("GEMINI", ids, lambda i: not any(
            x in i for x in ("embedding", "aqa", "imagen", "tts", "image", "veo", "vision")
        ))

    if s.groq_api_key:
        r = c.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {s.groq_api_key}"},
        )
        ids = [m["id"] for m in r.json().get("data", [])]
        show("GROQ", ids, lambda i: not any(
            x in i for x in ("whisper", "tts", "guard", "prompt-guard")
        ))

    if s.nvidia_api_key:
        r = c.get(
            "https://integrate.api.nvidia.com/v1/models",
            headers={"Authorization": f"Bearer {s.nvidia_api_key}"},
        )
        ids = [m["id"] for m in r.json().get("data", [])]
        show("NVIDIA (nemotron / deepseek / qwen / llama only)", ids, lambda i: any(
            x in i for x in ("nemotron", "deepseek", "qwen", "llama-3.3", "llama-4")
        ))

    r = c.get("https://openrouter.ai/api/v1/models")
    free = [
        m["id"]
        for m in r.json().get("data", [])
        if m["id"].endswith(":free")
        and int(m.get("context_length") or 0) >= 60000
    ]
    show("OPENROUTER free, >=60k context", free)
