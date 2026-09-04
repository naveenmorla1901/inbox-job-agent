# Inbox Job Agent

Watches your Gmail, pulls every posting out of job-alert digests, scores each one against your
resume, and pings you the moment a human (recruiter, hiring manager, interview scheduler) actually
writes to you. Everything runs on free tiers — no paid API is required at any point.

```
Gmail API ──> parse MIME ──> classify ──┬─ job_alert ─> extract posting links ─> scrape JD ─> score vs resume ─> SQLite/Postgres ─> dashboard
                                        └─ recruiter / interview / assessment / offer ─> Outreach table ─> Telegram ping
```

## What it actually does

- **Polls Gmail** with the official API (`gmail.readonly`), incrementally, using a stored cursor so
  each run only looks at new mail.
- **Classifies every message** into `job_alert`, `recruiter_outreach`, `interview_invite`,
  `assessment`, `offer`, `rejection`, `application_update`, or `other`. Rules first (free, instant);
  an LLM only refines the ambiguous ones, and only if you enable one.
- **Explodes job digests**: a LinkedIn "8 new jobs" email becomes 8 rows. Click-tracker wrappers
  are unwrapped (including base64 ones) and tracking parameters stripped, so the same posting is
  never stored twice even across different alert emails and different boards.
- **Reads the actual job page** through a fallback chain: board JSON API (Greenhouse, Lever) →
  LinkedIn public guest fragment → `schema.org/JobPosting` JSON-LD → readable HTML → optional LLM
  extraction from raw text. Bot walls and application forms are detected rather than stored as if
  they were the job description, and each row records how it was fetched (`ok`, `blocked`,
  `empty`) and by which extractor.
- **Keeps every posting**, not just the good ones. Matches are flagged; the rest stay searchable
  under "every posting received" so nothing that hit your inbox is lost.
- **Removes duplicates twice over**: by posting ID pulled from the link (the same LinkedIn or
  Greenhouse job in five different alerts is one row), then by company + normalised title, so the
  same role re-advertised on a second board is folded into the first and hidden by default.
- **Tracks applications end to end**: confirmation, assessment, interview, offer, rejection — each
  matched back to the role it belongs to and shown as a timeline.
- **Scores each posting** against `config/profile.yaml`: title fit (40%), weighted skill coverage
  (35%), resume cosine similarity (25%), minus penalties for over-seniority and location mismatch.
  Hard blockers (`security clearance`, excluded titles, …) reject outright.
- **Stores matches** above `MIN_JOB_SCORE` and shows them in a dashboard with save/apply/ignore.
- **Notifies you on Telegram** for recruiter mail, interviews, assessments and offers — the things
  that need a reply today, not tomorrow.

Full click-by-click setup for every integration: [`docs/SETUP.md`](docs/SETUP.md).
Run `python -m app.run doctor` at any time and it tells you what is still missing.

## Quick start (local, 10 minutes)

```bash
git clone <your-fork> inbox-job-agent && cd inbox-job-agent
python -m venv .venv && .venv\Scripts\activate      # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env                               # macOS/Linux: cp
copy config\profile.example.yaml config\profile.yaml
```

1. **Gmail credentials** — [Google Cloud Console](https://console.cloud.google.com/):
   enable the Gmail API → OAuth consent screen (*External*, add your own address as a test user) →
   Credentials → *Create OAuth client ID* → **Desktop app** → download the JSON to
   `secrets/client_secret.json`. Free, no billing account needed.
2. **Authorize once**: `python -m app.auth_setup` — a browser opens, and `secrets/token.json`
   appears. That file holds the refresh token; it is what you paste into a host later.
3. **Edit `config/profile.yaml`**: your target titles, weighted skills, and your resume text. This
   file is the whole brain of the matcher — spend ten minutes here, it pays for itself.
4. **Run it**:

```bash
python -m app.run doctor              # check Gmail, LLM, database, Telegram, scraping
python -m app.run poll                # one pass over new mail
python -m app.run poll --days 1       # everything that arrived today, ignoring the cursor
python -m app.run report --days 1     # how many alerts / interviews / assessments / other
python -m app.run backfill --days 7   # rewind and re-read the last week
python -m app.run serve               # dashboard at http://localhost:8000
python -m app.run loop                # poll forever, every 15 min
```

The dashboard has these pages:

| Page | What it holds |
| --- | --- |
| **Mail** | Each email in time order, with the jobs and follow-ups from that message |
| **Matches** | Roles that fit your profile, grouped by day, with the source email |
| **Follow-ups** | Mail an actual person sent you: recruiters, interview scheduling, assessments, offers |
| **Applications** | One row per role you applied to, with its status and mail timeline |
| **Run** | Check Gmail now, or start fresh |

An application appears automatically when a confirmation email arrives ("thank you for applying to
X at Y"), or the moment you press **applied** on a posting. After that, every interview invite,
assessment, rejection or update from that company is matched back to it and pushes the status
forward — never backwards, so a late "we received your application" cannot undo an interview.

No Gmail set up yet and just want to see the UI? `python -m app.run demo` seeds the database from a
sample alert email.

### Tune the matcher without touching your inbox

```bash
python -m app.run match --title "Machine Learning Engineer" --file some_job.txt
```

Prints the score breakdown, matched skills, and missing skills so you can calibrate weights and
`MIN_JOB_SCORE` before trusting it.

## Hosting

This app runs on **Google Cloud Run**. Google Cloud Build builds the Docker image on each push
to `main`. Secrets stay in GCP. First-time steps: [`docs/deploy.md`](docs/deploy.md).

Test the same image locally:

```powershell
docker build -t inbox-job-agent .
docker run --rm -p 8080:8080 -e PORT=8080 -e API_TOKEN=dev inbox-job-agent
```

`config/profile.yaml` is gitignored. After you edit it locally:

```powershell
gcloud secrets versions add profile-yaml --data-file=config\profile.yaml
gcloud run services update inbox-job-agent --region us-east1 --update-secrets PROFILE_YAML=profile-yaml:latest
```

On Cloud Run, `PROFILE_YAML` and `GMAIL_TOKEN_JSON` replace the local files.

## Where every credential goes

Nothing is hard-coded and nothing but these two files (plus `secrets/`) is personal.

| What | Local file | Hosted equivalent |
| --- | --- | --- |
| Gmail OAuth client | `secrets/client_secret.json` (downloaded from Google Cloud) | not needed after step 2 |
| Gmail refresh token | `secrets/token.json` (created by `python -m app.auth_setup`) | `GMAIL_TOKEN_JSON` secret = the file's contents |
| Which mail to read | `GMAIL_QUERY` in `.env` | env var / Actions variable |
| Your resume, target roles, skills | `config/profile.yaml` | `PROFILE_YAML` secret = the file's contents |
| Score cutoff | `MIN_JOB_SCORE` in `.env` | env var |
| Gemini key ([aistudio.google.com](https://aistudio.google.com/apikey)) | `GEMINI_API_KEY` | `GEMINI_API_KEY` secret |
| Groq key ([console.groq.com](https://console.groq.com/keys)) | `GROQ_API_KEY` | `GROQ_API_KEY` secret |
| NVIDIA NIM ([build.nvidia.com](https://build.nvidia.com)) | `NVIDIA_API_KEY` | `NVIDIA_API_KEY` secret |
| DeepSeek / OpenRouter | `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY` | same names |
| Local model instead | `LLM_PROVIDER=ollama`, `OLLAMA_MODEL` | n/a |
| Telegram alerts | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | same names as secrets |
| Dashboard password | `API_TOKEN` | same name on Cloud Run |
| Database | `DATABASE_URL` (SQLite by default) | `DATABASE_URL` secret for free Postgres |

## Configuration

`.env` (see `.env.example`) controls plumbing; `config/profile.yaml` controls judgment.

| Key | Meaning |
| --- | --- |
| `GMAIL_QUERY` | Gmail search used each poll. Narrow it, e.g. `in:inbox -category:promotions -from:me`. |
| `MIN_JOB_SCORE` | 0–1 cutoff for storing a posting. Start at 0.45, raise once you see the noise level. |
| `SCRAPE_JOB_PAGES` | Fetch each posting page. Off = faster and quieter, but scores rely on the email snippet only. |
| `LLM_PROVIDER` | `none` disables models. Any other value turns on failover across every key you set. |
| `GMAIL_APPLY_LABEL` | Label processed mail in Gmail. Needs the `gmail.modify` scope — re-run `app.auth_setup` after enabling. |
| `API_TOKEN` | Dashboard/API key. Leave as `change-me` for local-only, set it before exposing the app. |

## Layout

```
app/
  auth_setup.py    one-time OAuth flow -> secrets/token.json
  gmail_client.py  Gmail API wrapper (list, get, label)
  email_parse.py   MIME -> text/html/links
  extract_jobs.py  digest -> distinct postings, canonical de-dupe keys
  scrape.py        job page -> title/company/location/description
  classify.py      rules + optional LLM triage
  applications.py  company/role extraction, application matching, status transitions
  matcher.py       resume/profile scoring
  pipeline.py      the whole run, transactional per message
  reporting.py     category breakdown shared by the CLI and the Overview page
  notify.py        Telegram
  main.py          FastAPI dashboard + JSON API
  doctor.py        integration self-check behind `run doctor`
  run.py           CLI: doctor | poll | report | loop | backfill | serve | demo | match
config/profile.example.yaml
tests/
```

## Privacy

Mail is read from your own account with your own OAuth client; nothing is sent anywhere unless you
enable an LLM provider (then only sender, subject and the first 6k characters of ambiguous emails
are sent) or Telegram. The database is yours. Keep `secrets/` and `.env` out of git — `.gitignore`
already does that.

## Tests

```bash
python -m pytest -q
```

Covers digest extraction and de-duplication, scoring tiers and hard blockers, classifier rules,
JSON-LD scraping, and the end-to-end pipeline against a fixture email (no network, no Gmail).
