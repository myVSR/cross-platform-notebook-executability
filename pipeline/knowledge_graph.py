"""Read-only browsing of the materialised Notebook FAIR graph.

Only the configured local RDF file is read. User input selects existing entities;
no user-supplied SPARQL, remote RDF, or arbitrary filesystem paths are evaluated.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NF = "https://w3id.org/notebookfair#"
KINDS = {
    "RepositoryResource": "Repositories",
    "RepositoryPlatform": "Platforms",
    "NotebookResource": "Notebooks",
    "RepositoryRun": "Runs",
    "NotebookExecution": "Executions",
    "ReproducibilityResult": "Results",
    "NotebookClassificationActivity": "Classifications",
}
MAX_NEIGHBORS = 16
_lock = threading.Lock()
_cache = None


def _short(value: str) -> str:
    return value.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def _load():
    global _cache
    from rdflib import Graph, RDF, RDFS, URIRef

    workspace_graph = ROOT / "data/output/kg/notebookfair-workspace.nt"
    default_graph = workspace_graph if workspace_graph.is_file() else ROOT / "data/output/kg/notebookfair-pipeline.nt"
    graph_path = Path(os.environ.get("NOTEBOOKFAIR_KG_PATH", str(default_graph)))
    if not graph_path.is_file():
        raise FileNotFoundError("The knowledge graph has not been built yet. Build it from the saved analysis, then refresh this page.")
    stat = graph_path.stat()
    key = (str(graph_path.resolve()), stat.st_mtime_ns, stat.st_size)
    with _lock:
        if _cache is not None and _cache[0] == key:
            return _cache[1]
        graph = Graph()
        graph.parse(graph_path, format="nt")
        entities = {}
        groups = {kind: set() for kind in KINDS}
        for kind in KINDS:
            types = [kind]
            if kind == "RepositoryResource":
                types += ["GitRepositoryResource", "ArchivedResearchRecord"]
            for rdf_type in types:
                for subject in graph.subjects(RDF.type, URIRef(NF + rdf_type)):
                    if not isinstance(subject, URIRef):
                        continue
                    uri = str(subject)
                    label = str(next(graph.objects(subject, RDFS.label), "")) or _short(uri)
                    entities[uri] = {"id": uri, "label": label, "kind": kind}
                    groups[kind].add(uri)
        metadata = {}
        try:
            candidate = json.loads(graph_path.with_name("workspace-build-summary.json" if graph_path.name == "notebookfair-workspace.nt" else "build-summary.json").read_text(encoding="utf-8"))
            if isinstance(candidate, dict):
                metadata = candidate
        except (OSError, ValueError):
            pass
        data = (graph, entities, groups, {
            "triples": len(graph),
            "entities": len(entities),
            "builtAt": metadata.get("built_at") or datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "source": metadata.get("data_source", "saved_graph"),
            "kinds": [{"id": kind, "label": label, "count": len(groups[kind])} for kind, label in KINDS.items()],
        })
        _cache = (key, data)
        return data


def browse_graph(kind="RepositoryResource", query="", offset=0, focus=None):
    from rdflib import Literal, RDF, URIRef

    if kind not in KINDS:
        raise ValueError("Choose a supported graph entity type.")
    if len(query) > 200 or focus is not None and len(focus) > 4096:
        raise ValueError("The graph search is too long.")
    if offset < 0 or offset > 1_000_000:
        raise ValueError("The graph page is invalid.")
    graph, entities, groups, summary = _load()
    query = query.casefold().strip()
    matching = sorted(
        (entities[uri] for uri in groups[kind] if not query or query in entities[uri]["label"].casefold() or query in uri.casefold()),
        key=lambda node: (node["label"].casefold(), node["id"]),
    )
    page = matching[offset:offset + 25]
    selected = entities.get(focus) if focus else (page[0] if page else None)
    if focus and selected is None:
        raise ValueError("This entity is not present in the saved graph. Refresh and select another record.")
    properties, edges, nodes = [], [], []
    total_connections, neighbor_counts = 0, {}
    if selected:
        subject = URIRef(selected["id"])
        nodes = [selected]
        connections = set()
        for predicate, value in graph.predicate_objects(subject):
            if predicate == RDF.type:
                continue
            if isinstance(value, Literal) or str(value) not in entities:
                properties.append({"predicate": str(predicate), "label": _short(str(predicate)), "value": str(value)})
            elif str(value) in entities:
                connections.add((str(subject), str(predicate), str(value)))
        for source, predicate in graph.subject_predicates(subject):
            if str(source) in entities:
                connections.add((str(source), str(predicate), str(subject)))
        total_connections = len(connections)
        # Bound visual density by nodes, retaining every relation between the
        # selected entity and each displayed neighbor (including inverse links).
        # Neighbors are taken round-robin per kind so a large group (for example
        # a repository's notebooks) cannot hide its platform or runs.
        by_kind = {}
        for uri in {b if a == str(subject) else a for a, _, b in connections}:
            by_kind.setdefault(entities[uri]["kind"], []).append(uri)
        for items in by_kind.values():
            items.sort(key=lambda uri: (entities[uri]["label"].casefold(), uri))
        neighbors, rank = [], 0
        while len(neighbors) < MAX_NEIGHBORS and any(rank < len(items) for items in by_kind.values()):
            for kind_name in sorted(by_kind):
                if rank < len(by_kind[kind_name]) and len(neighbors) < MAX_NEIGHBORS:
                    neighbors.append(by_kind[kind_name][rank])
            rank += 1
        neighbor_counts = {kind_name: len(items) for kind_name, items in by_kind.items()}
        visible = {str(subject), *neighbors}
        nodes += [entities[uri] for uri in neighbors if uri != str(subject)]
        edges = [{"source": a, "target": b, "predicate": p, "label": _short(p)} for a, p, b in sorted(connections) if a in visible and b in visible]
        properties.sort(key=lambda item: (item["label"], item["value"]))
    return {"summary": summary, "results": page, "total": len(matching), "offset": offset,
            "selected": selected, "properties": properties, "nodes": nodes, "edges": edges,
            "totalConnections": total_connections, "neighborCounts": neighbor_counts}
