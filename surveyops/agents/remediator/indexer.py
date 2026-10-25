"""
Code Indexer for Remediator using pgvector in surveyops_postgres.
Chunks repository python files via AST, generates 768-dim embeddings, and performs cosine search.
"""

from __future__ import annotations

import ast
import hashlib
import logging
import math
import os
from pathlib import Path
from typing import Any, Optional

import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("surveyops.remediator.indexer")

EMBEDDING_DIM = 768


def get_db_url() -> str:
    """Returns database connection URL for surveyops_postgres."""
    default_url = os.getenv("SURVEYOPS_POSTGRES_URL")
    if default_url:
        return default_url

    host = os.getenv("SURVEYOPS_POSTGRES_HOST", "localhost")
    port = os.getenv("SURVEYOPS_POSTGRES_PORT", "5434" if host in ("localhost", "127.0.0.1") else "5432")
    user = os.getenv("SURVEYOPS_POSTGRES_USER", "postgres")
    password = os.getenv("SURVEYOPS_POSTGRES_PASSWORD", "postgres")
    db = os.getenv("SURVEYOPS_POSTGRES_DB", "surveyops")

    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def init_embeddings_table(db_url: Optional[str] = None) -> None:
    """Initializes the code_embeddings table and pgvector extension."""
    url = db_url or get_db_url()
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS code_embeddings (
                    id SERIAL PRIMARY KEY,
                    file_path VARCHAR(255) NOT NULL,
                    chunk_type VARCHAR(64) NOT NULL,
                    symbol_name VARCHAR(128),
                    start_line INT,
                    end_line INT,
                    code_content TEXT NOT NULL,
                    embedding vector({EMBEDDING_DIM}),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_code_embeddings_file ON code_embeddings(file_path);
                """
            )
        conn.commit()
    finally:
        conn.close()


def generate_deterministic_embedding(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """Generates a deterministic normalized unit vector from SHA256 of text for testing/fallback."""
    h = hashlib.sha256(text.encode("utf-8")).digest()
    vec = []
    for i in range(dim):
        byte_val = h[i % len(h)]
        val = (byte_val - 128.0) / 128.0 + math.sin(i + 1)
        vec.append(val)
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def embed_text(text: str) -> list[float]:
    """Generates 768-dim embedding using Gemini API with deterministic fallback."""
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if api_key:
        try:
            from google import genai
            client = genai.Client(api_key=api_key)
            response = client.models.embed_content(
                model="gemini-embedding-001",
                contents=text,
                config={"output_dimensionality": EMBEDDING_DIM},
            )
            if response.embeddings and response.embeddings[0].values:
                return [float(v) for v in response.embeddings[0].values]
        except Exception as exc:
            logger.warning(f"Gemini embedding API failed ({exc}), falling back to deterministic embedding.")

    return generate_deterministic_embedding(text)


class CodeChunk(ast.NodeVisitor):
    """Extracts functions and classes from Python source code."""

    def __init__(self, file_path: str, lines: list[str]) -> None:
        self.file_path = file_path
        self.lines = lines
        self.chunks: list[dict[str, Any]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._add_node_chunk(node, "function", node.name)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._add_node_chunk(node, "async_function", node.name)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._add_node_chunk(node, "class", node.name)
        self.generic_visit(node)

    def _add_node_chunk(self, node: ast.AST, chunk_type: str, name: str) -> None:
        start_line = getattr(node, "lineno", 1)
        end_line = getattr(node, "end_lineno", start_line)
        content = "\n".join(self.lines[start_line - 1 : end_line])
        self.chunks.append(
            {
                "file_path": self.file_path,
                "chunk_type": chunk_type,
                "symbol_name": name,
                "start_line": start_line,
                "end_line": end_line,
                "code_content": content,
            }
        )


def chunk_file(file_path: Path, repo_root: Path) -> list[dict[str, Any]]:
    """Chunks a python file into structured code chunks."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception:
        return []

    lines = content.splitlines()
    rel_path = str(file_path.relative_to(repo_root))

    chunks: list[dict[str, Any]] = []
    # Always include module-level header chunk
    header_lines = lines[: min(40, len(lines))]
    chunks.append(
        {
            "file_path": rel_path,
            "chunk_type": "module_header",
            "symbol_name": file_path.name,
            "start_line": 1,
            "end_line": len(header_lines),
            "code_content": "\n".join(header_lines),
        }
    )

    try:
        tree = ast.parse(content, filename=str(file_path))
        extractor = CodeChunk(rel_path, lines)
        extractor.visit(tree)
        chunks.extend(extractor.chunks)
    except Exception as exc:
        logger.debug(f"AST parsing skipped for {file_path}: {exc}")

    return chunks


def index_repository(repo_dir: Path, db_url: Optional[str] = None, force: bool = False) -> int:
    """
    Indexes all python service and test files in repo_dir into pgvector.
    Returns total chunks indexed.
    """
    init_embeddings_table(db_url)
    url = db_url or get_db_url()

    # Check if already indexed
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            if not force:
                cur.execute("SELECT COUNT(*) FROM code_embeddings;")
                count = cur.fetchone()[0]
                if count > 0:
                    logger.info(f"Repository already indexed with {count} chunks.")
                    return count
            else:
                cur.execute("DELETE FROM code_embeddings;")
                conn.commit()

        # Find python files in service directories
        services = ["orders", "payment", "inventory", "auth", "tests"]
        all_chunks: list[dict[str, Any]] = []

        for svc in services:
            svc_dir = repo_dir / svc
            if not svc_dir.exists():
                continue
            for py_file in svc_dir.glob("**/*.py"):
                if "__pycache__" in py_file.parts or ".pytest_cache" in py_file.parts:
                    continue
                chunks = chunk_file(py_file, repo_dir)
                all_chunks.extend(chunks)

        if not all_chunks:
            return 0

        # Compute embeddings and insert
        data = []
        for c in all_chunks:
            embedding = embed_text(f"{c['file_path']} {c['symbol_name']}\n{c['code_content']}")
            data.append(
                (
                    c["file_path"],
                    c["chunk_type"],
                    c["symbol_name"],
                    c["start_line"],
                    c["end_line"],
                    c["code_content"],
                    embedding,
                )
            )

        with conn.cursor() as cur:
            query = """
            INSERT INTO code_embeddings (
                file_path, chunk_type, symbol_name, start_line, end_line, code_content, embedding
            ) VALUES %s;
            """
            execute_values(cur, query, data)
        conn.commit()
        logger.info(f"Successfully indexed {len(data)} chunks into pgvector.")
        return len(data)
    finally:
        conn.close()


def search_code(query: str, top_k: int = 5, db_url: Optional[str] = None) -> list[dict[str, Any]]:
    """Performs cosine similarity search using pgvector."""
    query_vector = embed_text(query)
    url = db_url or get_db_url()

    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            sql = """
            SELECT file_path, chunk_type, symbol_name, start_line, end_line, code_content,
                   1 - (embedding <=> %s::vector) AS similarity
            FROM code_embeddings
            ORDER BY embedding <=> %s::vector
            LIMIT %s;
            """
            cur.execute(sql, (query_vector, query_vector, top_k))
            rows = cur.fetchall()
            return [
                {
                    "file_path": r[0],
                    "chunk_type": r[1],
                    "symbol_name": r[2],
                    "start_line": r[3],
                    "end_line": r[4],
                    "code_content": r[5],
                    "similarity": float(r[6]),
                }
                for r in rows
            ]
    finally:
        conn.close()
