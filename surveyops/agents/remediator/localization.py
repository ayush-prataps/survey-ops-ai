"""
Localization module for Remediator.
Combines DiagnosisResult.graph_edge_ref with pgvector semantic index and reports the determining signal.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from surveyops.schemas import DiagnosisResult
from surveyops.agents.remediator.schemas import LocalizationResult
from surveyops.agents.remediator.indexer import search_code

logger = logging.getLogger("surveyops.remediator.localization")


def extract_file_from_graph_edge(edge_ref: Optional[str]) -> Optional[str]:
    """Extracts candidate target file from graph_edge_ref string."""
    if not edge_ref:
        return None

    edge_lower = edge_ref.lower()
    # Check if a direct file path is mentioned
    file_match = re.search(r"([a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+\.py)", edge_ref)
    if file_match:
        return file_match.group(1)

    # Detect service name from graph edge
    if "payment" in edge_lower:
        return "payment/main.py"
    elif "orders" in edge_lower:
        return "orders/main.py"
    elif "inventory" in edge_lower:
        return "inventory/main.py"
    elif "auth" in edge_lower:
        return "auth/main.py"

    return None


def localize_target_file(
    diagnosis: DiagnosisResult,
    repo_dir: Optional[Path] = None,
    db_url: Optional[str] = None,
) -> LocalizationResult:
    """
    Localizes the file to patch by starting from graph_edge_ref and confirming with pgvector.
    Reports the exact signal used:
    - 'graph_edge_ref (confirmed by pgvector)'
    - 'graph_edge_ref'
    - 'pgvector semantic index'
    """
    graph_candidate = extract_file_from_graph_edge(diagnosis.graph_edge_ref)

    # Build semantic query from root cause, endpoint, and graph_edge_ref
    query_parts = []
    if diagnosis.root_cause:
        rc_val = diagnosis.root_cause.value if hasattr(diagnosis.root_cause, "value") else str(diagnosis.root_cause)
        query_parts.append(rc_val.replace("_", " "))
    if diagnosis.endpoint:
        query_parts.append(diagnosis.endpoint)
    if diagnosis.graph_edge_ref:
        query_parts.append(diagnosis.graph_edge_ref)
    query_str = " ".join(query_parts)

    semantic_results = search_code(query_str, top_k=3, db_url=db_url)
    semantic_candidate = semantic_results[0]["file_path"] if semantic_results else None
    semantic_score = semantic_results[0]["similarity"] if semantic_results else 0.0

    # Determine signal and final file
    if graph_candidate and semantic_candidate:
        # Check if they point to the same file or same service directory
        same_file = graph_candidate == semantic_candidate
        same_service = graph_candidate.split("/")[0] == semantic_candidate.split("/")[0]

        if same_file or same_service:
            chosen = graph_candidate if same_file else semantic_candidate
            signal = "graph_edge_ref (confirmed by pgvector)"
            logger.info(f"Localization: {chosen} confirmed by both graph edge and pgvector (score={semantic_score:.3f}).")
            return LocalizationResult(
                file_path=chosen,
                signal_used=signal,
                semantic_score=semantic_score,
                details={
                    "graph_candidate": graph_candidate,
                    "semantic_candidate": semantic_candidate,
                    "graph_edge_ref": diagnosis.graph_edge_ref,
                },
            )

    if graph_candidate:
        logger.info(f"Localization: {graph_candidate} found via graph_edge_ref.")
        return LocalizationResult(
            file_path=graph_candidate,
            signal_used="graph_edge_ref",
            semantic_score=semantic_score,
            details={"graph_edge_ref": diagnosis.graph_edge_ref, "semantic_candidate": semantic_candidate},
        )

    if semantic_candidate:
        logger.info(f"Localization: {semantic_candidate} found via pgvector semantic index (score={semantic_score:.3f}).")
        return LocalizationResult(
            file_path=semantic_candidate,
            signal_used="pgvector semantic index",
            semantic_score=semantic_score,
            details={"semantic_candidate": semantic_candidate, "query": query_str},
        )

    # Fallback default if nothing matched
    fallback = "payment/main.py"
    logger.warning("Localization fell back to payment/main.py")
    return LocalizationResult(
        file_path=fallback,
        signal_used="fallback",
        semantic_score=0.0,
        details={"fallback": True},
    )
