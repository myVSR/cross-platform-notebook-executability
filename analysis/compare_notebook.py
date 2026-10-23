# import argparse
# import json
# from pathlib import Path

# from scripts.nbprocess.loader import load_notebook
# from scripts.nbprocess.diff import diff_notebooks_safe
# from scripts.nbprocess.summary import build_detailed_summary, extract_error_from_notebook
# from scripts.nbprocess.filesystem import ensure_parent_dir

# def main():
#     parser = argparse.ArgumentParser()
#     parser.add_argument("notebook_a")
#     parser.add_argument("notebook_b")
#     parser.add_argument("NOTEBOOK_PATH")
#     parser.add_argument("REPO_ID")
#     parser.add_argument("--json", required=True)

#     args = parser.parse_args()

#     nb1 = load_notebook(args.notebook_a)
#     nb2 = load_notebook(args.notebook_b)

#     diff = diff_notebooks_safe(nb1, nb2)
#     nb_errors = extract_error_from_notebook(nb2)

#     summary = build_detailed_summary(
#         diff,
#         nb2,
#         args.NOTEBOOK_PATH,
#         args.REPO_ID
#     )

#     report = {
#         "summary": summary,
#         "raw_diff": diff,
#         "nb_errors": nb_errors,
#     }

#     ensure_parent_dir(args.json)
#     Path(args.json).write_text(json.dumps(report, indent=2, default=str))

# if __name__ == "__main__":
#     main()

# import argparse
# import json
# from pathlib import Path

# from scripts.nbprocess.loader import load_notebook
# from scripts.nbprocess.diff import diff_notebooks_safe
# from scripts.nbprocess.summary import build_detailed_summary, extract_error_from_notebook
# from scripts.nbprocess.filesystem import ensure_parent_dir

# def main():
#     parser = argparse.ArgumentParser()
#     parser.add_argument("notebook_a")
#     parser.add_argument("notebook_b")
#     parser.add_argument("NOTEBOOK_PATH")
#     parser.add_argument("REPO_ID")
#     parser.add_argument("--json", required=True)

#     args = parser.parse_args()

#     nb1 = load_notebook(args.notebook_a)
#     nb2 = load_notebook(args.notebook_b)

#     diff = diff_notebooks_safe(nb1, nb2)
#     nb_errors = extract_error_from_notebook(nb2)

#     summary = build_detailed_summary(
#         diff,
#         nb2,
#         args.NOTEBOOK_PATH,
#         args.REPO_ID
#     )

#     report = {
#         "summary": summary,
#         "raw_diff": diff,
#         "nb_errors": nb_errors,
#     }

#     ensure_parent_dir(args.json)
#     Path(args.json).write_text(json.dumps(report, indent=2, default=str))

# if __name__ == "__main__":
#     main()
import argparse
import json
import sys
from pathlib import Path

# project root on the import path without relying on PYTHONPATH (which must not leak into kernels)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis.nbprocess.loader import load_notebook
from analysis.nbprocess.diff import diff_notebooks_safe
from analysis.nbprocess.summary import build_detailed_summary, extract_error_from_notebook
from analysis.nbprocess.filesystem import ensure_parent_dir
from analysis.nbprocess import compare_cells
from analysis.nbprocess import summary as summary_module


def record_comparison_v2(report, execution_id, notebook_id, run_id):
    """Store comparison version 2: score variants on the metrics row, one row per cell."""
    import sqlite3
    connection = sqlite3.connect(summary_module.DB_FILE)
    counts = report["counts"]
    reproduced = sum(counts.get(k, 0) for k in ("identical", "identical normalised", "identical tolerance", "image similar"))
    connection.execute(
        "UPDATE notebook_reproducibility_metrics SET comparison_version = ?, score_strict = ?, score_normalised = ?, "
        "score_text_data = ?, score_before_error = ?, cells_assessable = ?, cells_empty = ?, cells_not_stored = ?, "
        "cells_identical = ?, cells_reproduced_normalised = ?, cells_image_similar = ?, cells_image_only = ?, cells_error = ?, "
        "cells_missing = ?, "
        "cells_different = ? WHERE notebook_execution_id = ?",
        (report["version"], report["scores"]["strict"], report["scores"]["normalised"], report["scores"]["text_data"],
         report["scores"]["before_error"], report["assessable"], counts.get("empty", 0), counts.get("not stored", 0),
         counts.get("identical", 0), reproduced, counts.get("image similar", 0), counts.get("image only", 0),
         counts.get("error", 0),
         counts.get("missing", 0), counts.get("different", 0), execution_id))
    connection.executemany(
        "INSERT INTO notebook_cell_comparisons (repository_run_id, notebook_execution_id, notebook_id, cell_index, "
        "outcome, rules_json, notes_json, stored_outputs, executed_outputs, error_name, source_changed) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(run_id, execution_id, notebook_id, c["cell_index"], c["outcome"], json.dumps(c["rules"]), json.dumps(c["notes"]),
          c["stored_outputs"], c["executed_outputs"], c["error"], int(c["source_changed"])) for c in report["cells"]])
    connection.commit()
    connection.close()


def record_missing_output(notebook_path, repo_id, original_path):
    """A notebook that produced no executed copy (timeout, crash): an execution row without scores."""
    import sqlite3
    log = summary_module.load_execution_log().get(notebook_path, {})
    invalid = None
    try:
        code = len(compare_cells.code_cells(json.loads(Path(original_path).read_text(errors="replace"))))
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as error:
        # not a notebook (empty file, HTML page, Git LFS pointer, broken JSON): still one execution row, so that
        # the notebook counts as not executed instead of having no record (study run 2026-10)
        code, invalid = None, f"{type(error).__name__}"
    reason = log.get("reason") or ("INVALID_NOTEBOOK" if invalid else "no output notebook")
    connection = sqlite3.connect(summary_module.DB_FILE)
    row = connection.execute("SELECT id FROM notebooks WHERE name = ? AND repository_id = ?", (notebook_path, repo_id)).fetchone()
    status = log.get("status") or "EXEC_FAIL"
    connection.execute(
        "INSERT INTO notebook_executions (repository_run_id, repository_id, notebook_id, notebook_name, url, "
        "execution_status, execution_duration, total_code_cells, executed_cells, error_type, error_category, "
        "error_message, error_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (summary_module.RUN_ID, repo_id, row[0] if row else None, notebook_path, summary_module.REPO_URL, status,
         log.get("duration"), code, 0, log.get("reason") or ("INVALID_NOTEBOOK" if invalid else "NO_OUTPUT_NOTEBOOK"),
         "EXECUTION_FAILURE", "the notebook produced no executed copy (" + reason
         + (f"; the file is not a valid notebook: {invalid}" if invalid else "") + ")", 0))
    connection.commit()
    connection.close()


def record_executed_without_comparison(notebook_path, repo_id, original_path, executed, problem):
    """An executed notebook whose outputs could not be compared: its execution row (status and first error from the
    executed copy) without reproduction scores."""
    import sqlite3
    log = summary_module.load_execution_log().get(notebook_path, {})
    errors = extract_error_from_notebook(executed)
    first = errors[0] if errors else {}
    status = log.get("status") or ("SUCCESS_WITH_ERRORS" if errors else "SUCCESS")
    try:
        code = len(compare_cells.code_cells(json.loads(Path(original_path).read_text(errors="replace"))))
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        code = None
    error_type = first.get("error_type")
    category = by_type = evidence = None
    if error_type:                  # the same rule as a compared notebook (summary.py): type plus context
        from analysis.nbprocess.error_context import categorize
        category, by_type, evidence = categorize(error_type, first.get("error_message"), first.get("traceback"),
                                                 executed, first.get("cell_index"))
    message = summary_module.sanitize_error_message(first.get("error_message") or "")
    connection = sqlite3.connect(summary_module.DB_FILE)
    from analysis.nbprocess.error_context import ensure_columns
    ensure_columns(connection)
    row = connection.execute("SELECT id FROM notebooks WHERE name = ? AND repository_id = ?", (notebook_path, repo_id)).fetchone()
    connection.execute(
        "INSERT INTO notebook_executions (repository_run_id, repository_id, notebook_id, notebook_name, url, "
        "execution_status, execution_duration, total_code_cells, executed_cells, error_type, error_category, "
        "error_category_by_type, error_evidence, error_cell_index, error_message, error_count) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (summary_module.RUN_ID, repo_id, row[0] if row else None, notebook_path, summary_module.REPO_URL, status,
         log.get("duration"), code, None, error_type, category, by_type, evidence, first.get("cell_index"),
         ((message or "")[:300] + f" [outputs not compared: {problem}]").strip(), len(errors)))
    connection.commit()
    connection.close()
    print(f"INFO: {notebook_path}: executed ({status}), outputs not compared: {problem}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("notebook_a")
    parser.add_argument("notebook_b")
    parser.add_argument("NOTEBOOK_PATH")
    parser.add_argument("REPO_ID")
    parser.add_argument("--json", required=True)
    parser.add_argument("--missing-output", action="store_true",
                        help="notebook_b does not exist: record the failed execution without scores")
    args = parser.parse_args()
    if args.missing_output:
        record_missing_output(args.NOTEBOOK_PATH, int(args.REPO_ID), args.notebook_a)
        return
    nb1 = load_notebook(args.notebook_a)
    nb2 = load_notebook(args.notebook_b)
    try:
        diff = diff_notebooks_safe(nb1, nb2)
    except Exception as error:      # noqa: BLE001 - e.g. RecursionError in nbdime on an nbformat test notebook
        # the notebook was executed: record the execution without scores instead of leaving it without a record
        # (study run 2026-10-11: test4plus.ipynb of a committed venv had no execution row)
        record_executed_without_comparison(args.NOTEBOOK_PATH, int(args.REPO_ID), args.notebook_a, nb2,
                                           f"{type(error).__name__}: {error}"[:200])
        return
    nb_errors = extract_error_from_notebook(nb2)
    # thesis comparison (version 1), unchanged
    summary = build_detailed_summary(
        diff,
        nb2,
        args.NOTEBOOK_PATH,
        args.REPO_ID
    )
    # comparison version 2: per cell, normalised, assessable cells only
    try:
        report_v2 = compare_cells.compare_notebooks(json.loads(Path(args.notebook_a).read_text(errors="replace")),
                                                    json.loads(Path(args.notebook_b).read_text(errors="replace")))
    except Exception as error:      # noqa: BLE001 - the execution is recorded; it only has no version-2 scores
        print(f"INFO: {args.NOTEBOOK_PATH}: comparison version 2 failed ({type(error).__name__}: {error})"[:300])
        report_v2 = None
    if report_v2 and summary.get("notebook_execution_id"):
        record_comparison_v2(report_v2, summary["notebook_execution_id"], summary.get("notebook_id"),
                             summary_module.RUN_ID)
    report = {
        "summary": summary,
        "raw_diff": diff,
        "nb_errors": nb_errors,
        "comparison_v2": report_v2,
    }
    ensure_parent_dir(args.json)
    Path(args.json).write_text(json.dumps(report, indent=2, default=str))

if __name__ == "__main__":
    main()
