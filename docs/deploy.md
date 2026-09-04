# First-time deploy: Google Cloud Run (Windows)

You already run this app on your laptop against **the same Gmail inbox**. Hosting it means:

1. Google builds the **existing Dockerfile** (you do not install Docker Desktop).
2. The container stays at a public URL (the dashboard).
3. Every 30 minutes Cloud Scheduler calls `POST /api/run`, which reads that same Gmail.
4. After a one-time **Connect GitHub** in Cloud Run, each push to `main` builds that Docker image and deploys it. Secrets stay in GCP — GitHub does not need a Google key.

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

After GitHub is connected in Cloud Run (next section), each push to `main` rebuilds the Docker image. The 30-minute poller is separate and already lives in GCP.

---

## Step 11 — Push to GitHub deploys the Docker image (once)

Do **not** put a Google key in GitHub. Secrets already on Cloud Run stay there.

Do **not** click **Connect** on the Cloud Run **Overview** page. That button creates a **second** service (`inbox-job-agent-git`, often in `europe-west1`) with **no secrets**. Your real dashboard is already:

`https://inbox-job-agent-244210842384.us-east1.run.app`

Service name **`inbox-job-agent`**, region **`us-east1`**. Open **that** service, then **Set up continuous deployment**.

You have **two logins**. Keep both. Do not try to make them the same.

| Where | Account | What it is |
| --- | --- | --- |
| Google Cloud | `naveen.morla04@gmail.com` | Project `inbox-job-agent`, Cloud Run, secrets |
| GitHub | user **`naveenmorla1901`** | Repo `inbox-job-agent` |

Google Cloud never “sees” the GitHub email. It only needs you to **install Google’s GitHub app** on the GitHub user that **owns the repo**. That is a popup. In that popup you must be `naveenmorla1901`, not the GCP Gmail.

### A. Before you click anything

1. Open a **new Incognito / InPrivate window** (stops Chrome from using the wrong GitHub login).
2. In that window, open [github.com](https://github.com) and sign in as **`naveenmorla1901`**. Confirm you can open [github.com/naveenmorla1901/inbox-job-agent](https://github.com/naveenmorla1901/inbox-job-agent).
3. Leave that GitHub tab signed in. Do not log in to GitHub as `naveen.morla04@gmail.com` in this window.

### B. Cloud Run (still your GCP Gmail)

1. In the **same Incognito window**, open [console.cloud.google.com](https://console.cloud.google.com) and sign in as **`naveen.morla04@gmail.com`**.
2. Top bar project: **inbox-job-agent**.
3. Open [Cloud Run → inbox-job-agent](https://console.cloud.google.com/run/detail/us-east1/inbox-job-agent?project=inbox-job-agent) — the row whose region is **us-east1**, not `inbox-job-agent-git`.
4. Click **Set up continuous deployment**. If you do not see it: the three-dot menu on **that** service. Do **not** use the Overview page **Connect** button (that creates `inbox-job-agent-git`).
5. If it asks Cloud Build vs Developer Connect, pick **Developer Connect** or **Cloud Build** (either works). Click **Connect** / **Authenticate**.

### C. The GitHub popup (this is the confusing part)

Google opens a GitHub window. Look at the **GitHub username in the top-right**. It must say **`naveenmorla1901`**.

- If it shows some other user, or a GitHub account created from `naveen.morla04@gmail.com`: click **Install the GitHub App on another GitHub account** (wording may be **Switch account**). Sign in as **`naveenmorla1901`**.
- Choose **Only select repositories** → pick **`inbox-job-agent`** only.
- Click green **Install** / **Authorize**.

Back in Cloud Run you should now see repository **`naveenmorla1901/inbox-job-agent`**. If the repo list is empty, you installed the app on the wrong GitHub user. Repeat C.

### D. Finish the form

| Field | Value |
| --- | --- |
| Repository | `naveenmorla1901/inbox-job-agent` |
| Branch | `main` (or `^main$`) |
| Build type | **Dockerfile**, or **Cloud Build configuration file** `/cloudbuild.yaml` |
| Source directory | `/` (leave default) |

Save. Do not change env vars or secrets on this screen if it shows them.

### E. Check it worked

1. Cloud Run service page → **Revisions**: a new revision after the next push to `main`.
2. Or Cloud Build → **History**: a green build.

You can delete GitHub secrets `GCP_SA_KEY` and `API_TOKEN`. They are not used for this.

If the repo still does not appear: GitHub → your user **`naveenmorla1901`** → **Settings** → **Applications** → **Installed GitHub Apps** → Google Cloud Build or Developer Connect → **Configure** → grant **`inbox-job-agent`**.

### F. If the build fails with `fetchReadToken` / 403

GitHub is already connected. Cloud Build just cannot **read** the repo until you grant one IAM role. This is **not** the Gmail vs GitHub email issue.

The error looks like:

`Permission 'developerconnect.gitRepositoryLinks.fetchReadToken' denied`

Do this in PowerShell (same GCP login as before):

```powershell
gcloud config set project inbox-job-agent

gcloud projects add-iam-policy-binding inbox-job-agent `
  --member="serviceAccount:244210842384-compute@developer.gserviceaccount.com" `
  --role="roles/developerconnect.readTokenAccessor"

gcloud projects add-iam-policy-binding inbox-job-agent `
  --member="serviceAccount:244210842384@cloudbuild.gserviceaccount.com" `
  --role="roles/developerconnect.readTokenAccessor"

gcloud projects add-iam-policy-binding inbox-job-agent `
  --member="serviceAccount:service-244210842384@gcp-sa-cloudbuild.iam.gserviceaccount.com" `
  --role="roles/developerconnect.readTokenAccessor"
```

Or in the browser: [IAM & Admin](https://console.cloud.google.com/iam-admin/iam?project=inbox-job-agent) → **Grant access** → paste each of those three emails → role **Developer Connect Read Token Accessor** → Save.

Then retry (do not reconnect GitHub):

1. Open [Cloud Build → History](https://console.cloud.google.com/cloud-build/builds?project=inbox-job-agent).
2. Open the failed build → **Retry**, or open **Triggers** → the `cloudrun-inbox-job-agent-...` trigger → **Run**.

A green build should then **Pull → Build → Push → Deploy**. Your Cloud Run service stays in **us-east1**; the trigger name can say `europe-west1` because that is where Developer Connect stored the GitHub link. That is fine.

### G. Check now says “No Gmail token” (two Cloud Run services)

Connecting GitHub from Overview created a second website. Only the first one has Gmail.

| Service | Region | URL | Use it? |
| --- | --- | --- | --- |
| `inbox-job-agent` | **us-east1** | https://inbox-job-agent-244210842384.us-east1.run.app | **Yes** — Gmail, Neon, password |
| `inbox-job-agent-git` | europe-west1 | `…europe-west1.run.app` | **No** — empty copy from Connect |

1. Open **only** https://inbox-job-agent-244210842384.us-east1.run.app and log in with the dashboard password.
2. If **Check now** still says no Gmail token on that URL, the secret is not attached. In PowerShell:

```powershell
gcloud config set project inbox-job-agent
gcloud run services update inbox-job-agent --region us-east1 --update-secrets "GMAIL_TOKEN_JSON=gmail-token:latest,PROFILE_YAML=profile-yaml:latest"
```

Or in the browser: Cloud Run → **inbox-job-agent** (us-east1) → **Edit & deploy new revision** → **Variables & secrets** → **Reference a secret** → secret `gmail-token` → **Exposed as environment variable** named `GMAIL_TOKEN_JSON` → version **latest** → Deploy.

3. Delete the extra service so you do not open it again:

```powershell
gcloud run services delete inbox-job-agent-git --region europe-west1
```

In the console: Cloud Run → **inbox-job-agent-git** → Delete.

Do **not** copy secrets onto `inbox-job-agent-git`. Keep one service in **us-east1**.

If the secret `gmail-token` was never created, on the laptop:

```powershell
gcloud secrets create gmail-token --data-file=secrets\token.json
```

If it already exists:

```powershell
gcloud secrets versions add gmail-token --data-file=secrets\token.json
```

Then run the `gcloud run services update` command above.

### Same image on this PC

```powershell
docker build -t inbox-job-agent .
docker run --rm -p 8080:8080 -e PORT=8080 -e API_TOKEN=dev inbox-job-agent
```

Open http://localhost:8080 — that is the Cloud Run container, locally.

Profile still is not in git. After you edit `config\profile.yaml`:

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
| Check now: `No Gmail token` | You opened `inbox-job-agent-git` (europe-west1). Use the us-east1 URL. Attach secret `gmail-token` as `GMAIL_TOKEN_JSON` (Step 11 G). |
| `invalid_grant` / Gmail auth error | Re-run `python -m app.auth_setup` locally, then update `gmail-token` |
| Token dies after a week | Publish the OAuth consent screen (not Testing) |
| 401 on `/api/run` | Scheduler header `x-api-token` does not match `API_TOKEN` |
| Site works, no new mail | Scheduler missing, or Gmail query too narrow |
| GitHub connect does not list the repo | You authorized the GitHub user for the GCP Gmail. Install the app on **`naveenmorla1901`**. Use Incognito. Click **Install on another GitHub account**. |
| Build fails in ~10s with `fetchReadToken` / 403 | GitHub is connected. Grant **Developer Connect Read Token Accessor** to the Cloud Build accounts (Step 11 F), then retry the build. |
| GitHub “Node.js 20 is deprecated” | Harmless warning. The Docker workflow does not use that Google action. |
