"""
ConsultBae assignment - Task 1
Loads source1 (Naukri), source2 (Gig Workers), source3 (CBNexus) CSVs
into Postgres, deduplicating people across sources using a
confidence-based matching cascade (exact email > exact phone > fuzzy name+city).

Requirements:
    pip install pandas psycopg2-binary rapidfuzz python-dotenv

Usage:
    python load_data.py
"""

import os
import re
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

FUZZY_AUTO_MERGE_THRESHOLD = 90
FUZZY_REVIEW_THRESHOLD = 75

CITY_ALIASES = {
    "gurugram": "gurgaon",
    "delhi ncr": "delhi",
    "new delhi": "delhi",
}

# In-memory indices built up as we process all 3 sources in one pass
email_index = {}      # normalized_email -> person_id
phone_index = {}       # normalized_phone -> person_id
name_city_index = []    # list of (person_id, normalized_name, normalized_city)

stats = {"exact_email": 0, "exact_phone": 0, "fuzzy_name_city": 0, "new_person": 0, "flagged_review": 0}


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def normalize_phone(raw):
    """Strip everything but digits, keep last 10 digits, prefix +91."""
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
    """source1 Applied Date has multiple formats: 24-07-2026, 2026-08-08,
    7 Jul 2026, 08/19/2026 -- try a list of formats before giving up."""
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


def clean_skills(raw):
    """Turns 'n8n, LangChain, REST APIs' into ['n8n', 'langchain', 'rest apis']."""
    if pd.isna(raw) or str(raw).strip() == "":
        return []
    parts = [p.strip().lower() for p in str(raw).split(",")]
    return [p for p in parts if p]


def fix_ctc(row_ctc, row_experience):
    """Data issue: some rows have CTC values like 5.1 / 11.2 (looks like
    experience-in-years accidentally landed in the CTC column, or CTC was
    entered in lakhs instead of rupees). Flag anything under 1000 as suspect
    rather than silently guessing -- store as-is but mark for the report."""
    try:
        val = float(row_ctc)
    except (TypeError, ValueError):
        return None, True
    if val < 1000:
        # very likely lakhs (e.g. 5.1 -> 510000) or a data entry error.
        # We store the raw value AND flag it -- do not silently multiply.
        return val, True
    return val, False


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def find_or_create_person(cur, name, email, phone, city):
    email_n = normalize_email(email)
    phone_n = normalize_phone(phone)
    city_n = normalize_city(city)
    name_n = normalize_name(name)

    # 1. exact email
    if email_n and email_n in email_index:
        pid = email_index[email_n]
        stats["exact_email"] += 1
        return pid, "exact_email", 1.0, False

    # 2. exact phone
    if phone_n and phone_n in phone_index:
        pid = phone_index[phone_n]
        stats["exact_phone"] += 1
        return pid, "exact_phone", 1.0, False

    # 3. fuzzy name + city (city must match, name similarity scored)
    best_score, best_pid = 0, None
    if name_n and city_n:
        for pid, ex_name, ex_city in name_city_index:
            if ex_city == city_n:
                score = fuzz.ratio(name_n, ex_name)
                if score > best_score:
                    best_score, best_pid = score, pid

    if best_score >= FUZZY_AUTO_MERGE_THRESHOLD:
        pid = best_pid
        stats["fuzzy_name_city"] += 1
        method, conf, flag = "fuzzy_name_city", best_score / 100, False
    elif best_score >= FUZZY_REVIEW_THRESHOLD:
        pid = best_pid
        stats["fuzzy_name_city"] += 1
        stats["flagged_review"] += 1
        method, conf, flag = "fuzzy_name_city", best_score / 100, True
    else:
        # 4. no match -> new person
        pid = str(uuid.uuid4())
        cur.execute(
            """INSERT INTO persons (person_id, canonical_name, canonical_email,
               canonical_phone, canonical_city)
               VALUES (%s, %s, %s, %s, %s)""",
            (pid, name_n, email_n, phone_n, city_n),
        )
        stats["new_person"] += 1
        method, conf, flag = "new_person", None, False

    if email_n:
        email_index[email_n] = pid
    if phone_n:
        phone_index[phone_n] = pid
    if name_n and city_n:
        name_city_index.append((pid, name_n, city_n))

    return pid, method, conf, flag


def log_match(cur, person_id, record_id, method, confidence, flagged):
    cur.execute(
        """INSERT INTO match_log (person_id, record_id, match_method, confidence, flagged_review)
           VALUES (%s, %s, %s, %s, %s)""",
        (person_id, record_id, method, confidence, flagged),
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
    """Naukri applicants: Full Name, Email, Phone, City, Experience (Years),
    Current CTC, Applied Date, Skills"""
    df = pd.read_csv(SOURCE1_PATH)
    df = df.dropna(how="all")

    for _, row in df.iterrows():
        name, email, phone, city = row["Full Name"], row["Email"], row["Phone"], row["City"]
        pid, method, conf, flag = find_or_create_person(cur, name, email, phone, city)

        ctc_clean, ctc_flagged = fix_ctc(row.get("Current CTC"), row.get("Experience (Years)"))

        raw = row.to_dict()
        raw["_ctc_flagged_suspect"] = ctc_flagged  # keep the flag inside raw_data for the report

        record_id = insert_source_record(cur, pid, "naukri", raw)
        log_match(cur, pid, record_id, method, conf, flag)
        upsert_skills(cur, pid, clean_skills(row.get("Skills")), "naukri")

    print(f"source1 (naukri): {len(df)} rows processed")


def load_source2(cur):
    """Gig workers: email_id, worker_name, rate, location, status, skill_tags"""
    df = pd.read_csv(SOURCE2_PATH)
    df = df.dropna(how="all")  # drops the fully-blank row

    fixed_rows = []
    for _, row in df.iterrows():
        row = row.copy()
        # Data issue: one row has columns shuffled -- email lands in a
        # non-email column and vice versa. Detect and repair before use.
        values = row.to_dict()
        email_col_val = str(values.get("email_id", ""))
        if "@" not in email_col_val:
            # find whichever column actually holds the email
            for col, val in values.items():
                if isinstance(val, str) and "@" in val:
                    # swap: put the real email into email_id, push the
                    # wrongly-placed value back into its likely column
                    values["email_id"], values[col] = values[col], values["email_id"]
                    break
        fixed_rows.append(values)

    for values in fixed_rows:
        name, email, phone_or_none, city = (
            values.get("worker_name"),
            values.get("email_id"),
            None,  # source2 has no phone column
            values.get("location"),
        )
        pid, method, conf, flag = find_or_create_person(cur, name, email, phone_or_none, city)

        record_id = insert_source_record(cur, pid, "gig_workers", values)
        log_match(cur, pid, record_id, method, conf, flag)
        upsert_skills(cur, pid, clean_skills(values.get("skill_tags")), "gig_workers")

    print(f"source2 (gig_workers): {len(fixed_rows)} rows processed")


def load_source3(cur):
    """CBNexus contacts: Name, Phone Number, City, Verified, Projects Completed
    Data issue: header row is duplicated mid-file -- must be dropped."""
    df = pd.read_csv(SOURCE3_PATH)
    df = df.dropna(how="all")
    # drop any row that is actually a repeated header
    df = df[df["Name"] != "Name"]

    for _, row in df.iterrows():
        name, phone, city = row["Name"], row["Phone Number"], row["City"]
        pid, method, conf, flag = find_or_create_person(cur, name, None, phone, city)

        record_id = insert_source_record(cur, pid, "cbnexus", row.to_dict())
        log_match(cur, pid, record_id, method, conf, flag)

    print(f"source3 (cbnexus): {len(df)} rows processed")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
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
