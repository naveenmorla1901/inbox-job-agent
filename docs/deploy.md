# Deploy for free: GitHub Actions + Hugging Face Spaces + Neon

Three services, no credit card, nothing expires:

| Piece | Service | What it does |
| --- | --- | --- |
| Poller | GitHub Actions | Reads new mail every 15 minutes |
| Database | Neon Postgres | Keeps jobs and outreach forever |
| Dashboard | Hugging Face Spaces | Always-on web UI |
| Alerts | Telegram | Pings you for recruiters and interviews |

Do them in this order — the database URL is needed by the other two.

---

## 1. Neon Postgres (2 minutes)

1. Sign up at <https://neon.tech> with GitHub. Create a project (any region near you).
2. Copy the connection string from the dashboard. It looks like:
   `postgresql://user:password@ep-xxx.us-east-2.aws.neon.tech/neondb?sslmode=require`
3. Change the scheme to the driver this app uses:
   `postgresql+psycopg://user:password@ep-xxx.us-east-2.aws.neon.tech/neondb?sslmode=require`

That string is your `DATABASE_URL`. Tables are created automatically on first run.

Test it locally before you deploy anything:

```powershell
$env:DATABASE_URL = "postgresql+psycopg://..."
.\.venv\Scripts\python.exe -m app.run report --days 1
```

Prints an empty report instead of an error = the connection works.

---

## 2. Push the repo to GitHub (private is fine)

```powershell
cd C:\projects\inbox-job-agent
git add -A
git commit -m "Inbox job agent"
gh repo create inbox-job-agent --private --source . --push
```

Nothing sensitive ships: `.gitignore` excludes `.env`, `secrets/`, `data/` and your real
`config/profile.yaml`.

---

## 3. GitHub Actions poller

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
| --- | --- |
| `GMAIL_TOKEN_JSON` | entire contents of `secrets/token.json` |
| `PROFILE_YAML` | entire contents of `config/profile.yaml` |
| `DATABASE_URL` | the Neon string from step 1 |
| `GEMINI_API_KEY` | free key from <https://aistudio.google.com/apikey> |
| `TELEGRAM_BOT_TOKEN` | from `@BotFather` (optional) |
| `TELEGRAM_CHAT_ID` | from `@userinfobot` (optional) |

Then under the **Variables** tab: `LLM_PROVIDER` = `gemini`, and optionally `MIN_JOB_SCORE`
and `GMAIL_QUERY`.

Run it once by hand: **Actions → poll-inbox → Run workflow**. The log ends with a JSON summary
(`processed`, `jobs_matched`, `categories`). After that it runs itself every 15 minutes.

> GitHub disables schedules on repos with no activity for 60 days. One commit, or one manual run,
> re-arms it.

---

## 4. Hugging Face Space (dashboard)

1. <https://huggingface.co/new-space> → name it, **Docker** SDK, **Blank** template, public or
   private, free CPU hardware.
2. Push this repo to the Space remote:

```powershell
git remote add space https://huggingface.co/spaces/<your-username>/<space-name>
copy deploy\huggingface\README.md README-space.md   # Spaces need YAML front matter in README.md
```

The simplest path: copy `deploy/huggingface/README.md` over `README.md` **on the Space branch
only**, so the project README stays intact on GitHub:

```powershell
git checkout -b space
copy /Y deploy\huggingface\README.md README.md
git commit -am "Space config"
git push space space:main
git checkout main
```

3. Space → **Settings → Variables and secrets**, add:

| Name | Kind | Value |
| --- | --- | --- |
| `GMAIL_TOKEN_JSON` | secret | contents of `secrets/token.json` |
| `PROFILE_YAML` | secret | contents of `config/profile.yaml` |
| `DATABASE_URL` | secret | Neon string |
| `API_TOKEN` | secret | a long random string — this is your dashboard password |
| `GEMINI_API_KEY` | secret | your Gemini key |
| `LLM_PROVIDER` | variable | `gemini` |

4. Open the Space URL, enter the `API_TOKEN` at the login prompt. Same data the Actions poller
   writes, because both point at the same Neon database.

The Space only serves the dashboard; polling stays on Actions. If you would rather have the Space
poll too, add `POLL_IN_APP=1`-style scheduling later, or just hit **POST /api/run** from the Space
with your `API_TOKEN`.

---

## 5. Verify end to end

```bash
curl -H "x-api-token: <API_TOKEN>" https://<your-space>.hf.space/api/breakdown?days=1
```

Should return category counts identical to what the Actions log printed.

## Costs

Zero. Neon free tier: 0.5 GB storage, scales to zero. Actions: ~4 min/hour on a private repo
(~2,900 min/month) — if you are close to the 2,000 min limit, change the cron to `*/30` or make the
repo public, where Actions minutes are unlimited. Spaces free CPU: always on. Gemini free tier
covers far more emails than a personal inbox produces.
