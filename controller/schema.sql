-- Controller state. One SQLite file per world (see store.py). WAL, synchronous=NORMAL.
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_cursor (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_chunk INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    ts_minute INTEGER NOT NULL,
    leads INTEGER NOT NULL,
    config_version_at_creation INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    lid INTEGER PRIMARY KEY,
    lead_id TEXT NOT NULL UNIQUE,
    person_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    cell INTEGER NOT NULL,
    campaign_id TEXT NOT NULL,
    ts_minute INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS leads_person ON leads (person_id);

-- event_id is the primary key: a replayed event cannot be inserted twice.
CREATE TABLE IF NOT EXISTS calls (
    event_id INTEGER PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    ts_minute INTEGER NOT NULL,
    day INTEGER NOT NULL,
    hour INTEGER NOT NULL,
    lid INTEGER NOT NULL,
    attempt_no INTEGER NOT NULL,
    config_version INTEGER NOT NULL,
    cohort_id TEXT NOT NULL,
    welcome TEXT NOT NULL,
    voice TEXT NOT NULL,
    caller_id TEXT NOT NULL,
    prompt TEXT NOT NULL,
    connected INTEGER NOT NULL,
    engaged INTEGER NOT NULL,
    halluc_flag INTEGER,
    adherence REAL
);
-- calls_lead (lid) is NOT created here: Store.build_calls_index() makes it once history is ingested.
CREATE INDEX IF NOT EXISTS calls_wf_day ON calls (workflow_id, day);

CREATE TABLE IF NOT EXISTS cube (
    workflow_id TEXT NOT NULL,
    day INTEGER NOT NULL,
    hour INTEGER NOT NULL,
    cell INTEGER NOT NULL,
    welcome TEXT NOT NULL,
    voice TEXT NOT NULL,
    caller_id TEXT NOT NULL,
    prompt TEXT NOT NULL,
    dials INTEGER NOT NULL,
    connects INTEGER NOT NULL,
    engaged INTEGER NOT NULL,
    flags INTEGER NOT NULL,
    sum_adherence REAL NOT NULL,
    PRIMARY KEY (workflow_id, day, hour, cell, welcome, voice, caller_id, prompt)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS config_versions (
    workflow_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    actor TEXT,
    ts_minute INTEGER,
    changed_dims TEXT,
    proposal_id TEXT,
    approval_id TEXT,
    body TEXT,
    PRIMARY KEY (workflow_id, version)
);

CREATE TABLE IF NOT EXISTS cohort_defs (
    workflow_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    cohorts TEXT NOT NULL,
    created_minute INTEGER,
    PRIMARY KEY (workflow_id, version)
);

CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    dim TEXT NOT NULL,
    state TEXT NOT NULL,
    started_minute INTEGER,
    body TEXT
);

CREATE TABLE IF NOT EXISTS arms (
    experiment_id TEXT NOT NULL,
    option_id TEXT NOT NULL,
    role TEXT NOT NULL,
    share REAL,
    body TEXT,
    PRIMARY KEY (experiment_id, option_id)
);

CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    approval_id TEXT,
    base_version INTEGER,
    created_minute INTEGER,
    body TEXT,
    route TEXT,
    priority INTEGER,
    plan_seq INTEGER,
    filed_minute INTEGER,
    apply_by_minute INTEGER,
    expires_minute INTEGER,
    supersedes TEXT,
    applied_version INTEGER,
    applied_minute INTEGER
);
CREATE INDEX IF NOT EXISTS proposals_wf ON proposals (workflow_id, state);

CREATE TABLE IF NOT EXISTS proposal_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER,
    proposal_id TEXT,
    approval_id TEXT,
    workflow_id TEXT,
    ts_minute INTEGER,
    event TEXT NOT NULL,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS proposal_events_approval ON proposal_events (approval_id);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sim_minute INTEGER NOT NULL,
    workflow_id TEXT,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    inputs TEXT
);
CREATE INDEX IF NOT EXISTS decisions_wf ON decisions (workflow_id, sim_minute);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    opened_minute INTEGER,
    closed_minute INTEGER,
    body TEXT
);

CREATE TABLE IF NOT EXISTS validation_runs (
    run_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    n INTEGER,
    pass_rate REAL,
    credits REAL,
    sim_minute INTEGER
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT,
    sim_minute INTEGER,
    credits REAL,
    request TEXT,
    response TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    name TEXT NOT NULL,
    workflow_id TEXT NOT NULL DEFAULT '',
    last_run_minute INTEGER NOT NULL,
    PRIMARY KEY (name, workflow_id)
);

CREATE TABLE IF NOT EXISTS detector_state (
    detector TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (detector, key)
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sim_minute INTEGER,
    workflow_id TEXT,
    severity TEXT NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tick_phases (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sim_minute INTEGER NOT NULL, reconcile_s REAL, ingest_s REAL,
    apply_s REAL, detect_s REAL, plan_s REAL, observer_s REAL
);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sim_minute INTEGER NOT NULL,
    events_ingested INTEGER NOT NULL,
    ingest_wall_s REAL NOT NULL,
    lag_s REAL NOT NULL,
    tick_wall_s REAL NOT NULL,
    step_minutes INTEGER NOT NULL
);

-- Per completed day arm counts for the experiment tests (derived from calls; safe to drop).
CREATE TABLE IF NOT EXISTS exp_day_cache (
    workflow_id TEXT NOT NULL,
    day INTEGER NOT NULL,
    col TEXT NOT NULL,
    cells TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (workflow_id, day, col, cells)
);
