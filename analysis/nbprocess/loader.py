import nbformat
from pathlib import Path

def load_notebook(path):
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Notebook not found: {path}")
    return nbformat.read(path, as_version=4)
