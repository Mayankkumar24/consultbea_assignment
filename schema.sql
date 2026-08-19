
-- 1. persons —  record, ONE row per real human
CREATE TABLE persons (
    person_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name    VARCHAR(150),
    canonical_email   VARCHAR(150) UNIQUE,
    canonical_phone   VARCHAR(15) UNIQUE,    
    canonical_city    VARCHAR(100),
    created_at        TIMESTAMP DEFAULT NOW(),
    updated_at        TIMESTAMP DEFAULT NOW()
);


-- 2. source_records — raw original data from each CSV
CREATE TABLE source_records (
    record_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id     UUID REFERENCES persons(person_id),  
    source_name   VARCHAR(50) NOT NULL,      
    raw_data      JSONB NOT NULL,         
    ingested_at   TIMESTAMP DEFAULT NOW()
);

-- 3. skills — normalized looku
CREATE TABLE skills (
    skill_id     SERIAL PRIMARY KEY,
    skill_name   VARCHAR(100) UNIQUE NOT NULL
);


-- 4. person_skills — many-to-many junction
CREATE TABLE person_skills (
    person_id   UUID REFERENCES persons(person_id),
    skill_id    INT REFERENCES skills(skill_id),
    source      VARCHAR(50),
    PRIMARY KEY (person_id, skill_id, source)
);


-- 5. audio_submissions — Task 3 data

CREATE TABLE audio_submissions (
    submission_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id         UUID REFERENCES persons(person_id),
    name              VARCHAR(150),
    phone             VARCHAR(15),
    file_path         TEXT NOT NULL,
    duration_sec      NUMERIC(8,2),
    sample_rate_hz    INT,
    bitrate_kbps      INT,
    loudness_db       NUMERIC(6,2),
    noise_estimate    NUMERIC(6,3), 
    submitted_at      TIMESTAMP DEFAULT NOW()
);

-- 6. match_log — audit trail for dedup decisions
CREATE TABLE match_log (
    log_id           SERIAL PRIMARY KEY,
    person_id        UUID REFERENCES persons(person_id),
    record_id        UUID REFERENCES source_records(record_id),
    match_method      VARCHAR(30),        
    confidence        NUMERIC(4,3),        
    flagged_review    BOOLEAN DEFAULT FALSE,
    logged_at         TIMESTAMP DEFAULT NOW()
);