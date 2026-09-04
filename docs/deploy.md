# First-time deploy: Google Cloud Run (Windows)

You already run this app on your laptop against **the same Gmail inbox**. Hosting it means:

1. Google builds the **existing Dockerfile** (you do not install Docker Desktop).
2. The container stays at a public URL (the dashboard).
3. Every 30 minutes Cloud Scheduler calls `POST /api/run`, which reads that same Gmail.
4. After a one-time GitHub secret setup, **push to `main`** deploys the same Docker image.

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
3. **Mail** is the home page: each email, newest first, with its jobs and follow-ups underneath.
4. **Matches** is the same roles grouped by day.
5. **Run** has **Check now** and **Start fresh**. Start fresh does not touch Gmail.

The first Mail page can be empty until a check runs. That is Neon, not your laptop SQLite.

---

## Step 10 — Check Gmail every 30 minutes

This is the whole schedule. No Pub/Sub. Cloud Scheduler calls `POST /api/run`.

Replace `YOUR_API_TOKEN` with the dashboard password:

```powershell
gcloud scheduler jobs create http inbox-job-agent-poll `
  --location us-east1 `
  --schedule "*/30 * * * *" `
  --time-zone "America/New_York" `
  --uri "https://inbox-job-agent-244210842384.us-east1.run.app/api/run" `
  --http-method POST `
  --headers "x-api-token=YOUR_API_TOKEN" `
  --attempt-deadline 900s
```

If the job already exists:

```powershell
gcloud scheduler jobs update http inbox-job-agent-poll `
  --location us-east1 `
  --schedule "*/30 * * * *" `
  --update-headers "x-api-token=YOUR_API_TOKEN"
```

After GitHub deploy is connected (next section), this job is created or updated automatically.

---

## Step 11 — Push to GitHub deploys Cloud Run (once)

GitHub Actions builds the same Dockerfile and runs `gcloud run deploy --source .`. You do this setup **once**.

### 1. Service account

```powershell
gcloud iam service-accounts create github-deploy --display-name="GitHub Cloud Run deploy"

gcloud projects add-iam-policy-binding inbox-job-agent `
  --member="serviceAccount:github-deploy@inbox-job-agent.iam.gserviceaccount.com" `
  --role="roles/run.admin"

gcloud projects add-iam-policy-binding inbox-job-agent `
  --member="serviceAccount:github-deploy@inbox-job-agent.iam.gserviceaccount.com" `
  --role="roles/iam.serviceAccountUser"

gcloud projects add-iam-policy-binding inbox-job-agent `
  --member="serviceAccount:github-deploy@inbox-job-agent.iam.gserviceaccount.com" `
  --role="roles/cloudbuild.builds.editor"

gcloud projects add-iam-policy-binding inbox-job-agent `
  --member="serviceAccount:github-deploy@inbox-job-agent.iam.gserviceaccount.com" `
  --role="roles/artifactregistry.admin"

gcloud projects add-iam-policy-binding inbox-job-agent `
  --member="serviceAccount:github-deploy@inbox-job-agent.iam.gserviceaccount.com" `
  --role="roles/storage.admin"

gcloud projects add-iam-policy-binding inbox-job-agent `
  --member="serviceAccount:github-deploy@inbox-job-agent.iam.gserviceaccount.com" `
  --role="roles/cloudscheduler.admin"

gcloud iam service-accounts keys create "$env:TEMP\github-deploy.json" `
  --iam-account=github-deploy@inbox-job-agent.iam.gserviceaccount.com
```

### 2. GitHub secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Name | Value |
| --- | --- |
| `GCP_SA_KEY` | entire contents of `$env:TEMP\github-deploy.json` |
| `API_TOKEN` | the same dashboard password Cloud Run already uses |

Delete the JSON file after you paste it.

### 3. After that

Every push (or merge) to **`main`** deploys. Profile and Gmail token stay in Secret Manager — they are not in git. To update the profile:

```powershell
gcloud secrets versions add profile-yaml --data-file=config\profile.yaml
gcloud run services update inbox-job-agent --region us-east1 --update-secrets PROFILE_YAML=profile-yaml:latest
```

---

## If something is wrong

| Symptom | Likely cause |
| --- | --- |
| Deploy asks for billing | Step 2: link a billing account |
| `gcloud` is not recognized | Reopen PowerShell after installing the SDK |
| Secret permission error | Step 7 IAM binding with **project number** |
| Login page, then empty mail | Neon URL missing or still on SQLite default |
| `invalid_grant` / Gmail auth error | Re-run `python -m app.auth_setup` locally, then update `gmail-token` |
| Token dies after a week | Publish the OAuth consent screen (not Testing) |
| 401 on `/api/run` | Scheduler header `x-api-token` does not match `API_TOKEN` |
| Site works, no new mail | Scheduler missing, or Gmail query too narrow |
| GitHub deploy fails on auth | `GCP_SA_KEY` secret missing or not the JSON key file |
