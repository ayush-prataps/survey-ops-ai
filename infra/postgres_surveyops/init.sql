-- SurveyOps internal database (pgvector enabled)
-- Isolated from target application database.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS propositions (
    proposition_id VARCHAR(64) PRIMARY KEY,
    endpoint VARCHAR(128) NOT NULL,
    failure_mode VARCHAR(64) NOT NULL,
    justification TEXT NOT NULL,
    predicted_signal TEXT NOT NULL,
    graph_edge_ref VARCHAR(255) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_propositions_endpoint ON propositions(endpoint);
CREATE INDEX IF NOT EXISTS idx_propositions_failure_mode ON propositions(failure_mode);

-- ── Observability & Demo Events ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS events (
    id SERIAL PRIMARY KEY,
    incident_id VARCHAR(128) NOT NULL,
    ts TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    component VARCHAR(64) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_incident_id ON events(incident_id);
CREATE INDEX IF NOT EXISTS idx_events_id ON events(id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

-- ── Least-Privilege Role for Surface Mapper ──────────────────────────────────
-- The Surface Mapper should ONLY access the propositions table.
-- It must NOT be able to access incidents, the evidence ledger, or runtime state tables.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'surface_mapper') THEN
        CREATE ROLE surface_mapper WITH LOGIN PASSWORD 'mapper_secret';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE surveyops TO surface_mapper;
GRANT USAGE ON SCHEMA public TO surface_mapper;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM surface_mapper;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON TABLE propositions TO surface_mapper;
