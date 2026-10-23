import ast
from pathlib import Path

from .loader import load_notebook

COLLECTOR_VERSION = "1.0.0"


def _extract_imports(source):
    lines = source.splitlines()
    first_content = next((line.strip() for line in lines if line.strip()), "")
    if first_content.startswith("%%"):
        return []
    python_source = "\n".join(
        line
        for line in lines
        if not line.lstrip().startswith(("%", "!", "?"))
    )
    try:
        tree = ast.parse(python_source)
    except SyntaxError:
        return []

    imports = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])

    return sorted(imports)


def _collect_output(output):
    output_type = output.get("output_type", "unknown")
    summary = {"type": output_type}

    if output_type in {"display_data", "execute_result"}:
        summary["mime_types"] = sorted(output.get("data", {}).keys())
    elif output_type == "stream":
        summary["stream"] = output.get("name", "stdout")
    elif output_type == "error":
        summary["error_name"] = output.get("ename", "")

    return summary


def collect_notebook(path):
    notebook_path = Path(path)
    notebook = load_notebook(notebook_path)

    collected = {
        "path": str(notebook_path),
        "metadata": dict(notebook.metadata),
        "markdown_cells": [],
        "code_cells": [],
        "imports": [],
        "outputs": [],
        "cell_counts": {"markdown": 0, "code": 0, "raw": 0},
    }

    for cell in notebook.cells:
        if cell.cell_type == "markdown":
            collected["markdown_cells"].append(cell.source)
            collected["cell_counts"]["markdown"] += 1
        elif cell.cell_type == "code":
            collected["code_cells"].append(cell.source)
            collected["imports"].extend(_extract_imports(cell.source))
            collected["outputs"].extend(
                _collect_output(output) for output in cell.get("outputs", [])
            )
            collected["cell_counts"]["code"] += 1
        else:
            collected["cell_counts"]["raw"] += 1

    collected["imports"] = sorted(set(collected["imports"]))
    return collected
