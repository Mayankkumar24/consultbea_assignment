# Task 5 — What breaks at 5,000 workers over a weekend

Honest assessment of the **current build** (FastAPI + pydub, Render free
tier, local disk, synchronous processing) — not an idealized system.

## What breaks first: CPU

- Audio decoding (`pydub`/`ffmpeg`) runs synchronously inside the request
  handler, on Render's free 0.1 shared vCPU, single instance, no
  autoscaling.
- This serializes every submission — one file decoding blocks all others.
- Even light concurrency (a few dozen/minute) → latency spikes → Render
  proxy timeouts. Breaks within the first hour of real traffic.

## Storage

- Audio saved to local disk (`static/uploads/`) — **ephemeral** on Render.
  Any restart/redeploy silently deletes all submitted files, no warning to
  the worker.
- No file size/type validation or disk-space check before accepting
  uploads — a few bad files could exhaust disk before 5,000 people finish.
- **Fix:** move to durable object storage (S3/R2/GCS); store only the
  URL/key in Postgres.

## Uploads & processing

- Upload, decode, extract, and DB-write all happen in one request cycle —
  no decoupling.
- Flaky mobile connections (realistic for gig workers) → timeout mid-
  upload → ambiguous state (file half-saved, DB row unclear) → worker
  retries → possible duplicate.
- **Fix:** accept the file fast, return immediately, process in a
  background queue (Celery+Redis or similar). Also fixes the CPU
  bottleneck above.

## Duplicates

- Exact-phone matching (from Task 1/3) correctly catches same-person
  re-submission by phone.
- But: shared household/agent phone numbers → wrongly merges different
  people into one `person_id`.
- Also: matching dedupes *people*, not *submissions* — a retried request
  (same person, same recording) creates a second row in
  `audio_submissions`.
- **Fix:** client-generated idempotency key per submission attempt.

## Database

- Fresh `psycopg2` connection per request, no pooling — risks exhausting
  Postgres's connection limit under concurrency (low to begin with on
  Render's free 256MB Postgres).
- Free Postgres also **expires after 30 days** — fine for the weekend
  itself, risky if data needs querying after.
- **Fix:** connection pooling (pgbouncer/SQLAlchemy pool) + paid,
  persistent Postgres before real launch.

## Failures

- No try/except around audio decoding — a corrupted/unsupported/zero-byte
  file → unhandled 500, with an orphaned file already on disk (DB insert
  never happens).
- Even a 1-2% malformed-upload rate = 50-100 people hitting a broken flow
  with no clear error.
- **Fix:** validate before accepting, wrap decode in try/except with a
  clear user-facing error, clean up orphaned files on failure.

## Cost

- Current setup = demo config, not launch config (free tier, single
  instance, 30-day DB expiry).
- Real launch needs: paid compute instance, object storage, managed
  persistent Postgres, background job runner.
- Rough order of magnitude: $0 → low double-digit $/month at this scale —
  not expensive, but should be a decision, not a surprise.

## Priority — top 3 fixes before launch

1. **Background queue for processing** — fixes CPU bottleneck + timeout/
   retry-duplicates.
2. **Object storage for files** — fixes data loss on restart.
3. **Idempotency keys + connection pooling** — fixes duplicate submissions
   + DB exhaustion under load.