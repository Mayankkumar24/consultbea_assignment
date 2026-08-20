"""
ConsultBae assignment - Task 3
Mini audio collection app.

- GET  /              -> submission form (record via browser OR upload file)
- POST /submit         -> saves audio, extracts features, links to persons table
- GET  /submissions     -> list view with players + extracted properties

Person-linking reuses the same idea as Task 1: exact phone match against
the existing `persons` table (built by load_data.py). If no match, a new
person is created -- same logic, just simpler since this form only
collects name + phone (no email/city).

Requirements: fastapi, uvicorn, python-multipart, psycopg2-binary,
              jinja2, pydub, numpy, python-dotenv
              + ffmpeg must be installed on the system (pydub depends on it)

Run locally:
    uvicorn main:app --reload
"""

import os
import re
import uuid
from datetime import datetime

import numpy as np
import psycopg2
from dotenv import load_dotenv
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydub import AudioSegment

load_dotenv()

DB_URL = os.getenv("DATABASE_URL", "postgresql://postgres:yourpassword@localhost:5432/consultbae_assignment")
UPLOAD_DIR = "static/uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def get_conn():
    return psycopg2.connect(DB_URL)


# ---------------------------------------------------------------------------
# Reuse the same normalization used in Task 1 -- keeps matching consistent
# ---------------------------------------------------------------------------

def normalize_phone(raw):
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) < 10:
        return None
    return "+91" + digits[-10:]


def normalize_name(raw):
    return str(raw).strip().title() if raw else None


def find_or_create_person(cur, name, phone):
    """Simple version of Task 1's matcher: this form only has name+phone,
    so we match on exact phone only. No fuzzy fallback here -- if phone
    doesn't match anyone, treat as a new person (safer than guessing off
    name alone, since name-only matching is the risky path we already
    saw cause false merges in Task 1)."""
    phone_n = normalize_phone(phone)
    name_n = normalize_name(name)

    if phone_n:
        cur.execute("SELECT person_id FROM persons WHERE canonical_phone = %s", (phone_n,))
        row = cur.fetchone()
        if row:
            return row[0]

    person_id = str(uuid.uuid4())
    cur.execute(
        """INSERT INTO persons (person_id, canonical_name, canonical_phone)
           VALUES (%s, %s, %s)""",
        (person_id, name_n, phone_n),
    )
    return person_id


# ---------------------------------------------------------------------------
# Audio feature extraction -- tested against real .wav and .webm files
# ---------------------------------------------------------------------------

def extract_audio_features(file_path):
    audio = AudioSegment.from_file(file_path)

    duration_sec = len(audio) / 1000.0
    sample_rate_hz = audio.frame_rate
    file_size_bytes = os.path.getsize(file_path)
    bitrate_kbps = round((file_size_bytes * 8) / duration_sec / 1000, 2) if duration_sec > 0 else None
    loudness_db = round(audio.dBFS, 2) if audio.dBFS != float("-inf") else None

    # Bonus: rough noise-floor estimate. We split the signal into small
    # windows, compute RMS (in dBFS) per window, and take the 10th
    # percentile as a proxy for the "quietest"/background-noise level --
    # a full spectral noise estimate is overkill for this assignment.
    samples = np.array(audio.get_array_of_samples()).astype(np.float32)
    if audio.channels > 1:
        samples = samples.reshape((-1, audio.channels)).mean(axis=1)

    window = 1024
    rms_windows = []
    for i in range(0, max(len(samples) - window, 0), window):
        chunk = samples[i:i + window]
        rms = np.sqrt(np.mean(chunk ** 2)) + 1e-9
        rms_windows.append(20 * np.log10(rms / 32768.0))
    noise_estimate_db = round(float(np.percentile(rms_windows, 10)), 2) if rms_windows else None

    return {
        "duration_sec": round(duration_sec, 2),
        "sample_rate_hz": sample_rate_hz,
        "bitrate_kbps": bitrate_kbps,
        "loudness_db": loudness_db,
        "noise_estimate_db": noise_estimate_db,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def form_page(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.post("/submit")
async def submit_audio(
    name: str = Form(...),
    phone: str = Form(...),
    audio_file: UploadFile = File(...),
):
    ext = os.path.splitext(audio_file.filename or "")[1] or ".webm"
    safe_filename = f"{uuid.uuid4()}{ext}"
    file_path = os.path.join(UPLOAD_DIR, safe_filename)

    with open(file_path, "wb") as f:
        f.write(await audio_file.read())

    features = extract_audio_features(file_path)

    conn = get_conn()
    cur = conn.cursor()
    try:
        person_id = find_or_create_person(cur, name, phone)
        cur.execute(
            """INSERT INTO audio_submissions
               (person_id, name, phone, file_path, duration_sec, sample_rate_hz,
                bitrate_kbps, loudness_db, noise_estimate, submitted_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                person_id, name, phone, file_path,
                features["duration_sec"], features["sample_rate_hz"],
                features["bitrate_kbps"], features["loudness_db"],
                features["noise_estimate_db"], datetime.now(),
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()

    return RedirectResponse(url="/submissions", status_code=303)


@app.get("/submissions", response_class=HTMLResponse)
async def list_submissions(request: Request):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT name, phone, file_path, duration_sec, sample_rate_hz,
                  bitrate_kbps, loudness_db, noise_estimate, submitted_at
           FROM audio_submissions
           ORDER BY submitted_at DESC"""
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    submissions = [
        {
            "name": r[0], "phone": r[1], "file_path": r[2],
            "duration_sec": r[3], "sample_rate_hz": r[4],
            "bitrate_kbps": r[5], "loudness_db": r[6],
            "noise_estimate": r[7], "submitted_at": r[8],
        }
        for r in rows
    ]
    return templates.TemplateResponse(
        request=request, name="submissions.html", context={"submissions": submissions}
    )
