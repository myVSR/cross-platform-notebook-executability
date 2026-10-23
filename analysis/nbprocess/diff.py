from nbdime.diffing.notebooks import diff_notebooks

def get_ops(node):
    if node is None:
        return []
    if isinstance(node, list):
        return node
    if isinstance(node, dict) and "ops" in node:
        return node["ops"]
    return []

def extract_cell_ops(diff):
    for entry in get_ops(diff):
        if entry.get("op") == "patch" and entry.get("key") == "cells":
            return get_ops(entry.get("diff"))
    return []

def diff_notebooks_safe(nb1, nb2):
    return diff_notebooks(nb1, nb2)
