#!/usr/bin/env python3
"""Export one completed shell-pipeline run as a stable JSON API payload."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def _row_dict(row: sqlite3.Row | None) -> dict:
    return dict(row) if row is not None else {}


def _aggregate_status(pipeline_status: str, notebooks: list[dict]) -> str:
    if pipeline_status != "SUCCESS":
        return "failed"
    statuses = {item.get("executionStatus") for item in notebooks}
    if not statuses or None in statuses or "EXEC_FAIL" in statuses or "FAIL" in statuses:
        return "partial"
    if "SUCCESS_WITH_ERRORS" in statuses:
        return "partial"
    return "succeeded"


def export_result(database: Path, repository_id: int) -> dict:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        run = connection.execute(
            """
            SELECT * FROM repository_runs
            WHERE repository_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (repository_id,),
        ).fetchone()
        if run is None:
            raise RuntimeError("The pipeline did not create a repository run")

        rows = connection.execute(
            """
            SELECT
                n.name AS notebook_path,
                n.language AS language,
                ne.execution_status,
                ne.execution_duration,
                ne.error_type,
                ne.error_category,
                ne.error_message,
                ne.error_count,
                metrics.total_code_cells,
                metrics.identical_cells_count,
                metrics.different_cells_count,
                metrics.nondeterministic_cells_count,
                metrics.reproducibility_score,
                classification.rule_category,
                classification.rule_confidence,
                classification.llm_category,
                classification.llm_confidence,
                classification.agreement_status,
                classification.needs_human_review,
                classification.warning,
                classification.provisional_category,
                classification.final_category
            FROM notebooks n
            LEFT JOIN notebook_executions ne
              ON ne.id = (
                SELECT nested_ne.id FROM notebook_executions nested_ne
                WHERE nested_ne.repository_run_id = ?
                  AND nested_ne.notebook_id = n.id
                ORDER BY nested_ne.id DESC LIMIT 1
              )
            LEFT JOIN notebook_reproducibility_metrics metrics
              ON metrics.id = (
                SELECT nested_metrics.id FROM notebook_reproducibility_metrics nested_metrics
                WHERE nested_metrics.repository_run_id = ?
                  AND nested_metrics.notebook_id = n.id
                ORDER BY nested_metrics.id DESC LIMIT 1
              )
            LEFT JOIN notebook_classifications classification
              ON classification.id = (
                SELECT nested_classification.id FROM notebook_classifications nested_classification
                WHERE nested_classification.notebook_id = n.id
                ORDER BY nested_classification.id DESC LIMIT 1
              )
            WHERE n.repository_id = ?
            ORDER BY n.name
            """,
            (run["id"], run["id"], repository_id),
        ).fetchall()

        notebooks = []
        for row in rows:
            score = row["reproducibility_score"]
            notebooks.append(
                {
                    "path": row["notebook_path"],
                    "language": row["language"] or "python",
                    "executionStatus": row["execution_status"],
                    "durationSeconds": row["execution_duration"],
                    "errorType": row["error_type"],
                    "errorCategory": row["error_category"],
                    "errorMessage": row["error_message"],
                    "errorCount": row["error_count"] or 0,
                    "totalCodeCells": row["total_code_cells"],
                    "identicalCellsCount": row["identical_cells_count"],
                    "differentCellsCount": row["different_cells_count"],
                    "nondeterministicCellsCount": row["nondeterministic_cells_count"],
                    "reproducibilityScore": None if score is None else round(float(score) * 100),
                    "ruleCategory": row["rule_category"],
                    "ruleConfidence": row["rule_confidence"],
                    "llmCategory": row["llm_category"],
                    "llmConfidence": row["llm_confidence"],
                    "agreementStatus": row["agreement_status"],
                    "needsHumanReview": bool(row["needs_human_review"]) if row["needs_human_review"] is not None else False,
                    "warning": row["warning"],
                    "finalCategory": row["final_category"] or row["provisional_category"],
                }
            )

        pipeline_status = run["run_status"]
        return {
            "status": _aggregate_status(pipeline_status, notebooks),
            "pipelineStatus": pipeline_status,
            "error": run["error_message"] if pipeline_status != "SUCCESS" else None,
            "startedAt": run["started_at"],
            "finishedAt": run["finished_at"],
            "durationSeconds": run["duration_seconds"],
            "notebooks": notebooks,
        }
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--repository-id", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    payload = export_result(arguments.database, arguments.repository_id)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = arguments.output.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(arguments.output)


if __name__ == "__main__":
    main()
