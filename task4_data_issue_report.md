# Task 4 — Data Quality Issues Report

This report documents every data quality issue found across the three source
files (`source1_naukri_applicants.csv`, `source2_gig_workers.csv`,
`source3_cbnexus_contacts.csv`) during ingestion, matching, and manual
verification, along with exactly what was done about each one. Issues are
grouped by where they were found: per-file structural issues, cross-file
matching issues, and bugs discovered in my own matching logic during manual
QA (included here in full, since debugging them was part of understanding
the data).

---

## 1. Per-file structural issues

### 1.1 source1_naukri_applicants.csv

**Inconsistent date formats in `Applied Date`**
The column contained at least four different date formats in the same
column: `24-07-2026` (DD-MM-YYYY), `2026-08-08` (YYYY-MM-DD), `7 Jul 2026`
(D Mon YYYY), and `08/19/2026` (MM/DD/YYYY).
*Fix:* wrote a `parse_date_flexible()` function that tries each known format
in sequence before giving up. Verified against real values pulled from the
file — all four formats parse correctly.

**Suspect values in `Current CTC`**
Most rows have CTC as a full annual figure (e.g. `417964`, `472935`), but a
subset of rows have small decimal values (e.g. `5.1`, `11.2`) that don't fit
a realistic CTC — these look like either the `Experience (Years)` value
leaking into the CTC column, or CTC entered in lakhs instead of full rupees.
*Fix:* did **not** guess which explanation was correct. Wrote a `fix_ctc()`
function that flags any CTC value under 1000 as `_ctc_flagged_suspect: true`
in the stored raw JSON, without altering or "correcting" the number. This
keeps the original (possibly wrong) value auditable rather than silently
multiplying or reassigning it.

**Phone number format inconsistency**
Values appeared as `9000000254`, `+919000000254`, `09000000254`, and
`919000000254` — four different representations of the same 10-digit
number.
*Fix:* `normalize_phone()` strips all non-digit characters, keeps the last
10 digits, and prefixes `+91`. This was unit-tested against real values from
the file before being used in the matching pipeline.

**City name inconsistency (also present across other files — see 2.1)**
`Noida `, `NOIDA`, `noida` (case/whitespace only) as well as `New Delhi` vs
`Delhi NCR` vs `Gurgaon` vs `Gurugram` (genuinely different labels for the
same city). Covered in the cross-file section since this affects matching
across all three sources.

### 1.2 source2_gig_workers.csv

**One fully blank row**
A row with no values in any column.
*Fix:* `df.dropna(how="all")` removes it before processing.

**Partial column-shuffle (one row, two columns swapped)**
One row had `email_id` and `skill_tags` swapped — the skills value
(`"react, javascript, mysql"`) was in the `email_id` column and the actual
email was in the `skill_tags` column.
*Fix:* a repair check detects when `email_id` doesn't contain `@`, scans the
other columns for one that does, and swaps them back. This was the first
corruption pattern found and fixed.

**Full column-rotation (one row, all six columns rotated except email)**
A second, more severe corruption was found later during manual verification
of a specific person's linked records: **all six columns were rotated**
(`worker_name` held skills, `rate` held the name, `location` held the rate,
`skill_tags` held the status, `status` held the city) — only `email_id`
happened to still contain a valid email, so the earlier partial-shuffle
repair (which only checks the email column) did not catch or fix this row.
*Impact discovered:* because `email_id` was correct, the row still matched
to the right person (Isha Chopra) via exact email — matching was not
affected. But `skill_tags` for this row actually contained the word
`"active"` (a status value, not a skill), which got inserted into the
`skills` table as a real, bogus skill entry (`"active"`, count 1), attached
to Isha Chopra.
*Fix:* added a `looks_like_valid_skills()` check — if the `skill_tags`
field contains a known status word (`active`, `inactive`, `yes`, `no`,
`verified`, `pending`) instead of a plausible skills string, the row is
flagged `_row_corrupted: true` in its stored raw JSON and its skills are
**not** extracted (the raw row is still stored in full for audit purposes —
nothing is deleted, only the bogus skill-insert step is skipped). Verified
after the fix: the `"active"` bogus skill entry that had already been
inserted was removed, and re-running the pipeline no longer creates it.
*Known limitation:* this detector only catches rotation when the resulting
`skill_tags` value happens to match a known status word. A rotation that
produces plausible-looking but wrong skills text would not be caught. This
is called out explicitly as a limitation rather than claimed as fully
solved.

**Email casing inconsistency**
Some emails appeared uppercase (`ISHA.CHOPRA95@MAILTEST.EXAMPLE.ORG`).
*Fix:* `normalize_email()` lowercases and strips whitespace before matching.

**No phone number column in this source**
Unlike source1 and source3, this file has no phone field, so matching for
rows from this source can only use email or fuzzy name+city — never exact
phone. Noted as a structural limitation of the source, not something to
"fix," but relevant to why some gig_workers rows land in the fuzzy-match
bucket more often.

### 1.3 source3_cbnexus_contacts.csv

**Duplicate header row embedded mid-file**
The column header row (`Name,Phone Number,City,Verified,Projects
Completed`) appears a second time as data, at row 16 of the file — not just
at the top.
*Fix:* after loading, any row where `Name == "Name"` (i.e. matches the
header value exactly) is dropped. Verified by direct row count before/after
(31 → 30 rows) and confirmed the correct row was removed.

**Genuine same-name, same-city, different-phone duplicate**
Two rows for **"Arjun Mehta"**, both in **Noida**, but with **different
phone numbers** (`+91-9000000131` and `9000000272`). With no shared ID
field, this is genuinely ambiguous — it could be the same person with two
phone numbers on file, or two different people who happen to share a common
name and city.
*Fix / decision:* my matching cascade (email → phone → fuzzy name+city)
will merge these into one person via the fuzzy name+city path if their
similarity score clears the auto-merge threshold, since no phone/email
match exists to arbitrate. This is flagged here as a **known, accepted
limitation** rather than silently resolved — there is no reliable way to
disambiguate this pair from the data available, and this is explicitly
called out rather than presented as a confident merge.

---

## 2. Cross-file issues

### 2.1 City naming inconsistency across all three sources

The same real city was written differently depending on the source and the
person entering the data:
- `New Delhi`, `Delhi NCR`, `Delhi`, `new delhi` → all the same city
- `Gurgaon`, `Gurugram`, `gurugram ` (trailing space) → all the same city
- Plain case/whitespace variance elsewhere (`Noida `, `NOIDA`, `noida`)

*Fix:* `normalize_city()` lowercases, strips whitespace, and applies an
explicit alias map (`{"gurugram": "gurgaon", "delhi ncr": "delhi", "new
delhi": "delhi"}`) before title-casing the result. This was a deliberate
judgment call — the alias list is not exhaustive and was built from the
variants actually observed in these three files, not a general-purpose
Indian-city gazetteer. A city variant not in this list will pass through
unmerged.

### 2.2 No common unique identifier across the three sources

None of the files share a common ID field, which is the core challenge of
Task 1. We used a confidence-based matching cascade instead of a single join
key:
1. Exact match on normalized email (highest confidence)
2. Exact match on normalized phone
3. Fuzzy match on normalized name + normalized city (lowest confidence,
   used only when the first two fail)

Every match decision — including which method was used and the confidence
score — is logged in a `match_log` table, specifically so that low-confidence
merges can be reviewed rather than trusted blindly. This logging is what
allowed the two matching bugs below to be caught during manual verification.

### 2.3 Phone number format inconsistency across sources

Beyond the within-file inconsistency noted in 1.1, the exact same
normalization had to hold across source1 and source3 for phone-based
matching to work at all (e.g. `09000000260` in source1 vs `919000000260` in
source3 for the same person, Rahul Malhotra — both normalize to
`+919000000260` and correctly matched via `exact_phone`).

---

## 3. Bugs found in my own matching logic during manual verification

These aren't data issues in the source files — they're bugs in my matching
code that were only caught by manually spot-checking flagged/low-confidence
matches, and are included here in the interest of a fully honest report.

### 3.1 Index-pollution bug causing a false merge (Isha Chopra ↔ Sneha Mishra)

**What happened:** my first matching implementation appended a processed
row's own name/city into the fuzzy-matching index **even when that row had
already matched an existing person via email or phone** — not only when a
genuinely new person was created. Over the course of a run, this let
unrelated name variants accumulate under an existing person's ID. A later,
completely unrelated row for **"Sneha Mishra"** (different email, different
phone, only sharing city = Pune) ended up fuzzy-matching at 75% confidence
against noise in the index tied to **Isha Chopra's** person record — a
clearly incorrect merge (direct name similarity between "Isha Chopra" and
"Sneha Mishra" is only ~52%, confirmed by direct calculation).

**How it was caught:** by manually reviewing every row in `match_log` where
`flagged_review = TRUE` (my review-confidence band, 75–89%) and cross-
checking the linked `raw_data` against the canonical person it had been
merged into.

**Fix:**
- The fuzzy-match index (`name_city_index`) is now populated **only** when a
  brand-new person is created — never on a match via any method. This
  removes the pollution path entirely.
- Auto-merge threshold raised from 90% to 95%; the "flag for review" floor
  raised from 75% to 82%, for extra safety margin.
- `match_log` now stores `matched_against_name` — the specific name string
  that triggered a fuzzy match — so future audits don't require manually
  reading raw JSON to trace a decision.

**Verification after fix:** re-ran the full pipeline (`--reset`); Sneha
Mishra and Isha Chopra are now correctly separate persons, and the fuzzy-
match rerun produced 0 flagged (uncertain) matches out of 6 total fuzzy
matches, all at ≥95% confidence.

### 3.2 Duplicate gig_workers row for the same person, caused by the full-column-rotation issue (1.2)

Cross-referenced with 1.2 above — while investigating the index-pollution
bug, manual review also surfaced that Isha Chopra had two `gig_workers`
source records instead of one: the genuine row, and the fully-rotated
corrupted row. This led directly to discovering and fixing the column-
rotation issue described in 1.2.

---

## 4. Summary table

| # | Issue | File(s) | Type | Resolution |
|---|---|---|---|---|
| 1 | Inconsistent date formats | source1 | Format | Multi-format parser |
| 2 | Suspect low CTC values | source1 | Suspect data | Flagged, not altered |
| 3 | Phone format inconsistency | source1, source3 | Format | Normalized to +91XXXXXXXXXX |
| 4 | Blank row | source2 | Structural | Dropped |
| 5 | Partial column shuffle (1 row) | source2 | Corruption | Detected + repaired |
| 6 | Full column rotation (1 row) | source2 | Corruption | Detected, skills extraction skipped, raw data preserved |
| 7 | Email casing | source2 (and others) | Format | Lowercased |
| 8 | No phone field in source | source2 | Structural limitation | Noted, not fixable |
| 9 | Duplicate embedded header row | source3 | Structural | Dropped |
| 10 | Same-name/city, different-phone duplicate | source3 | Ambiguous | Flagged as known limitation, not force-resolved |
| 11 | City naming inconsistency | All 3 | Format | Alias map + normalization |
| 12 | No common ID field | All 3 | Structural (core challenge) | Cascading match: email → phone → fuzzy name+city, all logged |
| 13 | Index-pollution false-merge bug | Matching logic (not source data) | Code bug | Root-caused, fixed, re-verified |

---

## 5. What was explicitly *not* fixed, and why

- The city alias map (2.1) is not exhaustive — it covers only the variants
  observed in these three files.
- The corrupted-row detector (1.2) only catches rotation when it produces a
  recognizable status word in the skills field; a rotation producing
  plausible-but-wrong text would pass through undetected.
- The Arjun Mehta same-name/city/different-phone case (1.3) is left as a
  logged, flagged ambiguity rather than force-resolved one way or the
  other, since no data exists to confidently decide it either way.

These are called out deliberately rather than omitted, in line with the
brief's instruction to be specific about what was found and done — including
the limits of what automated matching can responsibly resolve without a
shared identifier.