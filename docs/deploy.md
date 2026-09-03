# Deploy: Google Cloud Run + Neon

| Piece | Service | What it does |
| --- | --- | --- |
| Dashboard | **Google Cloud Run** | Docker container, public URL, login with `API_TOKEN` |
| Poller | Cloud Scheduler → `POST /api/run` | Reads Gmail every 30 minutes |
| Database | Neon Postgres | Keeps jobs and applications |
| Alerts | Telegram (optional) | Pings you for recruiters and interviews |

GitHub Actions is paused. Hugging Face Docker Spaces are paid now — skip them.

Do them in this order — the database URL is needed by Cloud Run.

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

**Paused.** The scheduled poller is turned off so failed runs stop emailing you. Poll on your
machine with `python -m app.run poll`. To turn GitHub back on later: restore the `cron` in
`.github/workflows/poll.yml`, remove `if: false` on the `poll` job, and push.

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
| --- | --- |
| `GMAIL_TOKEN_JSON` | entire contents of `secrets/token.json` |
| `PROFILE_YAML` | entire contents of `config/profile.yaml` |
| `DATABASE_URL` | the Neon string from step 1 |
| `GEMINI_API_KEY` | free key from <https://aistudio.google.com/apikey> |
| `GROQ_API_KEY` | free key from <https://console.groq.com/keys> |
| `NVIDIA_API_KEY` | free key from <https://build.nvidia.com> (optional) |
| `DEEPSEEK_API_KEY` | optional |
| `OPENROUTER_API_KEY` | optional |
| `TELEGRAM_BOT_TOKEN` | from `@BotFather` (optional) |
| `TELEGRAM_CHAT_ID` | from `@userinfobot` (optional) |

Then under the **Variables** tab: `LLM_PROVIDER` = `gemini` (turns the chain on; every key
you added is walked automatically), and optionally `MIN_JOB_SCORE` and `GMAIL_QUERY`.

Do not run it from Actions while it is paused. Poll locally instead.

---

## 4. Google Cloud Run (dashboard + optional poll)

This is the simple Docker path. One container serves the website. Cloud Scheduler hits
`POST /api/run` every 30 minutes so Gmail is polled without GitHub Actions.

You already have a Google Cloud project (`inbox-job-agent`) from Gmail OAuth. Use that.

### 4a. One-time Google Cloud setup

1. Install the Google Cloud SDK: <https://cloud.google.com/sdk/docs/install>
2. In PowerShell:

```powershell
gcloud auth login
gcloud config set project inbox-job-agent
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com cloudscheduler.googleapis.com secretmanager.googleapis.com
```

If `gcloud config get-value project` prints a different id, use that id in every command below.

### 4b. Put secrets in Secret Manager (do this on your laptop)

These files must not go in the Docker image. Cloud Run reads them as environment variables.

```powershell
cd C:\projects\inbox-job-agent

gcloud secrets create gmail-token --data-file=secrets\token.json
gcloud secrets create profile-yaml --data-file=config\profile.yaml
```

If a secret already exists, update it instead of creating:

```powershell
gcloud secrets versions add gmail-token --data-file=secrets\token.json
gcloud secrets versions add profile-yaml --data-file=config\profile.yaml
```

### 4c. Deploy the container

Pick a long dashboard password first (this is `API_TOKEN`). Then, still in the project folder:

```powershell
gcloud run deploy inbox-job-agent `
  --source . `
  --region us-east1 `
  --allow-unauthenticated `
  --timeout 900 `
  --memory 1Gi `
  --cpu 1 `
  --max-instances 1 `
  --set-env-vars "LLM_PROVIDER=gemini,GMAIL_QUERY=in:inbox -category:promotions,MIN_JOB_SCORE=0.45,API_TOKEN=pick-a-long-random-string" `
  --set-secrets "GMAIL_TOKEN_JSON=gmail-token:latest,PROFILE_YAML=profile-yaml:latest"
```

`--source .` builds the existing `Dockerfile` in Cloud Build. You do not push an image yourself.

When it finishes it prints a URL like:

`https://inbox-job-agent-xxxxx-ue.a.run.app`

Open that, enter `API_TOKEN` on the login page. That is your hosted dashboard.

### 4d. Add the remaining env vars in the console

Cloud Run → **inbox-job-agent** → **Edit & deploy new revision** → **Variables & secrets**:

| Name | Type | Value |
| --- | --- | --- |
| `DATABASE_URL` | env var | your Neon URL starting with `postgresql+psycopg://` |
| `GEMINI_API_KEY` | env var | from `.env` |
| `GROQ_API_KEY` | env var | from `.env` |
| `NVIDIA_API_KEY` | env var | optional |
| `DEEPSEEK_API_KEY` | env var | optional |
| `OPENROUTER_API_KEY` | env var | optional |

`GMAIL_TOKEN_JSON` and `PROFILE_YAML` should already be listed as secrets from step 4c.

Deploy the revision.

### 4e. Poll on a schedule (replaces GitHub Actions)

Cloud Scheduler → **Create job**:

| Field | Value |
| --- | --- |
| Name | `inbox-job-agent-poll` |
| Region | `us-east1` |
| Frequency | `*/30 * * * *` |
| Timezone | your local zone |
| Target type | HTTP |
| URL | `https://inbox-job-agent-xxxxx-ue.a.run.app/api/run` |
| HTTP method | POST |
| Auth header | add header `x-api-token` = the same `API_TOKEN` |

Timeout on the job: 15 minutes if the UI offers it.

Or from the CLI (replace the URL and token):

```powershell
gcloud scheduler jobs create http inbox-job-agent-poll `
  --location us-east1 `
  --schedule "*/30 * * * *" `
  --uri "https://inbox-job-agent-xxxxx-ue.a.run.app/api/run" `
  --http-method POST `
  --headers "x-api-token=YOUR_API_TOKEN" `
  --attempt-deadline 900s
```

### 4f. If the service account cannot read secrets

First deploy can fail with a permission error on Secret Manager. Grant access (the number is
your project number, from the Cloud Console home page):

```powershell
gcloud secrets add-iam-policy-binding gmail-token --member="serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor"
gcloud secrets add-iam-policy-binding profile-yaml --member="serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor"
```

Then deploy again.

## 5. Hugging Face (skip)

Docker Spaces are paid now. This app needs Docker. Use Cloud Run instead.

## Costs

Cloud Run idle with max-instances 1 and min-instances 0 is inside the free tier for a personal
dashboard. Cloud Scheduler’s first few jobs are free. Neon stays free. You pay only if the
container is busy for many hours or you raise memory a lot.
