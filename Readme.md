# ConsultBae AI Automation Assignment

## Overview

This repo contains my submission for the ConsultBae take-home assignment:
merging three messy, ID-less data sources into a single Postgres database,
a duplicate-detection automation built in n8n, a mini audio-collection web
app with automatic feature extraction, a full data-quality report, and a
scale/failure analysis for a hypothetical 5,000-user launch.

## What's in this repo

```
.
├── .env                            # Environment variables
├── .gitignore
├── schema.sql                      # Task 1 - database schema
├── load_data.py                    # Task 1 - ETL: normalize, match, load 3 CSVs into Postgres
├── requirements1.txt               # Task 1 dependencies
├── source1_naukri_applicants.csv   # Provided source data
├── source2_gig_workers.csv
├── source3_cbnexus_contacts.csv
├── audio_app/                      # Task 3 - FastAPI audio collection app
│   ├── main.py
│   ├── requirements.txt
│   ├── Dockerfile                  # includes ffmpeg (required by pydub)
│   ├── static/
│   └── templates/
│       ├── index.html              # submission form (record or upload)
│       └── submissions.html        # list view with players + extracted features
├── task4_data_issue_report.md      # Task 4 - full data quality report
├── task5_stretch.md                # Task 5 - scale/failure analysis
└── Readme.md                       # this file
```

*(Task 2's n8n workflow lives in n8n Cloud, not this repo — see below for
details and how to view it.)*

## Task 1 — Database & matching

**Stack:** PostgreSQL (local dev via pgAdmin, deployed to Render Postgres
for the live demo).

**Approach:** since none of the three sources share a common ID, people are
matched using a confidence-based cascade — exact email match, then exact
phone match, then fuzzy name+city match (only used when the first two fail,
and only above a confidence threshold; anything in between gets logged for
review rather than auto-merged). Every match decision is recorded in
`match_log` with its method and confidence score, which is what let me
catch and fix a false-merge bug during manual QA (see Task 4 report,
section 3, for the full story).

**Setup:**
```bash
# 1. Create the schema
psql -d your_db -f schema.sql
psql -d your_db -f schema_patch_v2.sql

# 2. Load the data
pip install -r requirements.txt
cp .env.example .env   # set your DATABASE_URL
python load_data.py --reset
```

**Result:** 42 + 31 + 30 = 103 raw rows across the three sources resolve to
54 unique people (25 appearing in only one source, 13 in two, 16 in all
three).

## Task 2 — n8n automation

**What it does:** a webhook receives a new candidate's name/email/phone,
checks it against the `persons` table for an existing match (same logic as
Task 1's exact-match rules), and either sends an email alert (duplicate
found) or inserts a new person (no match).

**Where to see it:** [n8n workflow link — add your workflow's shareable/
read-only link here before submitting]. Demonstrated live in the Loom
video.

**Why this design:** kept deliberately simple (webhook → Postgres lookup →
conditional branch) rather than building an LLM-based auto-tagging flow,
to keep the automation reliable and explainable within the time budget.

## Task 3 — Audio collection app

**Stack:** FastAPI + vanilla HTML/JS (browser `MediaRecorder` API for
in-browser recording, plus a file-upload fallback), Postgres, deployed on
Render via Docker (ffmpeg is required for audio decoding and isn't present
in Render's native Python buildpack, hence the Dockerfile).

**Extracted per submission:** duration, sample rate, bitrate (estimated
from file size / duration), loudness (dBFS), and a bonus rough noise-floor
estimate (10th-percentile RMS across short windows).

**Person linking:** reuses the phone-normalization logic from Task 1 —
exact phone match against the existing `persons` table, or creates a new
person. Fuzzy matching is deliberately *not* used here, to avoid the same
class of false-merge risk documented in the Task 4 report.

**Run locally:**
```bash
cd audio_app
pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --reload
```

**Live demo:** [Render URL — add your deployed URL here before submitting]

## Task 4 — Data quality report

See [`task4_data_issues_report.md`](./task4_data_issues_report.md) for the
full, specific list of every data issue found (mixed date formats, a
suspect CTC column, two separate column-shuffling corruptions in
source2 — one partial, one a full rotation caught only during manual
verification, a duplicate embedded header row in source3, city-naming
inconsistencies, and a genuinely ambiguous same-name duplicate) — plus a
full account of a matching bug found in my own code during QA (an
index-pollution issue that caused a false merge), how it was diagnosed,
and how it was fixed.

## Task 5 — Scale analysis

See [`task5_scale_answer.md`](./task5_scale_answer.md) for an honest
breakdown of what would actually break if the current Task 3 app were
launched to 5,000 workers over a weekend — the CPU bottleneck from
synchronous audio processing, ephemeral disk storage, unhandled failures,
duplicate-submission risk, and DB connection handling — and the three
changes I'd prioritize before a real launch.

## Stuck log: Where I got stuck & how I got unstuck

**1. Data Matching Logic: Index-Pollution False Merge Bug**
- **The Problem:** During manual QA, I noticed that "Sneha Mishra" was incorrectly merged with "Isha Chopra" despite having different emails and phones.
- **Why I got stuck:** My matching code used a confidence-based cascade (email → phone → fuzzy name/city). At first, I couldn't understand why the fuzzy matcher connected these two distinct names.
- **How I got unstuck:** I debugged by checking my custom `match_log` table. I realized I had an "index pollution" bug: my code was adding a processed row's name/city into the fuzzy-matching index *even when that row had already matched an existing person via email or phone*. This caused unrelated names to accumulate under a single person ID. I fixed this by only populating the fuzzy index when a *brand new* person is created, and raised the auto-merge threshold from 90% to 95% for safety.

**2. Render Deployment: Docker Build Context & Paths**
- **The Problem:** Deploying the FastAPI `audio_app` on Render via Dockerfile failed with: `Error loading ASGI app. Could not import module "main".`
- **Why I got stuck:** My project root has the ETL scripts, while the web app is inside the `audio_app/` folder. When setting up Render, I messed up the Docker paths. I asked AI (Claude) for help, and it initially suggested setting the Build Context to `./audio_app` and Dockerfile Path to `./Dockerfile`. This caused a `no such file or directory` error.
- **How I got unstuck:** Claude searched the official Render docs and corrected itself: *both* fields on Render are independently relative to the repository root. The correct setup was Build Context: `./audio_app` and Dockerfile Path: `./audio_app/Dockerfile`.
- **Follow-up error:** I then got `failed to calculate checksum ... "/requirements.txt": not found`. Claude helped me realize that because my build context was restricted to `./audio_app`, Docker couldn't access the repo root's `requirements.txt`. I fixed this by creating a dedicated `requirements.txt` specifically for the audio app inside the `audio_app/` folder.

**3. n8n Workflow Configuration**
- **The Problem:** I wasn't sure how to pass data and query results effectively between nodes in n8n.
- **How I got unstuck:** I took guidance from Claude on how n8n nodes process queries and how the JSON result from one node is passed to the next node. This short and accurate guidance helped me successfully connect the workflow.

## Known limitations (honest, not hidden)

- The city-alias normalization list covers only the variants observed in
  these three files, not a general gazetteer.
- The row-corruption detector in `load_data.py` only catches column
  rotation when it produces a recognizable status word — a rotation
  producing plausible-but-wrong text would pass through undetected.
- One genuinely ambiguous duplicate (same name + city, different phone,
  source3) is left flagged rather than force-resolved, since the data
  doesn't support a confident decision either way.
- Both Render services (web app + Postgres) run on free tiers for this
  submission — not production-grade; see Task 5 for what changes before a
  real launch.
