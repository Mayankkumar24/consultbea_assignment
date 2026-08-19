"""
ConsultBae assignment - Task 1 (v2 - fixed)
Fixes applied after manual verification:
  1. name_city_index no longer gets polluted by matched rows -- only a
     brand-new person adds an entry. This was the root cause of the
     "Sneha Mishra" false-merge (index had accumulated unrelated name
     variants under an existing person_id, causing unrelated rows to
     fuzzy-match against noise instead of the real canonical name).
  2. Thresholds tightened: auto-merge 95 (was 90), review floor 82 (was 75).
  3. match_log now stores WHICH existing person's name/record triggered a
     fuzzy match, so future audits don't require manually eyeballing JSONB.
  4. --reset flag truncates all tables first, for clean re-runs.

Requirements:
    pip install pandas psycopg2-binary rapidfuzz python-dotenv

Usage:
    python load_data.py --reset
"""

import os
import re
import sys
import uuid
import pandas as pd
from rapidfuzz import fuzz
import psycopg2
from psycopg2.extras import Json
from dotenv import load_dotenv

load_dotenv()

DB_URL = os.getenv("DATABASE_URL", "postgresql://postgres:yourpassword@localhost:5432/consultbae_assignment")

SOURCE1_PATH = "source1_naukri_applicants.csv"
SOURCE2_PATH = "source2_gig_workers.csv"
SOURCE3_PATH = "source3_cbnexus_contacts.csv"

# Tightened after the Sneha Mishra false-merge review
FUZZY_AUTO_MERGE_THRESHOLD = 95
FUZZY_REVIEW_THRESHOLD = 82

CITY_ALIASES = {
    "gurugram": "gurgaon",
    "delhi ncr": "delhi",
    "new delhi": "delhi",
}

# In-memory indices built up as we process all 3 sources in one pass
email_index = {}       # normalized_email -> person_id
phone_index = {}        # normalized_phone -> person_id
name_city_index = []     # list of (person_id, normalized_name, normalized_city)
                          # IMPORTANT: only populated when a NEW person is created.
                          # Matched rows do NOT add entries here anymore --
                          # that was the pollution bug.

stats = {"exact_email": 0, "exact_phone": 0, "fuzzy_name_city": 0, "new_person": 0, "flagged_review": 0}


# ---------------------------------------------------------------------------
# Normalization helpers (unchanged, already verified against real data)
# ---------------------------------------------------------------------------

def normalize_phone(raw):
    if pd.isna(raw) or str(raw).strip() == "":
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) < 10:
        return None
    return "+91" + digits[-10:]


def normalize_email(raw):
    if pd.isna(raw) or str(raw).strip() == "":
        return None
    return str(raw).strip().lower()


def normalize_city(raw):
    if pd.isna(raw) or str(raw).strip() == "":
        return None
    c = str(raw).strip().lower()
    c = CITY_ALIASES.get(c, c)
    return c.title()


def normalize_name(raw):
    if pd.isna(raw) or str(raw).strip() == "":
        return None
    return str(raw).strip().title()


def parse_date_flexible(raw):
    if pd.isna(raw) or str(raw).strip() == "":
        return None
    raw = str(raw).strip()
    formats = ["%d-%m-%Y", "%Y-%m-%d", "%d %b %Y", "%m/%d/%Y", "%d/%m/%Y"]
    for fmt in formats:
        try:
            return pd.to_datetime(raw, format=fmt).date()
        except ValueError:
            continue
    try:
        return pd.to_datetime(raw).date()
    except Exception:
        return None

STATUS_VALUES = {"active", "inactive", "yes", "no", "verified", "pending"}

def looks_like_valid_skills(raw_skill_tags):
    """Row corruption check: if the 'skill_tags' field actually contains
    a status word instead of tech skills, the row's columns are rotated."""
    if pd.isna(raw_skill_tags):
        return True
    cleaned = str(raw_skill_tags).strip().lower()
    return cleaned not in STATUS_VALUES


def clean_skills(raw):
    if pd.isna(raw) or str(raw).strip() == "":
        return []
    parts = [p.strip().lower() for p in str(raw).split(",")]
    return [p for p in parts if p]


def fix_ctc(row_ctc, row_experience):
    try:
        val = float(row_ctc)
    except (TypeError, ValueError):
        return None, True
    if val < 1000:
        return val, True
    return val, False


# ---------------------------------------------------------------------------
# Matching (fixed)
# ---------------------------------------------------------------------------

def find_or_create_person(cur, name, email, phone, city):
    """Returns (person_id, method, confidence, flagged_review, matched_against_name, matched_against_pid)"""
    email_n = normalize_email(email)
    phone_n = normalize_phone(phone)
    city_n = normalize_city(city)
    name_n = normalize_name(name)

    # 1. exact email
    if email_n and email_n in email_index:
        pid = email_index[email_n]
        stats["exact_email"] += 1
        return pid, "exact_email", 1.0, False, None

    # 2. exact phone
    if phone_n and phone_n in phone_index:
        pid = phone_index[phone_n]
        stats["exact_phone"] += 1
        return pid, "exact_phone", 1.0, False, None

    # 3. fuzzy name + city -- ONLY compares against names that came from
    #    brand-new-person creation, never against names added via a prior
    #    match. This is the fix: no more index pollution.
    best_score, best_pid, best_matched_name = 0, None, None
    if name_n and city_n:
        for pid, ex_name, ex_city in name_city_index:
            if ex_city == city_n:
                score = fuzz.ratio(name_n, ex_name)
                if score > best_score:
                    best_score, best_pid, best_matched_name = score, pid, ex_name

    if best_score >= FUZZY_AUTO_MERGE_THRESHOLD:
        pid = best_pid
        stats["fuzzy_name_city"] += 1
        method, conf, flag = "fuzzy_name_city", best_score / 100, False
        # still record what it matched against, for auditability
        return pid, method, conf, flag, best_matched_name

    elif best_score >= FUZZY_REVIEW_THRESHOLD:
        pid = best_pid
        stats["fuzzy_name_city"] += 1
        stats["flagged_review"] += 1
        method, conf, flag = "fuzzy_name_city", best_score / 100, True
        return pid, method, conf, flag, best_matched_name

    else:
        # 4. no match -> new person. This is the ONLY place that adds to
        # name_city_index -- prevents future pollution.
        pid = str(uuid.uuid4())
        cur.execute(
            """INSERT INTO persons (person_id, canonical_name, canonical_email,
               canonical_phone, canonical_city)
               VALUES (%s, %s, %s, %s, %s)""",
            (pid, name_n, email_n, phone_n, city_n),
        )
        stats["new_person"] += 1
        if email_n:
            email_index[email_n] = pid
        if phone_n:
            phone_index[phone_n] = pid
        if name_n and city_n:
            name_city_index.append((pid, name_n, city_n))
        return pid, "new_person", None, False, None

    # NOTE: exact_email / exact_phone matches deliberately do NOT touch
    # name_city_index or email_index/phone_index with the incoming row's
    # own values beyond what's already there -- the canonical values stay
    # as set at person-creation time. This is intentional: it keeps the
    # index clean and traceable to one source of truth per person.


def log_match(cur, person_id, record_id, method, confidence, flagged, matched_against_name):
    cur.execute(
        """INSERT INTO match_log (person_id, record_id, match_method, confidence,
           flagged_review, matched_against_name)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (person_id, record_id, method, confidence, flagged, matched_against_name),
    )


def insert_source_record(cur, person_id, source_name, raw_row_dict):
    record_id = str(uuid.uuid4())
    cur.execute(
        """INSERT INTO source_records (record_id, person_id, source_name, raw_data)
           VALUES (%s, %s, %s, %s)""",
        (record_id, person_id, source_name, Json(raw_row_dict)),
    )
    return record_id


def upsert_skills(cur, person_id, skill_list, source_name):
    for skill in skill_list:
        cur.execute(
            "INSERT INTO skills (skill_name) VALUES (%s) ON CONFLICT (skill_name) DO NOTHING",
            (skill,),
        )
        cur.execute("SELECT skill_id FROM skills WHERE skill_name = %s", (skill,))
        skill_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO person_skills (person_id, skill_id, source)
               VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
            (person_id, skill_id, source_name),
        )


# ---------------------------------------------------------------------------
# Source-specific loaders
# ---------------------------------------------------------------------------

def load_source1(cur):
    df = pd.read_csv(SOURCE1_PATH)
    df = df.dropna(how="all")

    for _, row in df.iterrows():
        name, email, phone, city = row["Full Name"], row["Email"], row["Phone"], row["City"]
        pid, method, conf, flag, matched_name = find_or_create_person(cur, name, email, phone, city)

        ctc_clean, ctc_flagged = fix_ctc(row.get("Current CTC"), row.get("Experience (Years)"))
        raw = row.to_dict()
        raw["_ctc_flagged_suspect"] = ctc_flagged

        record_id = insert_source_record(cur, pid, "naukri", raw)
        log_match(cur, pid, record_id, method, conf, flag, matched_name)
        upsert_skills(cur, pid, clean_skills(row.get("Skills")), "naukri")

    print(f"source1 (naukri): {len(df)} rows processed")


def load_source2(cur):
    df = pd.read_csv(SOURCE2_PATH)
    df = df.dropna(how="all")

    fixed_rows = []
    for _, row in df.iterrows():
        values = row.to_dict()
        email_col_val = str(values.get("email_id", ""))
        if "@" not in email_col_val:
            for col, val in values.items():
                if isinstance(val, str) and "@" in val:
                    values["email_id"], values[col] = values[col], values["email_id"]
                    break
        fixed_rows.append(values)

    for values in fixed_rows:
        name, email, phone_or_none, city = (
            values.get("worker_name"),
            values.get("email_id"),
            None,
            values.get("location"),
        )
        pid, method, conf, flag, matched_name = find_or_create_person(cur, name, email, phone_or_none, city)

        row_corrupted = not looks_like_valid_skills(values.get("skill_tags"))
        values["_row_corrupted"] = row_corrupted

        record_id = insert_source_record(cur, pid, "gig_workers", values)
        log_match(cur, pid, record_id, method, conf, flag, matched_name)

        if not row_corrupted:
            upsert_skills(cur, pid, clean_skills(values.get("skill_tags")), "gig_workers")

    print(f"source2 (gig_workers): {len(fixed_rows)} rows processed")


def load_source3(cur):
    df = pd.read_csv(SOURCE3_PATH)
    df = df.dropna(how="all")
    df = df[df["Name"] != "Name"]

    for _, row in df.iterrows():
        name, phone, city = row["Name"], row["Phone Number"], row["City"]
        pid, method, conf, flag, matched_name = find_or_create_person(cur, name, None, phone, city)

        record_id = insert_source_record(cur, pid, "cbnexus", row.to_dict())
        log_match(cur, pid, record_id, method, conf, flag, matched_name)

    print(f"source3 (cbnexus): {len(df)} rows processed")


# ---------------------------------------------------------------------------
# Reset helper
# ---------------------------------------------------------------------------

def reset_tables(cur):
    print("Resetting tables (TRUNCATE)...")
    cur.execute("""
        TRUNCATE TABLE match_log, person_skills, audio_submissions,
                       source_records, skills, persons
        RESTART IDENTITY CASCADE;
    """)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        if "--reset" in sys.argv:
            reset_tables(cur)

        load_source1(cur)
        load_source2(cur)
        load_source3(cur)
        conn.commit()
        print("\nAll sources loaded and committed successfully.")
    except Exception as e:
        conn.rollback()
        print(f"\nERROR - rolled back transaction: {e}")
        raise
    finally:
        cur.close()
        conn.close()

    print("\n--- Match summary ---")
    for k, v in stats.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()