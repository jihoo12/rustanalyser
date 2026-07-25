"""MCP server for rust-analyzer-db.

Exposes Rust code analysis capabilities as MCP tools, allowing AI
assistants to scan, query, and analyze Rust codebases via the
Model Context Protocol.

Usage:
    rust-analyzer-db mcp --db rust_code.db
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from . import __version__
from .db import RustCodeDB

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_db_path: str = "rust_code.db"


def _get_db() -> RustCodeDB:
    return RustCodeDB(_db_path)


def _rows(rows: list) -> list[dict]:
    return [dict(r) for r in rows]


def _row(row) -> dict:
    if row is None:
        return {}
    return dict(row)


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    f"rust-analyzer-db v{__version__}",
    instructions=(
        "MCP server for Rust code analysis. Provides tools to scan Rust "
        "projects, query code entities, search code, analyze complexity, "
        "inspect call graphs, public API surfaces, and dependencies."
    ),
)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def scan_project(path: str, force: bool = False) -> str:
    """Scan a Rust project directory or file and populate the database.

    Extracts all code items (structs, enums, traits, functions, methods, etc.),
    computes complexity metrics, builds call edges, and stores everything
    in the SQLite database.

    Args:
        path: Absolute path to a Rust file or directory to scan.
        force: If true, re-parse even unchanged files. Default false.

    Returns:
        Summary of the scan results.
    """
    import hashlib
    import time

    from .cli import _hash_bytes, _store_item, _count_items
    from .exceptions import ParseError
    from .extractor import extract_file, extract_calls

    root = Path(path)
    if not root.exists():
        return f"Error: Path not found: {path}"

    rs_files = [root] if root.is_file() else sorted(root.rglob("*.rs"))
    if not rs_files:
        return f"No .rs files found in {path}"

    skip_dirs = {"target", ".git", "node_modules", ".cargo"}
    rs_files = [
        f for f in rs_files
        if not any(part in skip_dirs for part in f.parts)
    ]

    db = _get_db()
    total_items = 0
    total_uses = 0
    total_externs = 0
    scanned = 0
    skipped = 0
    errors = 0
    start_time = time.monotonic()

    try:
        for f in rs_files:
            data = f.read_bytes()
            file_hash = _hash_bytes(data)
            abs_path = str(f.resolve())
            if not force and db.file_unchanged(abs_path, file_hash):
                skipped += 1
                continue

            file_id = db.upsert_file(abs_path, f.stat().st_mtime, file_hash)
            try:
                extraction = extract_file(data)
            except ParseError:
                errors += 1
                continue
            except Exception:
                errors += 1
                continue

            for item in extraction.items:
                _store_item(db, file_id, item, data)

            db.update_file_lines(file_id, extraction.total_lines)

            for use in extraction.use_declarations:
                db.insert_use_decl(
                    file_id, use.path, use.alias, use.is_glob,
                    use.start_line, use.end_line,
                )
                total_uses += 1

            for ext in extraction.extern_crates:
                db.insert_extern_crate(
                    file_id, ext.name, ext.alias, ext.start_line, ext.end_line,
                )
                total_externs += 1

            db.commit()
            count = _count_items(extraction.items)
            total_items += count
            scanned += 1

        if scanned:
            total, resolved = db.resolve_calls()

        elapsed = time.monotonic() - start_time
        return (
            f"Scanned {scanned} file(s), skipped {skipped} unchanged, "
            f"errors {errors}. Extracted {total_items} items, "
            f"{total_uses} use declarations, {total_externs} extern crates. "
            f"({elapsed:.1f}s)"
        )
    finally:
        db.close()


@mcp.tool()
def list_items(
    kind: Optional[str] = None,
    name: Optional[str] = None,
    target: Optional[str] = None,
    file_pattern: Optional[str] = None,
    limit: int = 50,
) -> str:
    """List code items (structs, enums, functions, methods, etc.) with optional filters.

    Args:
        kind: Filter by kind (e.g. 'function', 'struct', 'method', 'trait', 'impl').
        name: Substring match on item name.
        target: Substring match on impl/method target type.
        file_pattern: Substring match on file path.
        limit: Maximum number of results (default 50, max 500).

    Returns:
        JSON string with matching items.
    """
    db = _get_db()
    try:
        rows = db.list_items(
            kind=kind, name=name, target=target,
            file_like=file_pattern, limit=min(limit, 500),
        )
        items = _rows(rows)
        if not items:
            return "No matching items found."
        return json.dumps(items, indent=2, default=str)
    finally:
        db.close()


@mcp.tool()
def get_item(item_id: int) -> str:
    """Get full details and source code for a specific item by ID.

    Args:
        item_id: The numeric ID of the item.

    Returns:
        JSON string with full item details including source code.
    """
    db = _get_db()
    try:
        row = db.get_item(item_id)
        if row is None:
            return f"No item found with ID {item_id}."
        return json.dumps(_row(row), indent=2, default=str)
    finally:
        db.close()


@mcp.tool()
def search_code(query: str, kind: Optional[str] = None, limit: int = 30) -> str:
    """Full-text search across item names, signatures, doc comments, and source code.

    Uses SQLite FTS5 for fast text search. Supports FTS5 query syntax
    (e.g. 'parse OR tokenize', '"exact phrase"', 'NOT keyword').

    Args:
        query: Search query string (FTS5 syntax supported).
        kind: Optional filter by item kind.
        limit: Maximum results (default 30).

    Returns:
        JSON string with matching items.
    """
    db = _get_db()
    try:
        rows = db.search(query, kind=kind, limit=limit)
        items = _rows(rows)
        if not items:
            return f"No matches found for '{query}'."
        return json.dumps(items, indent=2, default=str)
    finally:
        db.close()


@mcp.tool()
def get_stats() -> str:
    """Get project statistics: file count, item counts by kind, call graph summary.

    Returns:
        JSON string with project statistics.
    """
    db = _get_db()
    try:
        n_files, kind_rows = db.stats()
        total_calls, resolved_calls = db.call_graph_stats()
        total_items = sum(r["n"] for r in kind_rows)
        return json.dumps({
            "files": n_files,
            "total_items": total_items,
            "items_by_kind": {r["kind"]: r["n"] for r in kind_rows},
            "total_call_edges": total_calls,
            "resolved_call_edges": resolved_calls,
        }, indent=2)
    finally:
        db.close()


@mcp.tool()
def complexity_report(min_complexity: int = 5) -> str:
    """Report functions/methods with cyclomatic complexity above a threshold.

    Higher complexity indicates harder-to-test and harder-to-maintain code.

    Args:
        min_complexity: Minimum cyclomatic complexity to report (default 5).

    Returns:
        JSON string with complex functions sorted by complexity descending.
    """
    db = _get_db()
    try:
        rows = db.complexity_report(min_complexity=min_complexity)
        items = _rows(rows)
        if not items:
            return f"No functions exceed complexity threshold {min_complexity}."
        return json.dumps(items, indent=2, default=str)
    finally:
        db.close()


@mcp.tool()
def api_surface() -> str:
    """List all public API items (pub functions, structs, traits, etc.).

    Returns:
        JSON string with public items grouped by kind.
    """
    db = _get_db()
    try:
        rows = db.api_surface()
        items = _rows(rows)
        if not items:
            return "No public items found."
        by_kind: dict = {}
        for item in items:
            by_kind.setdefault(item["kind"], []).append(item)
        return json.dumps(by_kind, indent=2, default=str)
    finally:
        db.close()


@mcp.tool()
def dependencies() -> str:
    """Show external dependencies: extern crate declarations and use imports.

    Returns:
        JSON string with extern crates and grouped use declarations.
    """
    db = _get_db()
    try:
        externs = _rows(db.all_extern_crates())
        uses = _rows(db.get_use_declarations())

        crate_groups: dict[str, list[str]] = {}
        for r in uses:
            path = r["path"]
            top = path.split("::")[0] if path else "(unknown)"
            crate_groups.setdefault(top, []).append(path)

        return json.dumps({
            "extern_crates": externs,
            "use_groups": {k: v for k, v in sorted(crate_groups.items())},
            "total_use_declarations": len(uses),
        }, indent=2, default=str)
    finally:
        db.close()


@mcp.tool()
def call_graph_info(function_name: Optional[str] = None) -> str:
    """Get call graph information for a function or the whole project.

    If a function name is provided, shows its callers and callees.
    Otherwise, returns overall call graph statistics.

    Args:
        function_name: Name of a function/method to trace. If empty, shows project-wide stats.

    Returns:
        JSON string with call graph data.
    """
    db = _get_db()
    try:
        if not function_name:
            total, resolved = db.call_graph_stats()
            return json.dumps({
                "total_call_edges": total,
                "resolved_call_edges": resolved,
                "unresolved": total - resolved,
            }, indent=2)

        rows = db.list_items(name=function_name, limit=10)
        rows = [r for r in rows if r["kind"] in ("function", "method")]
        if not rows:
            return f"No function/method found matching '{function_name}'."

        results = []
        for row in rows:
            item = _row(row)
            callees = _rows(db.get_calls_from(row["id"]))
            callers = _rows(db.get_calls_to(row["id"]))
            results.append({
                "item": item,
                "calls": callees,
                "called_by": callers,
            })
        return json.dumps(results, indent=2, default=str)
    finally:
        db.close()


@mcp.tool()
def methods_of(type_name: str) -> str:
    """List all methods defined on a type or trait.

    Args:
        type_name: Name of the type or trait (substring match).

    Returns:
        JSON string with methods including their signatures and complexity.
    """
    db = _get_db()
    try:
        rows = db.get_methods_of(type_name)
        items = _rows(rows)
        if not items:
            return f"No methods found for type matching '{type_name}'."
        return json.dumps(items, indent=2, default=str)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@mcp.resource("rust-analyzer://schema")
def db_schema() -> str:
    """Database schema documentation describing all tables and columns."""
    return """# rust-analyzer-db Schema

## Tables

### files
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| path | TEXT | Absolute file path (unique) |
| mtime | REAL | File modification time |
| hash | TEXT | Content hash (SHA-256) |
| total_lines | INTEGER | Total lines in file |
| scanned_at | TIMESTAMP | When the file was scanned |

### items
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| file_id | INTEGER | FK to files |
| kind | TEXT | struct/enum/trait/impl/function/method/const/static/mod/macro/type_alias/union |
| name | TEXT | Item name |
| target | TEXT | impl target type |
| trait_name | TEXT | Trait name (for impl blocks) |
| visibility | TEXT | pub, pub(crate), etc. |
| signature | TEXT | Function signature (no body) |
| doc | TEXT | Doc comments |
| attributes | TEXT | #[derive(...)] etc. |
| start_line / end_line | INTEGER | Line range |
| source | TEXT | Full source text |
| is_pub | INTEGER | 1 if public |
| cyclomatic_complexity | INTEGER | Cyclomatic complexity |
| cognitive_complexity | INTEGER | Cognitive complexity |
| nesting_depth | INTEGER | Max nesting depth |
| num_branches | INTEGER | Branch count |
| num_function_calls | INTEGER | Function call count |
| lines_of_code | INTEGER | Lines of code |

### calls
| Column | Type | Description |
|--------|------|-------------|
| caller_id | INTEGER | FK to items (caller) |
| callee_name | TEXT | Name of called function |
| callee_id | INTEGER | FK to items (resolved) |
| is_method_call | INTEGER | 1 if method call |
| receiver | TEXT | Receiver object (e.g. 'self') |

### use_declarations
| Column | Type | Description |
|--------|------|-------------|
| file_id | INTEGER | FK to files |
| path | TEXT | Import path |
| alias | TEXT | Import alias |

### extern_crates
| Column | Type | Description |
|--------|------|-------------|
| name | TEXT | Crate name |
| alias | TEXT | Crate alias |

### generic_params / lifetime_params
| Column | Type | Description |
|--------|------|-------------|
| item_id | INTEGER | FK to items |
| name | TEXT | Parameter name |
| bounds | TEXT | Trait bounds |
"""


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


@mcp.prompt()
def review_function(name: str) -> str:
    """Prompt for reviewing a specific function's quality and complexity."""
    return (
        f"Please review the function '{name}' in the codebase. "
        f"Use the call_graph_info tool to see its callers and callees, "
        f"then use get_item to read its source code. "
        f"Evaluate its complexity, testability, and suggest improvements."
    )


@mcp.prompt()
def analyze_project() -> str:
    """Prompt for a comprehensive project analysis."""
    return (
        "Analyze this Rust project comprehensively:\n"
        "1. Use get_stats() for an overview\n"
        "2. Use complexity_report() to find hotspots\n"
        "3. Use api_surface() to review the public API\n"
        "4. Use dependencies() to understand external deps\n"
        "5. Summarize findings and recommendations"
    )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_server(db_path: str) -> None:
    """Start the MCP server over stdio transport."""
    global _db_path
    _db_path = db_path
    mcp.run(transport="stdio")
