import os
from typing import Optional
import psycopg2
from psycopg2.extras import execute_values
from surveyops.schemas.failure_modes import FailureMode
from surveyops.schemas.propositions import Proposition


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
            query = """
            INSERT INTO propositions (
                proposition_id,
                endpoint,
                failure_mode,
                justification,
                predicted_signal,
                graph_edge_ref
            ) VALUES %s
            ON CONFLICT (proposition_id) DO UPDATE SET
                endpoint = EXCLUDED.endpoint,
                failure_mode = EXCLUDED.failure_mode,
                justification = EXCLUDED.justification,
                predicted_signal = EXCLUDED.predicted_signal,
                graph_edge_ref = EXCLUDED.graph_edge_ref;
            """
            data = [
                (
                    p.proposition_id,
                    p.endpoint,
                    p.failure_mode.value if isinstance(p.failure_mode, FailureMode) else str(p.failure_mode),
                    p.justification,
                    p.predicted_signal,
                    p.graph_edge_ref,
                )
                for p in propositions
            ]
            execute_values(cur, query, data)
        conn.commit()
        return len(propositions)
    finally:
        conn.close()


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
            query = "SELECT proposition_id, endpoint, failure_mode, justification, predicted_signal, graph_edge_ref FROM propositions"
            conditions = []
            params = []
            if endpoint:
                conditions.append("endpoint = %s")
                params.append(endpoint)
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
                results.append(
                    Proposition(
                        proposition_id=r[0],
                        endpoint=r[1],
                        failure_mode=FailureMode(r[2]),
                        justification=r[3],
                        predicted_signal=r[4],
                        graph_edge_ref=r[5],
                    )
                )
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
