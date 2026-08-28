# Setup, step by step

Every integration in this project, in the order you should do them. Each step ends with a command
that proves it worked. Total time: about 25 minutes, and the only one with any real clicking is
Gmail.

At any point you can run the built-in check and it will tell you what is still missing:

```powershell
cd C:\projects\inbox-job-agent
.\.venv\Scripts\python.exe -m app.run doctor
```

```
[ ok ] config file (.env)             C:\projects\inbox-job-agent\.env
[warn] profile (config/profile.yaml)  16 skills, 10 target titles - still the example resume
[ ok ] database                       sqlite: 4 messages, 3 jobs, 3 follow-ups, 1 applications
[FAIL] Gmail OAuth client             not found
[FAIL] Gmail token                    no token yet
[warn] LLM                            gemini selected but no key set; falling back to rules
[skip] Telegram                       not configured - alerts print to the log instead
[ ok ] job page fetching              probe returned status=ok via=greenhouse (3091 chars)
```

---

## Step 0 — Dependencies (already done on this machine)

```powershell
cd C:\projects\inbox-job-agent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
copy config\profile.example.yaml config\profile.yaml
```

Everything below edits those two files: `.env` for keys and switches, `config/profile.yaml` for
what counts as a good job.

**Verify:** `.\.venv\Scripts\python.exe -m app.run doctor --offline`

---

## Step 1 — Gmail API access

This is the only account setup that matters. You are creating your own private OAuth app that can
read your own mailbox. It is free and needs no billing account.

### 1a. Create the project and enable the API

1. Go to <https://console.cloud.google.com/> and sign in with **the Gmail account you want to
   monitor**.
2. Top bar → project dropdown → **New Project**. Name it `inbox-job-agent`. Create, then make sure
   it is the selected project.
3. Go to <https://console.cloud.google.com/apis/library/gmail.googleapis.com> → **Enable**.

### 1b. Configure the consent screen

1. **APIs & Services → OAuth consent screen**.
2. User type: **External** → Create. (Internal only exists for Workspace organisations.)
3. App name: anything. User support email and developer email: your address. Save and continue.
4. Scopes: skip this screen, the app requests its scope at runtime. Save and continue.
5. Test users: **Add users** → your own Gmail address. Save and continue.

**Important:** while the app is in *Testing*, Google expires the refresh token after **7 days**,
and the agent will stop with an invalid-grant error. To avoid re-authorising every week:

6. Back on the OAuth consent screen, click **Publish app** → confirm. Status becomes *In
   production*. Verification is not required for personal use; you will see an "unverified app"
   warning during the consent flow in step 1d, which you can pass with **Advanced → Go to
   inbox-job-agent (unsafe)**. It is your own app reading your own mailbox.

### 1c. Create the OAuth client

1. **APIs & Services → Credentials → Create credentials → OAuth client ID**.
2. Application type: **Desktop app**. Name it anything → Create.
3. **Download JSON** and save it as:

```
C:\projects\inbox-job-agent\secrets\client_secret.json
```

### 1d. Authorise once

```powershell
.\.venv\Scripts\python.exe -m app.auth_setup
```

A browser opens. Pick the account, pass the unverified-app warning, allow read access. The script
writes `secrets/token.json` — that file contains the refresh token and is what the hosted copies
use later. Never commit it (`.gitignore` already excludes `secrets/`).

Scope used: `gmail.readonly`. If you set `GMAIL_APPLY_LABEL=true` in `.env`, the app needs
`gmail.modify` instead — change it first, then re-run `app.auth_setup`.

**Verify:**

```powershell
.\.venv\Scripts\python.exe -m app.run doctor
```

You want:

```
[ ok ] Gmail token   you@gmail.com - 48,120 messages in the account
[ ok ] Gmail query   17 message(s) in the last 24h matching 'in:inbox -category:promotions after:...'
```

---

## Step 2 — Gemini key (free)

Used only for emails the rules cannot classify confidently, and to rescue job details when a site
blocks the scraper. The app works without it.

1. Go to <https://aistudio.google.com/apikey> → **Create API key** → pick your project → copy.
2. In `.env`:

```ini
LLM_PROVIDER=gemini
GEMINI_API_KEY=AIza...your key...
GEMINI_MODEL=gemini-2.0-flash
```

**Verify:** `doctor` should print `[ ok ] LLM  gemini (gemini-2.0-flash) answered` — that is a real
round trip to the API, not a config check.

Alternatives: `LLM_PROVIDER=groq` with a key from <https://console.groq.com/keys>,
`LLM_PROVIDER=ollama` for a local model, or `LLM_PROVIDER=none` to stay fully offline.

---

## Step 3 — Telegram alerts (optional, 2 minutes)

1. In Telegram, message **@BotFather** → `/newbot` → follow the prompts → copy the token.
2. Message **@userinfobot** → it replies with your numeric chat id.
3. Send your new bot any message once (bots cannot start conversations).
4. In `.env`:

```ini
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=123456789
```

**Verify:** `doctor` prints `[ ok ] Telegram  bot @yourbot, chat id ...`. You get a message when a
recruiter writes, an interview is scheduled, an assessment arrives, or new matching jobs are found.

---

## Step 4 — Your profile (the part that actually decides things)

Open `config/profile.yaml` and replace `resume_text` with your real resume as plain text. Then
adjust:

- `target_titles` — exact titles you want; a hit scores full marks on the title component.
- `title_keywords` — softer signals for partial credit.
- `exclude_titles`, `exclude_keywords` — hard rejects (sales roles, clearance requirements…).
- `skills` — weight 3 for core skills, 1 for nice-to-haves. Aliases catch how postings phrase them.
- `max_years_experience` — postings wanting much more get a penalty.

**Verify** against a real posting before trusting it on your inbox:

```powershell
.\.venv\Scripts\python.exe -m app.run match --title "Machine Learning Engineer" --file some_jd.txt
```

It prints the score plus the title/skills/resume breakdown and which skills matched. Tune
`MIN_JOB_SCORE` in `.env` until the cutoff feels right (0.45 is a reasonable start).

---

## Step 5 — First real run

```powershell
.\.venv\Scripts\python.exe -m app.run poll --days 1     # today's mail
.\.venv\Scripts\python.exe -m app.run report --days 1   # what it found, by category
.\.venv\Scripts\python.exe -m app.run serve             # http://localhost:8000
```

`poll --days 1` ignores the saved cursor and re-reads the last 24 hours. Normal runs
(`poll`, or `loop`) only look at mail newer than the last message they processed.

The dashboard: **Overview** (counts), **Jobs** (deduped postings), **Follow-ups** (mail from
people), **Applications** (your pipeline), **Mail log** (everything, with its category).

---

## Step 6 — Free Postgres so the data outlives the machine

Only needed for hosting; skip if you run locally.

1. Sign up at <https://neon.tech> with GitHub, create a project.
2. Copy the connection string and change the scheme to `postgresql+psycopg://`:

```ini
DATABASE_URL=postgresql+psycopg://user:pass@ep-xxx.aws.neon.tech/neondb?sslmode=require
```

**Verify:** `doctor` prints `[ ok ] database  postgresql: 0 messages, ...`. Tables are created on
first use.

---

## Step 7 — GitHub Actions (the poller that runs without you)

```powershell
git add -A
git commit -m "Inbox job agent"
gh repo create inbox-job-agent --private --source . --push
```

Repo → **Settings → Secrets and variables → Actions**:

| Secret | Where it comes from |
| --- | --- |
| `GMAIL_TOKEN_JSON` | paste the whole contents of `secrets/token.json` |
| `PROFILE_YAML` | paste the whole contents of `config/profile.yaml` |
| `DATABASE_URL` | Neon string from step 6 |
| `GEMINI_API_KEY` | step 2 |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | step 3 |

Variables tab: `LLM_PROVIDER=gemini`, optionally `MIN_JOB_SCORE`, `GMAIL_QUERY`.

**Verify:** Actions tab → **poll-inbox** → *Run workflow*. The log ends with a JSON summary
(`processed`, `jobs_matched`, `categories`). After that it runs every 30 minutes on its own.

---

## Step 8 — Hugging Face Space (the dashboard, always on)

Full instructions in [`deploy.md`](deploy.md). Short version: create a **Docker** Space, push this
repo to it with `deploy/huggingface/README.md` as the Space's `README.md`, and add the same secrets
plus `API_TOKEN` (a long random string — it is the dashboard password). Point it at the same
`DATABASE_URL` and it shows exactly what the Actions poller collects.

---

## What each credential is actually used for

| Credential | Used by | If missing |
| --- | --- | --- |
| `secrets/client_secret.json` | one-time OAuth flow | cannot authorise |
| `secrets/token.json` / `GMAIL_TOKEN_JSON` | every poll | poll fails immediately with a clear message |
| `config/profile.yaml` / `PROFILE_YAML` | scoring, alert-sender list | falls back to the example profile |
| `GEMINI_API_KEY` | ambiguous email triage, blocked-page rescue | rules-only classification |
| `TELEGRAM_*` | push alerts | alerts go to the log |
| `DATABASE_URL` | storage | local SQLite file |
| `API_TOKEN` | dashboard login, `POST /api/run` | dashboard open to anyone who reaches it |

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `invalid_grant` after a week | OAuth app still in *Testing*. Publish it (step 1b.6) and re-run `app.auth_setup`. |
| `No Gmail token found` | `secrets/token.json` missing, or `GMAIL_TOKEN_JSON` not set on the host. |
| `insufficient permission` when labelling | `GMAIL_APPLY_LABEL=true` needs the `gmail.modify` scope — re-run `app.auth_setup`. |
| Poll returns 0 messages | `GMAIL_QUERY` too narrow, or everything already processed. Use `poll --days 3`. |
| Jobs saved with `scrape_status=blocked` | The board refused the fetch (Indeed does this often). The email's own summary is used instead, and Gemini fills the gaps. |
| Everything scores low | `resume_text` is still the placeholder, or your skill weights do not match how postings phrase things. |
| Actions schedule stopped | GitHub pauses cron on repos with no activity for 60 days. Push a commit or run the workflow manually. |
