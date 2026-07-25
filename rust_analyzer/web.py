"""FastAPI web frontend for rust-analyzer-db.

Provides a master-panel style web UI with dashboard, item browser,
complexity report, call graph, API surface, dependency analysis, and search.

Usage:
    rust-analyzer-db serve --db rust_code.db
    rust-analyzer-db serve --db rust_code.db --port 8080 --host 0.0.0.0
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from .db import RustCodeDB
from .logging import get_logger

log = get_logger("web")

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATE_DIR = Path(__file__).parent / "templates"

app = FastAPI(title="Rust Analyzer DB", version=__version__)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

_db_path: str = "rust_code.db"


def _db() -> RustCodeDB:
    return RustCodeDB(_db_path)


def _r(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    return dict(row)


def _rs(rows: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _tr(request: Request, name: str, ctx: dict[str, Any]) -> HTMLResponse:
    ctx["request"] = request
    return templates.TemplateResponse(request, name, ctx)


# ── Dashboard ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    db = _db()
    try:
        n_files, kind_rows_raw = db.stats()
        kind_rows = _rs(kind_rows_raw)
        total_items = sum(r["n"] for r in kind_rows)
        total_calls, resolved_calls = db.call_graph_stats()
        pub_count = len(db.api_surface())
        top_complex = _rs(db.most_complex_functions(limit=10))
        files = _rs(db.largest_files(limit=10))
        extern_crates = _rs(db.all_extern_crates())

        avg_complexity = 0.0
        if top_complex:
            avg_complexity = sum(r["cyclomatic_complexity"] for r in top_complex) / len(top_complex)

        kind_labels = [r["kind"] for r in kind_rows]
        kind_counts = [r["n"] for r in kind_rows]

        cx_buckets: dict[str, int] = {"1": 0, "2-3": 0, "4-5": 0, "6-9": 0, "10-14": 0, "15+": 0}
        all_fns = _rs(db.list_items(kind="function", limit=10000))
        all_fns += _rs(db.list_items(kind="method", limit=10000))
        for fn in all_fns:
            cc = fn["cyclomatic_complexity"] or 1
            if cc <= 1:
                cx_buckets["1"] += 1
            elif cc <= 3:
                cx_buckets["2-3"] += 1
            elif cc <= 5:
                cx_buckets["4-5"] += 1
            elif cc <= 9:
                cx_buckets["6-9"] += 1
            elif cc <= 14:
                cx_buckets["10-14"] += 1
            else:
                cx_buckets["15+"] += 1

        file_labels: list[str] = []
        file_counts: list[int] = []
        for f in files:
            name_str = Path(f["path"]).name
            if len(name_str) > 25:
                name_str = name_str[:22] + "..."
            file_labels.append(name_str)
            file_counts.append(f["total_loc"] or 0)

        return _tr(request, "dashboard.html", {
            "active": "dashboard",
            "n_files": n_files,
            "total_items": total_items,
            "total_calls": total_calls,
            "resolved_calls": resolved_calls,
            "pub_count": pub_count,
            "avg_complexity": avg_complexity,
            "n_extern_crates": len(extern_crates),
            "top_complex": top_complex,
            "kind_labels": kind_labels,
            "kind_counts": kind_counts,
            "cx_labels": list(cx_buckets.keys()),
            "cx_counts": list(cx_buckets.values()),
            "file_labels": file_labels,
            "file_counts": file_counts,
        })
    finally:
        db.close()


# ── Items Browser ────────────────────────────────────────────────────────────

@app.get("/items", response_class=HTMLResponse)
def items_page(
    request: Request,
    kind: str = "",
    name: str = "",
    file: str = "",
    page: int = Query(1, ge=1),
    per_page: int = 50,
) -> HTMLResponse:
    db = _db()
    try:
        all_kind_rows = _rs(db.stats()[1])
        all_kinds = [r["kind"] for r in all_kind_rows]

        all_rows = _rs(db.list_items(
            kind=kind or None,
            name=name or None,
            file_like=file or None,
            limit=10000,
        ))
        total = len(all_rows)
        total_pages = max(1, math.ceil(total / per_page))
        start = (page - 1) * per_page
        items = all_rows[start:start + per_page]

        return _tr(request, "items.html", {
            "active": "items",
            "items": items,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "kind_filter": kind,
            "name_filter": name,
            "file_filter": file,
            "all_kinds": all_kinds,
        })
    finally:
        db.close()


# ── Complexity ───────────────────────────────────────────────────────────────

@app.get("/complexity", response_class=HTMLResponse)
def complexity_page(
    request: Request,
    min: int = Query(5, ge=1, le=100),
) -> HTMLResponse:
    db = _db()
    try:
        rows = _rs(db.complexity_report(min_complexity=min))
        max_cc = max((r["cyclomatic_complexity"] for r in rows), default=0)
        max_cog = max((r["cognitive_complexity"] for r in rows), default=0)

        return _tr(request, "complexity.html", {
            "active": "complexity",
            "rows": rows,
            "min_cx": min,
            "max_cc": max_cc,
            "max_cog": max_cog,
        })
    finally:
        db.close()


# ── Call Graph ───────────────────────────────────────────────────────────────

@app.get("/graph", response_class=HTMLResponse)
def graph_page(
    request: Request,
    root: str = "",
    direction: str = "both",
    depth: int = Query(2, ge=1, le=10),
    unresolved: bool = True,
) -> HTMLResponse:
    from . import graph as graphmod

    db = _db()
    try:
        title = "Call graph (whole project)"
        node_count = 0
        edge_count = 0

        if root:
            rows = _rs(db.list_items(name=root, limit=50))
            rows = [r for r in rows if r["kind"] in ("function", "method")]
            if rows:
                root_ids = [r["id"] for r in rows]
                names = ", ".join(r["name"] for r in rows[:3])
                title = f"Call graph: {names} (depth {depth}, {direction})"
                g = graphmod.build_subgraph(
                    db, root_ids, depth=depth, direction=direction,
                    include_unresolved=unresolved,
                )
            else:
                g = graphmod.CallGraph()
                title = f"No function found matching '{root}'"
        else:
            g = graphmod.build_whole_graph(db, include_unresolved=unresolved)

        nodes_data = [{"id": nid, **meta} for nid, meta in g.nodes.items()]
        edges_data = [{"from": s, "to": d} for s, d in sorted(g.edges)]
        node_count = len(nodes_data)
        edge_count = len(edges_data)

        return _tr(request, "graph.html", {
            "active": "graph",
            "nodes_json": json.dumps(nodes_data),
            "edges_json": json.dumps(edges_data),
            "title": title,
            "node_count": node_count,
            "edge_count": edge_count,
            "root_name": root,
            "direction": direction,
            "depth": depth,
            "no_unresolved": not unresolved,
        })
    finally:
        db.close()


# ── API Surface ──────────────────────────────────────────────────────────────

@app.get("/api-surface", response_class=HTMLResponse)
def api_surface_page(request: Request) -> HTMLResponse:
    db = _db()
    try:
        rows = _rs(db.api_surface())
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_kind.setdefault(row["kind"], []).append(row)

        return _tr(request, "api_surface.html", {
            "active": "api",
            "by_kind": by_kind,
            "total": len(rows),
        })
    finally:
        db.close()


# ── Dependencies ─────────────────────────────────────────────────────────────

@app.get("/deps", response_class=HTMLResponse)
def deps_page(request: Request) -> HTMLResponse:
    db = _db()
    try:
        extern_crates = _rs(db.all_extern_crates())
        uses = _rs(db.get_use_declarations())

        crate_groups: dict[str, list[str]] = {}
        for r in uses:
            path = r["path"]
            top = path.split("::")[0] if path else "(unknown)"
            crate_groups.setdefault(top, []).append(path)

        return _tr(request, "deps.html", {
            "active": "deps",
            "extern_crates": extern_crates,
            "crate_groups": crate_groups,
        })
    finally:
        db.close()


# ── Search ───────────────────────────────────────────────────────────────────

@app.get("/search", response_class=HTMLResponse)
def search_page(
    request: Request,
    q: str = "",
    kind: str = "",
) -> HTMLResponse:
    db = _db()
    try:
        all_kind_rows = _rs(db.stats()[1])
        all_kinds = [r["kind"] for r in all_kind_rows]

        rows: list[dict[str, Any]] = []
        if q:
            try:
                rows = _rs(db.search(q, kind=kind or None, limit=200))
            except Exception:
                rows = []

        return _tr(request, "search.html", {
            "active": "search",
            "rows": rows,
            "query": q,
            "kind_filter": kind,
            "all_kinds": all_kinds,
        })
    finally:
        db.close()


# ── JSON API ─────────────────────────────────────────────────────────────────

@app.get("/api/item/{item_id}")
def api_item(item_id: int) -> dict[str, Any]:
    db = _db()
    try:
        row = db.get_item(item_id)
        if row is None:
            return {"error": "not found"}
        return _r(row)
    finally:
        db.close()


@app.get("/api/stats")
def api_stats() -> dict[str, Any]:
    db = _db()
    try:
        n_files, rows = db.stats()
        total, resolved = db.call_graph_stats()
        return {
            "files": n_files,
            "items_by_kind": {r["kind"]: r["n"] for r in rows},
            "total_calls": total,
            "resolved_calls": resolved,
        }
    finally:
        db.close()


@app.get("/api/graph-data")
def api_graph_data(
    root: str = "",
    direction: str = "both",
    depth: int = Query(2, ge=1, le=10),
    unresolved: bool = True,
) -> dict[str, Any]:
    from . import graph as graphmod

    db = _db()
    try:
        if root:
            rows = _rs(db.list_items(name=root, limit=50))
            rows = [r for r in rows if r["kind"] in ("function", "method")]
            if rows:
                g = graphmod.build_subgraph(
                    db, [r["id"] for r in rows], depth=depth,
                    direction=direction, include_unresolved=unresolved,
                )
            else:
                g = graphmod.CallGraph()
        else:
            g = graphmod.build_whole_graph(db, include_unresolved=unresolved)

        return {
            "nodes": [{"id": nid, **meta} for nid, meta in g.nodes.items()],
            "edges": [{"from": s, "to": d} for s, d in sorted(g.edges)],
        }
    finally:
        db.close()


# ── Serve helper ─────────────────────────────────────────────────────────────

def create_app(db_path: str) -> FastAPI:
    global _db_path
    _db_path = db_path
    return app
