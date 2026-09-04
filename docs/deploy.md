# First-time deploy: Google Cloud Run (Windows)

You already run this app on your laptop against **the same Gmail inbox**. Hosting it means:

1. Google builds the **existing Dockerfile** in the cloud (you do not install Docker Desktop).
2. The container stays at a public URL (the dashboard).
3. Every 30 minutes Cloud Scheduler calls `POST /api/run`, which reads that same Gmail.

GitHub merge does **not** deploy this. You deploy from this folder with `gcloud`.

| Piece | Where | What it does |
| --- | --- | --- |
| Dashboard + poller | **Cloud Run** (one container) | Website + `POST /api/run` |
| Schedule | **Cloud Scheduler** | Hits `/api/run` every 30 minutes |
| Database | **Neon Postgres** (free) | Jobs survive when the container sleeps |
| Gmail | Your existing `secrets/token.json` | Same mailbox as localhost |
| Profile | Your existing `config/profile.yaml` | Same titles / skills |

Do **not** upload `.env`, `secrets/`, or `config/profile.yaml` to GitHub. Cloud Run gets those as secrets.

---

## What you need on this PC (you already have most of it)

- This repo at `C:\projects\inbox-job-agent`
- `secrets\token.json` (created when you ran Gmail login locally)
- `config\profile.yaml` (your real profile)
- `.env` with `GEMINI_API_KEY` (and `GEMINI_API_KEY_2` / `GROQ_API_KEY` if you use them)

If `token.json` is missing, run this **on the laptop** first, then come back:

```powershell
cd C:\projects\inbox-job-agent
.\.venv\Scripts\python.exe -m app.auth_setup
```

Use the **same Google account** you want the cloud app to read.

---

## Cost (read this once)

Google requires a **credit card** to turn on billing, even if you stay in the free tier. Cloud Run with `max-instances 1` and min instances 0 is meant to stay inside the free allowance for a personal dashboard. Neon is free. You are billed only if the container runs for many hours or you raise memory a lot.

If you never want a card on file, keep polling on this laptop. There is no card-free Cloud Run path.

---

## Step 1 — Free database (Neon)

Cloud Run’s disk is wiped when the service sleeps. SQLite from your laptop will **not** work in the cloud. Use Neon.

1. Open <https://neon.tech> and sign in with GitHub.
2. Create a project (any region near the US East Coast is fine).
3. Open the connection string. It looks like:

   `postgresql://USER:PASSWORD@ep-xxx.us-east-2.aws.neon.tech/neondb?sslmode=require`

4. Change **only** the start from `postgresql://` to `postgresql+psycopg://`:

   `postgresql+psycopg://USER:PASSWORD@ep-xxx.us-east-2.aws.neon.tech/neondb?sslmode=require`

5. Save that string in Notepad. That is `DATABASE_URL`. Tables are created on first run.

Optional check on this laptop:

```powershell
cd C:\projects\inbox-job-agent
$env:DATABASE_URL = "postgresql+psycopg://USER:PASSWORD@HOST/neondb?sslmode=require"
.\.venv\Scripts\python.exe -m app.run report --days 1
```

An empty report (not an error) means Neon works. This does **not** copy your local SQLite jobs into Neon. The cloud dashboard starts empty and fills as Gmail is polled.

---

## Step 2 — Create a Google Cloud project (browser)

1. Open <https://console.cloud.google.com/> and sign in with **the same Gmail** this app already reads.
2. Top bar → project dropdown → **New project**.
3. Project name: `inbox-job-agent`. Create.
4. Wait until it finishes, then select that project in the top bar.
5. Open **Billing** → **Link a billing account**. Add a card if Google asks. Cloud Run will not deploy without this.

If you already created a project named `inbox-job-agent` for Gmail OAuth, **reuse it**. Do not make a second project.

The **Project ID** in **Home** (often `inbox-job-agent` or `inbox-job-agent-123456`) is what you type in `gcloud` commands. It is not always identical to the display name.

On the OAuth consent screen for that project: if the app is still **Testing**, Google kills the Gmail refresh token after **7 days**. Publish the app (Personal use / External, no verification needed). You will see an “unverified app” warning once; that is your own desktop client reading your own mail.

---

## Step 3 — Install the Google Cloud SDK (one time)

1. Download: <https://cloud.google.com/sdk/docs/install>
2. Run the Windows installer. Leave defaults. Allow it to add `gcloud` to PATH.
3. **Close and reopen PowerShell**, then:

```powershell
gcloud --version
```

If that fails, reopen the terminal again (PATH only updates in new windows).

---

## Step 4 — Log in and turn on APIs

In PowerShell:

```powershell
cd C:\projects\inbox-job-agent

gcloud auth login
```

A browser window opens. Sign in with the **same Google account**, then allow access.

```powershell
gcloud projects list
```

Copy the `PROJECT_ID` for `inbox-job-agent`, then:

```powershell
gcloud config set project YOUR_PROJECT_ID
gcloud config get-value project
```

The second command must print `YOUR_PROJECT_ID`. Then enable the services this deploy uses:

```powershell
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com cloudscheduler.googleapis.com secretmanager.googleapis.com
```

Wait until it finishes (about a minute).

---

## Step 5 — Store Gmail + profile as secrets

The Docker image does **not** contain your token or profile (see `.dockerignore`). Cloud Run reads them from Secret Manager.

```powershell
cd C:\projects\inbox-job-agent

gcloud secrets create gmail-token --data-file=secrets\token.json
gcloud secrets create profile-yaml --data-file=config\profile.yaml
```

If Google says the secret already exists:

```powershell
gcloud secrets versions add gmail-token --data-file=secrets\token.json
gcloud secrets versions add profile-yaml --data-file=config\profile.yaml
```

That `token.json` is the login you already did on this PC. The cloud app reads **the same inbox**. You do not log into Gmail again unless the token expires.

---

## Step 6 — Pick a dashboard password

This is `API_TOKEN`. Anyone who knows it can open the site and trigger a poll.

In PowerShell:

```powershell
-join ((65..90) + (97..122) + (48..57) | Get-Random -Count 24 | ForEach-Object {[char]$_})
```

Copy the result. Do not use commas in it.

---

## Step 7 — Deploy (Google builds the Dockerfile)

Stay in `C:\projects\inbox-job-agent`. Replace `YOUR_API_TOKEN` with the password from step 6.

```powershell
gcloud run deploy inbox-job-agent `
  --source . `
  --region us-east1 `
  --allow-unauthenticated `
  --timeout 900 `
  --memory 1Gi `
  --cpu 1 `
  --max-instances 1 `
  --set-env-vars "LLM_PROVIDER=gemini,GMAIL_QUERY=in:inbox -category:promotions,MIN_JOB_SCORE=0.45,API_TOKEN=YOUR_API_TOKEN" `
  --set-secrets "GMAIL_TOKEN_JSON=gmail-token:latest,PROFILE_YAML=profile-yaml:latest"
```

`--source .` uploads this folder and builds `Dockerfile` in Cloud Build. You never run `docker build` yourself.

The first deploy can take **5–10 minutes**. When it works, it prints a URL:

`https://inbox-job-agent-xxxxx-ue.a.run.app`

Save that URL.

### If deploy fails with a Secret Manager permission error

On **Home** in Cloud Console, copy **Project number** (digits, not the project id). Then:

```powershell
gcloud secrets add-iam-policy-binding gmail-token --member="serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor"
gcloud secrets add-iam-policy-binding profile-yaml --member="serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor"
```

Run the `gcloud run deploy` command again.

---

## Step 8 — Add database URL and API keys (console)

The first deploy only has Gmail + profile + a few flags. Add the rest in the browser so you do not fight quoting in PowerShell.

1. <https://console.cloud.google.com/run>
2. Click the service **inbox-job-agent**.
3. **Edit & deploy new revision**.
4. Open **Variables & secrets**.
5. Add these **environment variables** (plain values, not files):

| Name | Value |
| --- | --- |
| `DATABASE_URL` | Neon string from step 1 (`postgresql+psycopg://...`) |
| `GEMINI_API_KEY` | from your local `.env` |
| `GEMINI_API_KEY_2` | optional second AI Studio key |
| `GROQ_API_KEY` | optional, from `.env` |
| `NVIDIA_API_KEY` | optional |
| `OPENROUTER_API_KEY` | optional |
| `LLM_CHAIN_CLASSIFY` | `gemini,gemini2,groq` if you have two Gemini keys |
| `LLM_CHAIN_EXTRACT` | `gemini,gemini2,groq,nvidia,openrouter` |

Leave `GMAIL_TOKEN_JSON` and `PROFILE_YAML` as secrets from step 7.

6. **Deploy**.

---

## Step 9 — Open the dashboard

1. Open the Cloud Run URL.
2. Log in with the dashboard password (`API_TOKEN`).
3. Open **Inbox**. Use **Start fresh** if you want an empty database (Gmail is not touched). Use **Check new mail** to analyze the inbox one email at a time.

The Jobs page starts empty until a check runs. That is Neon, not your laptop SQLite.

---

## Step 10 — Trigger on each new email

Gmail tells Google Pub/Sub when the inbox changes. Pub/Sub calls `/api/gmail-push`. The app then reads **only new** messages, one by one.

Replace `YOUR_API_TOKEN` with the dashboard password. The service URL is the one `gcloud run deploy` printed.

```powershell
gcloud services enable pubsub.googleapis.com gmail.googleapis.com

gcloud pubsub topics create gmail-inbox

gcloud pubsub topics add-iam-policy-binding gmail-inbox `
  --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" `
  --role="roles/pubsub.publisher"

gcloud pubsub subscriptions create gmail-inbox-push `
  --topic gmail-inbox `
  --push-endpoint "https://inbox-job-agent-244210842384.us-east1.run.app/api/gmail-push?key=YOUR_API_TOKEN" `
  --ack-deadline 600
```

Add the topic name on the Cloud Run service (console → **inbox-job-agent** → **Edit & deploy new revision** → **Variables & secrets**):

| Name | Value |
| --- | --- |
| `GMAIL_PUBSUB_TOPIC` | `projects/inbox-job-agent/topics/gmail-inbox` |

Deploy that revision. Then open **Inbox** and click **Turn on new-mail trigger**.

A backup check every 10 minutes covers a missed push:

```powershell
gcloud scheduler jobs create http inbox-job-agent-poll `
  --location us-east1 `
  --schedule "*/10 * * * *" `
  --time-zone "America/New_York" `
  --uri "https://inbox-job-agent-244210842384.us-east1.run.app/api/run" `
  --http-method POST `
  --headers "x-api-token=YOUR_API_TOKEN" `
  --attempt-deadline 900s
```

GitHub Actions polling stays **off**. Do not turn it back on while this trigger is running.

---

## After you change code later

Merging on GitHub still does nothing to Cloud Run. From this folder, on `main`:

```powershell
git checkout main
git pull origin main
gcloud run deploy inbox-job-agent --source . --region us-east1
```

If you edited `config\profile.yaml` or re-ran Gmail login:

```powershell
gcloud secrets versions add profile-yaml --data-file=config\profile.yaml
gcloud secrets versions add gmail-token --data-file=secrets\token.json
```

Then deploy again (or wait — new secret versions are used on the next revision; a deploy picks them up if the service is bound to `latest`).

---

## If something is wrong

| Symptom | Likely cause |
| --- | --- |
| Deploy asks for billing | Step 2: link a billing account |
| `gcloud` is not recognized | Reopen PowerShell after installing the SDK |
| Secret permission error | Step 7 IAM binding with **project number** |
| Login page, then empty jobs | Neon URL missing or still on SQLite default |
| `invalid_grant` / Gmail auth error | Re-run `python -m app.auth_setup` locally, then update `gmail-token` |
| Token dies after a week | Publish the OAuth consent screen (not Testing) |
| 401 on `/api/run` | Scheduler header `x-api-token` does not match `API_TOKEN` |
| Site works, no new mail | Trigger not turned on, or Gmail query too narrow |

Do not add a GitHub Actions poller while Cloud Run is processing mail, or you will double-process.
