# Free resources used (and the alternatives considered)

Master list to browse when you outgrow anything here: <https://github.com/ripienaar/free-for-dev>

## Mail ingestion

| Option | Cost | Why / why not |
| --- | --- | --- |
| **Gmail API** (chosen) | Free, 1B quota units/day | Direct access to your own inbox, no domain, no MX records, no forwarding delay. Refresh token works headless. |
| IMAP + [imap-to-webhook](https://github.com/Ibsciss/imap-to-webhook) (MIT) | Free | Works for any provider; needs an app password and a always-on process to hold the connection. Swap it in if you leave Gmail. |
| [EmailEngine / IMAP API](https://emailengine.app) (AGPL v1) | Free self-hosted | Persistent IMAP + webhooks. Heavier (Node + Redis) than this project needs. |
| [MailLaser](https://github.com/tsirysndr/maillaser) (MIT) | Free | SMTP server that POSTs JSON to a webhook. Requires a domain + MX records; only sees mail sent to that domain. |
| [Cloudflare Email Routing](https://developers.cloudflare.com/email-routing/) | Free | Great if you own a domain and want to forward job alerts to a Worker. Needs DNS control. |
| [Mailgun Routes](https://www.mailgun.com/pricing/) / [Postmark Inbound](https://postmarkapp.com/inbound-email) | Free tier (1 route / 100 msgs/mo) | Inbound parse to webhook; again domain-bound and quota-capped. |
| [ImprovMX](https://improvmx.com) | 500 forwards/day free | Forwarding only, no parsing. |

Gmail API won because the requirement is "watch **my** inbox", not "receive mail at a new address".

## Scheduling / compute

| Option | Free tier | Notes |
| --- | --- | --- |
| **GitHub Actions** | 2,000 min/mo private, unlimited on public repos | Used for the 15-minute poll. Cron only fires on the default branch. |
| **Hugging Face Spaces** (Docker) | Always-on CPU basic | Best free always-on host for the dashboard. |
| **Render** free web service | 750 h/mo, sleeps when idle | Fine for a personal dashboard; cold start ~30 s. |
| **Koyeb / Fly.io** | Small always-free allowances | Good middle ground if Render's sleeping annoys you. |
| **Oracle Cloud Always Free** | 4 ARM cores / 24 GB RAM | The most generous permanent free VM; run `python -m app.run loop` under systemd. |
| **Cloudflare Workers Cron** | 100k req/day | Would require a rewrite to JS; noted for completeness. |

## Database

| Option | Free tier | Notes |
| --- | --- | --- |
| **SQLite** (default) | Free | Perfect locally or on a VM. On GitHub Actions it survives only via the Actions cache (7 idle days). |
| **Neon** Postgres | 0.5 GB, always free | Serverless, scales to zero. `DATABASE_URL=postgresql+psycopg://…` |
| **Supabase** Postgres | 500 MB free | Adds a UI and REST API for free. Pauses after a week of inactivity. |
| **Turso** (libSQL) | 500 DBs / 9 GB free | SQLite-compatible; needs the `libsql-client` driver instead of stock SQLAlchemy. |

## LLM (optional — the rule engine works without any)

| Option | Free tier | Notes |
| --- | --- | --- |
| **Google AI Studio / Gemini** | Free tier with generous RPM/RPD on flash models | Default choice, `LLM_PROVIDER=gemini`. |
| **Groq** | Free developer tier, very fast | OpenAI-compatible, `LLM_PROVIDER=groq`. |
| **Ollama** local | Free, private | `llama3.1:8b` or `qwen2.5:7b` handle this triage fine on 16 GB RAM. |
| **OpenRouter** free models | Rate-limited free routes | Same OpenAI-compatible shape as Groq if you want to add it. |

Only ambiguous emails are sent to a model: job digests and obvious rejections never leave the box.

## Notifications

| Option | Free tier | Notes |
| --- | --- | --- |
| **Telegram Bot API** | Unlimited | Chosen. Token from `@BotFather`, chat id from `@userinfobot`. |
| Discord webhook | Unlimited | Drop-in replacement — same one POST shape as `notify.py`. |
| ntfy.sh | Free public server | Nice for phone push without an account. |
| Gmail label + filter | Free | `GMAIL_APPLY_LABEL=true` gives you a native "JobAgent" label if you prefer no third party. |

## Prior art worth reading

- [Inbox.ai](https://github.com/dhruvpat22/inbox-ai) — Gmail/Outlook OAuth + LLM labelling (MIT).
- [SYJ Mail Intelligence](https://github.com/syjblog/mail-intelligence) — self-hosted Gmail triage with local LLMs.
- [n8n](https://github.com/n8n-io/n8n) — if you would rather wire this as a no-code workflow.
- [mail-parser](https://github.com/SpamScope/mail-parser) — the MIME parsing this project does inline.
