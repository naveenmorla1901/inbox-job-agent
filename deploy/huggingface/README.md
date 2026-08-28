---
title: Inbox Job Agent
emoji: 📬
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# Inbox Job Agent

Private dashboard for job postings and recruiter mail extracted from Gmail.

This file replaces the project README **on the Space branch only** — Hugging Face requires the
YAML front matter above to build a Docker Space.

## Required Space secrets

| Name | Kind | Value |
| --- | --- | --- |
| `GMAIL_TOKEN_JSON` | secret | contents of `secrets/token.json` |
| `PROFILE_YAML` | secret | contents of `config/profile.yaml` |
| `DATABASE_URL` | secret | `postgresql+psycopg://...` from Neon |
| `API_TOKEN` | secret | long random string, acts as the dashboard password |
| `GEMINI_API_KEY` | secret | free key from Google AI Studio |
| `LLM_PROVIDER` | variable | `gemini` |

Set `API_TOKEN`. Without it the dashboard is readable by anyone who finds the Space URL.
