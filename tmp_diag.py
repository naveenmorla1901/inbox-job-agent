"""Read-only diagnosis: emails whose raw extract has candidates but no stored Job rows."""
import json
import os
import subprocess

from sqlalchemy import create_engine, text

env = subprocess.run(
    [
        "gcloud", "run", "services", "describe", "inbox-job-agent",
        "--region", "us-east1", "--project", "inbox-job-agent",
        "--format=value(spec.template.spec.containers[0].env)",
    ],
    capture_output=True, text=True, shell=True,
).stdout

url = ""
for chunk in env.split("};"):
    if "DATABASE_URL" in chunk:
        url = chunk.split("'value':")[1].strip().strip("'} \n")
if not url:
    raise SystemExit("could not read DATABASE_URL")

engine = create_engine(url)
with engine.connect() as conn:
    rows = conn.execute(text("""
        SELECT m.id, m.category, m.email_type, m.jobs_found, m.sender_email,
               left(m.subject, 70) AS subject,
               length(coalesce(m.body_text, '')) AS body_chars,
               m.extract_json,
               (SELECT count(*) FROM job j WHERE j.message_id = m.id) AS job_rows
        FROM message m
        WHERE m.jobs_found > 0
        ORDER BY m.received_at DESC
        LIMIT 300
    """)).fetchall()

print(f"{len(rows)} recent messages with jobs_found > 0\n")
print("--- raw extract has candidates but ZERO job rows stored ---")
bad = 0
by_category: dict[str, int] = {}
for r in rows:
    payload = json.loads(r.extract_json or "{}")
    cands = payload.get("candidates", [])
    if cands and r.job_rows == 0:
        bad += 1
        by_category[r.category] = by_category.get(r.category, 0) + 1
        sources = sorted({c.get("source") or "(none)" for c in cands})
        titled = sum(1 for c in cands if (c.get("title") or "").strip())
        urls = sum(1 for c in cands if (c.get("url") or "").strip())
        if bad <= 15:
            print(
                f"{r.category:<20} cands={len(cands):<3} titled={titled:<3} urls={urls:<3} "
                f"body={r.body_chars:<6} src={sources} | {r.subject}"
            )
print(f"\ntotal affected: {bad}")
print("by category:", by_category)

print("\n--- messages with an empty stored body ---")
empty = [r for r in rows if r.body_chars == 0]
for r in empty[:10]:
    print(f"{r.category:<20} body=0 jobs_found={r.jobs_found} rows={r.job_rows} | {r.subject}")
print(f"total with empty body: {len(empty)} of {len(rows)}")
