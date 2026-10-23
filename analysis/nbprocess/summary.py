"""
Reproducibility Assessment Module

Create summary for the reproducibility assessment module

Author: the authors <anonymous@example.org>
Institution: an anonymised institution
License: GPL-3.0 license
Repository: https://github.com/the authors-Samuel/computational-reproducibility-pmc-docker
"""

import sqlite3
import os
import json
from pathlib import Path
from datetime import datetime

from .diff import extract_cell_ops, get_ops
from .outputs import extract_output_values, is_float
from .nondeterminism import detect_nondeterminism

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

IGNORE_FIELDS = {"execution_count", "metadata"}

DB_FILE = os.environ.get("DB_FILE")
REPO_TOTAL_TIME = float(os.getenv("REPO_TOTAL_TIME", 0))
REPO_URL = os.environ.get("REPO_URL")
NOTEBOOKS_COUNT = os.environ.get("NOTEBOOKS_COUNT")
#EXEC_LOG_PATH = Path("logs/notebook_execution_times.log")
# The run's own execution log (src/pyenv.sh exports NOTEBOOK_EXEC_LOG); the shared log is the old fallback.
EXEC_LOG_PATH = Path(os.environ.get("NOTEBOOK_EXEC_LOG") or Path(os.environ.get("LOG_DIR", "")) / "notebook_execution_times.log")
RUN_ID = int(os.environ.get("RUN_ID"))

if not DB_FILE:
    raise RuntimeError(
        "DB_FILE environment variable is not set. "
        "Make sure main.sh exports DB_FILE before running Python."
    )

DB_FILE = Path(DB_FILE)





def insert_notebook_execution(summary, repository_run_id):
    logging.info("insert_notebook_execution summary: %s.", summary)    
    logging.info("insert_notebook_execution repository_run_id: %s.", repository_run_id) 
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    notebook_name = summary["notebook"]
    repository_id = summary["repository_id"]

    # Resolve notebook_id (may be NULL)
    cur.execute(
        "SELECT id, repository_id FROM notebooks WHERE name = ? AND repository_id = ? LIMIT 1",
        (notebook_name, repository_id)
    )
    row = cur.fetchone()
    if row:
        notebook_id, repository_id = row
    else:
        notebook_id = None
        repository_id = None
    
    logging.info("notebook_id: %s.", notebook_id)
    logging.info("repository_id: %s.", repository_id)
    logging.info("REPO_URL: %s.", REPO_URL)
    logging.info("REPO_TOTAL_TIME: %s.", REPO_TOTAL_TIME)

    from .error_context import ensure_columns
    ensure_columns(conn)
    cur.execute(
        """
        INSERT INTO notebook_executions (
            repository_run_id,
            repository_id,
            notebook_id,
            notebook_name,
            url,
            execution_status,
            execution_duration,
            total_code_cells,
            executed_cells,
            error_type,
            error_category,
            error_message,
            error_cell_index,
            error_count,
            error_category_by_type,
            error_evidence
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            repository_run_id,
            repository_id,
            notebook_id,
            notebook_name,
            summary["url"],
            summary["execution_status"],
            summary["notebook_execution_duration"],
            summary["total_code_cells"],
            summary["executed_cells"],
            summary["error_type"],
            summary["error_category"],
            summary["error_message"],
            summary["error_cell_index"],
            summary["error_count"],
            summary.get("error_category_by_type", summary["error_category"]),
            summary.get("error_evidence"),
        ),
    )

    notebook_execution_id = cur.lastrowid
    summary["notebook_execution_id"] = notebook_execution_id
    summary["notebook_id"] = notebook_id

    insert_notebook_reproducibility_metrics(
        conn,
        summary,
        repository_run_id,
        notebook_execution_id,
        repository_id,
        notebook_id,
    )

    conn.commit()
    conn.close()

def classify_reproducibility(summary):
    if summary["execution_status"] == "FAIL":
        return "FAIL"

    total = summary["total_code_cells"]
    identical = summary["same_cells_count"]
    different = summary["different_cells_count"]

    if total == 0:
        return "FAIL"

    if identical == total:
        return "FULL"

    ratio = identical / total

    if ratio >= 0.8:
        return "PARTIAL"

    return "NON_REPRODUCIBLE"

def classify_repository_run(conn, run_id):
    cur = conn.cursor()

    cur.execute("""
        SELECT reproducibility_status
        FROM notebook_reproducibility_metrics
        WHERE repository_run_id = ?
    """, (run_id,))

    statuses = [row[0] for row in cur.fetchall()]

    if not statuses:
        return "FAIL"

    if all(s == "FULL" for s in statuses):
        return "FULL"

    if any(s == "FAIL" for s in statuses):
        return "FAIL"

    if any(s == "NON_REPRODUCIBLE" for s in statuses):
        return "PARTIAL"

    return "PARTIAL"

def insert_notebook_reproducibility_metrics(
    conn,
    summary,
    repository_run_id,
    notebook_execution_id,
    repository_id,
    notebook_id,
):
    cur = conn.cursor()
    logging.info("insert_notebook_reproducibility_metrics summary: %s.", summary)
    logging.info("insert_notebook_reproducibility_metrics repository_run_id: %s.", repository_run_id)
    logging.info("insert_notebook_reproducibility_metrics notebook_execution_id: %s.", notebook_execution_id)
    logging.info("insert_notebook_reproducibility_metrics repository_id: %s.", repository_id)
    logging.info("insert_notebook_reproducibility_metrics notebook_id: %s.", notebook_id)
    # reproducibility_status = classify_reproducibility(summary)

    cur.execute(
        """
        INSERT INTO notebook_reproducibility_metrics (
            repository_run_id,
            notebook_execution_id,
            repository_id,
            notebook_id,
            total_code_cells,
            identical_cells_count,
            different_cells_count,
            nondeterministic_cells_count,
            identical_cells,
            different_cells,
            nondeterministic_cells,
            reproducibility_score
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            repository_run_id,
            notebook_execution_id,
            repository_id,
            notebook_id,
            summary["total_code_cells"],
            summary["same_cells_count"],
            summary["different_cells_count"],
            summary["nondeterministic_cells_count"],
            summary["same_cells"],
            summary["different_cells"],
            summary["nondeterministic_cells"],
            summary["reproducibility_score"]                    
        ),
    )





def load_execution_log(log_path=EXEC_LOG_PATH):
    """
    Reads notebook_execution_times.log and returns:
    {
        notebook_path: {
            "status": "SUCCESS" | "FAIL",
            "duration": float | None
        }
    }
    """
    #log_path = Path(log_dir) / "notebook_execution_times.log"
    execution_log = {}

    if not log_path.exists():
        logging.info("LOG PATH does not exist: %s", log_path)
        return execution_log

    with open(log_path, "r") as f:
        for line in f:
            parts = line.strip().split("|")
            if len(parts) not in (4, 5):          # EXEC_FAIL lines carry a fifth field: the reason
                continue

            status, repo, notebook, duration = parts[:4]

            execution_log[notebook] = {
                "status": status,
                "duration": float(duration) if duration.replace(".", "", 1).isdigit() else None,
                "reason": parts[4] if len(parts) == 5 else None,
            }

    return execution_log

def load_notebook_durations(log_path=EXEC_LOG_PATH):
    """
    Reads notebook_execution_times.log and returns execution time in seconds
    for the given notebook name.
    """
    durations = {}
    if not os.path.exists(log_path):        
        return durations

    with open(log_path) as f:
        for line in f:
            if line.startswith("EXEC_TIME|"):
                _, nb, dur = line.strip().split("|")
                durations[os.path.basename(nb)] = float(dur)
    return durations

def categorize_error_type(error_type: str) -> str:
    if not error_type:
        return "UNKNOWN_ERROR"

    error_type = error_type.strip()

    mapping = {
        "ModuleNotFoundError": "DEPENDENCY_ERROR",
        "ImportError": "DEPENDENCY_ERROR",
        "FileNotFoundError": "FILE_ERROR",
        "PermissionError": "FILE_ERROR",
        "KeyError": "DATA_ERROR",
        "ValueError": "DATA_ERROR",
        "TypeError": "CODE_ERROR",
        "AttributeError": "CODE_ERROR",
        "NameError": "CODE_ERROR",
        "SyntaxError": "CODE_ERROR",
        "MemoryError": "RESOURCE_ERROR",
        "TimeoutError": "RESOURCE_ERROR",
        "ConnectionError": "NETWORK_ERROR",
        "HTTPError": "NETWORK_ERROR",
        "KernelDeadError": "EXECUTION_ENVIRONMENT_ERROR",
        "CalledProcessError": "EXECUTION_ENVIRONMENT_ERROR",
    }

    return mapping.get(error_type, "OTHER_ERROR")


def extract_error_from_notebook(notebook):
    """
    Extract runtime errors from an executed notebook.

    Returns:
        List of error dicts:
        [
            {
                "cell_index": int,
                "error_type": str,
                "error_message": str,
                "traceback": str
            }
        ]
    """
    errors = []

    if not hasattr(notebook, "cells"):
        return errors

    for i, cell in enumerate(notebook.cells):
        if cell.cell_type != "code":
            continue

        for output in cell.get("outputs", []):
            if output.get("output_type") == "error":
                error_type = output.get("ename")
                error_message = output.get("evalue")
                traceback = "\n".join(output.get("traceback", []))

                errors.append({
                    "cell_index": i,
                    "error_type": error_type,
                    "error_message": error_message,
                    "traceback": traceback
                })

    return errors

def sanitize_error_message(msg: str, max_len: int = 500):
    if not msg:
        return None
    return msg.strip().replace("\n", " ")[:max_len]



def build_detailed_summary(diff, notebook, notebook_name, repo_id):
    #logging.info("notebook_name: %s", notebook_name)
    #logging.info("repo_id: %s", repo_id)


    execution_log = load_execution_log()
    #logging.info("execution_log: %s", execution_log)
    
    code_cell_indices = [
        i for i, cell in enumerate(notebook.cells)
        if cell.cell_type == "code"
    ]

    total_code_cells = len(code_cell_indices)
    different_cells = []
    different_cell_indices = set()
    nondeterministic_cells = [
        i for i in code_cell_indices
        if detect_nondeterminism(notebook.cells[i].source)
    ]

    for cell_op in extract_cell_ops(diff):
        if cell_op.get("op") != "patch":
            continue

        cell_index = cell_op.get("key")
        if cell_index not in code_cell_indices:
            continue

        for field_op in get_ops(cell_op.get("diff")):
            field = field_op.get("key")
            if field in IGNORE_FIELDS:
                continue

            if field == "source":
                different_cells.append({
                    "cell_index": cell_index,
                    "difference_type": "source",
                    "field": "source",
                    "details": {}
                })
                different_cell_indices.add(cell_index)

            if field == "outputs":
                left, right, mime = extract_output_values(field_op.get("diff"))

                if left is not None and right is not None:
                    diff_type = "numeric" if is_float(left) and is_float(right) else "textual"
                    details = {"mime": mime, "left": left, "right": right}
                else:
                    diff_type = "structural"
                    details = {}

                different_cells.append({
                    "cell_index": cell_index,
                    "difference_type": diff_type,
                    "field": "outputs",
                    "details": details
                })
                different_cell_indices.add(cell_index)

    same_cells = sorted(set(code_cell_indices) - different_cell_indices)
    #durations = load_notebook_durations()
    #logging.info("durations: %s.", durations)

    
    exec_info = execution_log.get(notebook_name, {})

    execution_status = exec_info.get("status", "UNKNOWN")
    notebook_duration = exec_info.get("duration")

    # Extract runtime errors directly from executed notebook
    runtime_errors = extract_error_from_notebook(notebook)

    error_count = len(runtime_errors)
    error_type = None
    error_category = None
    error_category_by_type = None
    error_evidence = None
    error_message = None
    error_cell_index = None

        # Case 1: Execution failed entirely (from execution log)
    if execution_status == "FAIL":
        error_category = "ERROR"

    # Case 2: Notebook executed but contains runtime errors
    elif error_count > 0:
        first_error = runtime_errors[0]

        error_type = first_error.get("error_type")
        error_message = first_error.get("error_message")
        error_cell_index = first_error.get("cell_index")
        # type-based category (thesis) plus context: EXTERNAL_SERVICE_ERROR (error_context.py)
        from .error_context import categorize
        error_category, error_category_by_type, error_evidence = categorize(
            error_type, error_message, first_error.get("traceback"), notebook, error_cell_index)
        error_message = sanitize_error_message(error_message)

        # Ensure correct status
        if execution_status != "FAIL":
            execution_status = "SUCCESS_WITH_ERRORS"
        

    identical_cells = same_cells
    different_cells_list = list(different_cell_indices)

    executed_cells = len(identical_cells) + len(different_cells_list)    
    

    summary = {
        "repository_id": repo_id,
        "notebook": notebook_name,
        "url": REPO_URL,

        "execution_status": execution_status,
        "notebook_execution_duration": notebook_duration,

        "total_code_cells": total_code_cells,
        "executed_cells": executed_cells,

        "same_cells_count": len(identical_cells),
        "different_cells_count": len(different_cells_list),
        "nondeterministic_cells_count": len(nondeterministic_cells),

        "same_cells": ",".join(map(str, identical_cells)),
        "different_cells": ",".join(map(str, different_cells_list)),
        "nondeterministic_cells": ",".join(map(str, nondeterministic_cells)),

        "reproducibility_score": round(
            len(identical_cells) / total_code_cells if total_code_cells else 1.0,
            3,
        ),        
        "error_type": error_type,
        "error_category": error_category,
        "error_category_by_type": error_category_by_type,
        "error_evidence": error_evidence,
        "error_message": error_message,
        "error_cell_index": error_cell_index,
        "error_count": error_count
    }



    # summary = {
    #     "repository_id": repo_id,
    #     "notebook": notebook_name,
    #     "url": GITHUB_REPO,
    #     "total_code_cells": total_code_cells,
    #     "same_cells_count": len(same_cells),
    #     "different_cells_count": len(different_cell_indices),
    #     "same_cells": ",".join(map(str, same_cells)),
    #     "different_cells": ",".join(map(str, different_cell_indices)),
    #     "nondeterministic_cells": ",".join(map(str, nondeterministic_cells)),
    #     "execution_status": execution_status,
    #     "error_type": error_type,
    #     "error_category": error_category,
    #     "error_message": error_message,
    #     "error_cell_index": error_cell_index,
    #     "error_count": error_count,
    #     "notebook_execution_duration": notebook_duration,
    #     "repo_execution_duration": REPO_TOTAL_TIME,
    #     "notebooks_count": NOTEBOOKS_COUNT,
    #     "reproducibility_score": round(
    #         len(same_cells) / total_code_cells if total_code_cells else 1.0, 3
    #     ),
    # }

    # Handle failed execution
    if execution_status == "EXEC_FAIL":
        summary.update({
            "total_code_cells": 0,
            "same_cells_count": 0,
            "different_cells_count": 0,
            "same_cells": "",
            "different_cells": "",
            "nondeterministic_cells": "",
            "reproducibility_score": 0.0,
        })
        
    # Normal successful case
    insert_notebook_execution(summary, RUN_ID)
    return summary
