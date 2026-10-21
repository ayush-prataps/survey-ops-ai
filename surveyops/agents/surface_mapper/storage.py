import json
import os
from typing import Optional
import psycopg2
from psycopg2.extras import execute_values
from surveyops.schemas.failure_modes import FailureMode
from surveyops.schemas.propositions import Proposition, CheckableSignal


def get_surveyops_db_url() -> str:
    """Returns database connection URL for SurveyOps internal postgres."""
    default_url = os.getenv("SURVEYOPS_POSTGRES_URL")
    if default_url:
        return default_url

    host = os.getenv("SURVEYOPS_POSTGRES_HOST", "localhost")
    port = os.getenv("SURVEYOPS_POSTGRES_PORT", "5434" if host in ("localhost", "127.0.0.1") else "5432")
    user = os.getenv("SURVEYOPS_POSTGRES_USER", "surface_mapper")
    password = os.getenv("SURVEYOPS_POSTGRES_PASSWORD", "mapper_secret")
    db = os.getenv("SURVEYOPS_POSTGRES_DB", "surveyops")

    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def store_propositions(
    propositions: list[Proposition],
    db_url: Optional[str] = None,
) -> int:
    """
    Upserts propositions into the SurveyOps propositions table.
    Returns the count of propositions stored.
    """
    if not propositions:
        return 0

    url = db_url or get_surveyops_db_url()
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("ALTER TABLE propositions ADD COLUMN IF NOT EXISTS checkable_signal JSONB;")
            except Exception:
                conn.rollback()
            query = """
            INSERT INTO propositions (
                proposition_id,
                endpoint,
                failure_mode,
                justification,
                predicted_signal,
                graph_edge_ref,
                checkable_signal
            ) VALUES %s
            ON CONFLICT (proposition_id) DO UPDATE SET
                endpoint = EXCLUDED.endpoint,
                failure_mode = EXCLUDED.failure_mode,
                justification = EXCLUDED.justification,
                predicted_signal = EXCLUDED.predicted_signal,
                graph_edge_ref = EXCLUDED.graph_edge_ref,
                checkable_signal = EXCLUDED.checkable_signal;
            """
            data = [
                (
                    p.proposition_id,
                    p.endpoint,
                    p.failure_mode.value if isinstance(p.failure_mode, FailureMode) else str(p.failure_mode),
                    p.justification,
                    p.predicted_signal,
                    p.graph_edge_ref,
                    json.dumps(p.checkable_signal.model_dump()) if p.checkable_signal else None,
                )
                for p in propositions
            ]
            execute_values(cur, query, data)
        conn.commit()
        return len(propositions)
    finally:
        conn.close()


def normalize_endpoint(endpoint: str | None) -> str:
    """Normalizes endpoint by stripping HTTP method prefixes (e.g., 'POST /orders' -> '/orders')."""
    if not endpoint:
        return ""
    parts = endpoint.strip().split()
    return parts[-1] if len(parts) > 1 else parts[0]


def load_propositions(
    endpoint: Optional[str] = None,
    failure_mode: Optional[str] = None,
    db_url: Optional[str] = None,
) -> list[Proposition]:
    """Loads propositions from SurveyOps database with optional filtering."""
    url = db_url or get_surveyops_db_url()
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE propositions ADD COLUMN IF NOT EXISTS checkable_signal JSONB;")
            query = "SELECT proposition_id, endpoint, failure_mode, justification, predicted_signal, graph_edge_ref, checkable_signal FROM propositions"
            conditions = []
            params = []
            norm_ep = normalize_endpoint(endpoint) if endpoint else None
            if endpoint and norm_ep:
                conditions.append("(endpoint = %s OR endpoint = %s OR endpoint LIKE %s)")
                params.extend([endpoint, f"POST {norm_ep}", f"%{norm_ep}"])
            if failure_mode:
                conditions.append("failure_mode = %s")
                params.append(failure_mode)

            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY proposition_id"

            cur.execute(query, params)
            rows = cur.fetchall()

            results = []
            for r in rows:
                sig = None
                if len(r) > 6 and r[6]:
                    try:
                        sig_data = json.loads(r[6]) if isinstance(r[6], str) else r[6]
                        sig = CheckableSignal(**sig_data)
                    except Exception:
                        pass
                p = Proposition(
                    proposition_id=r[0],
                    endpoint=r[1],
                    failure_mode=FailureMode(r[2]),
                    justification=r[3],
                    predicted_signal=r[4],
                    graph_edge_ref=r[5],
                    checkable_signal=sig,
                )
                if norm_ep and normalize_endpoint(p.endpoint) != norm_ep:
                    continue
                results.append(p)
            return results
    finally:
        conn.close()


def clear_propositions(db_url: Optional[str] = None) -> None:
    """Clears all propositions from the database."""
    url = db_url or get_surveyops_db_url()
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM propositions;")
        conn.commit()
    finally:
        conn.close()
