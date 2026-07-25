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

# Default AGENTS.md content for MCP tool documentation
_AGENTS_MD_TEMPLATE = """# MCP Tools: rust-analyzer-db

This project uses **rust-analyzer-db** for Rust code analysis via MCP tools.

## Quick Start

1. First, scan the project: `scan_project(path="/path/to/project")`
2. Verify scan: `get_stats()`
3. Use analysis tools as needed

## Available Tools

### Scanning & Stats
| Tool | Description |
|------|-------------|
| `scan_project` | Scan Rust project and populate database |
| `get_stats` | Project overview (files, items, call edges) |
| `list_files` | List all scanned files |
| `get_file_info` | Get detailed file information |
| `file_metrics` | Get file-level metrics (complexity, LOC) |

### Querying Code
| Tool | Description |
|------|-------------|
| `list_items` | List code items with filters (kind, name, pub, async, etc.) |
| `get_item` | Get full item details by ID |
| `search_code` | Full-text search (FTS5 syntax) |
| `methods_of` | List methods on a type/trait |
| `get_item_generics` | Get generic type parameters |
| `get_item_lifetimes` | Get lifetime parameters |

### Call Analysis
| Tool | Description |
|------|-------------|
| `call_graph_info` | Get call graph for function or project |
| `callers_of` | Find all callers of a function |
| `callees_of` | Find all callees of a function |

### Code Quality
| Tool | Description |
|------|-------------|
| `complexity_report` | Find complex functions |
| `get_most_complex` | Top N most complex functions |
| `find_unused_imports` | Find unused use declarations |
| `find_dead_code` | Find functions with zero callers |

### Structure & API
| Tool | Description |
|------|-------------|
| `api_surface` | List all public API items |
| `dependencies` | Show external dependencies |
| `module_structure` | Show module hierarchy |
| `implementors_of_trait` | Find types implementing a trait |
| `get_largest_files` | Get largest files by LOC |

## Tips

- Always run `scan_project` first before using other tools
- Use `get_stats()` to verify scan was successful
- Tools return JSON for easy parsing
- Use `force=true` in scan_project to re-san all files

"""


def _get_db() -> RustCodeDB:
    return RustCodeDB(_db_path)


def _rows(rows: list) -> list[dict]:
    return [dict(r) for r in rows]


def _row(row) -> dict:
    if row is None:
        return {}
    return dict(row)


def _ensure_gitignore(project_path: Path, db_filename: str) -> tuple[bool, str]:
    """Ensure the database file is in .gitignore.
    
    Returns (was_added, message) tuple.
    """
    gitignore_path = project_path / ".gitignore"
    
    # Check if .gitignore exists
    if gitignore_path.exists():
        content = gitignore_path.read_text(encoding="utf-8")
        # Check if db is already ignored
        if db_filename in content:
            return False, ""
    else:
        content = ""
    
    # Add to .gitignore
    entry = f"\n# rust-analyzer-db database\n{db_filename}\n"
    
    try:
        if content and not content.endswith("\n"):
            entry = "\n" + entry
        gitignore_path.write_text(content + entry, encoding="utf-8")
        return True, f"Added {db_filename} to .gitignore"
    except Exception as e:
        return False, f"Warning: Could not update .gitignore: {e}"


def _ensure_agents_md(project_path: Path, db_filename: str) -> tuple[bool, str]:
    """Ensure AGENTS.md exists with MCP tool documentation.
    
    Returns (was_created_or_updated, message) tuple.
    """
    agents_path = project_path / "AGENTS.md"
    
    # Check if AGENTS.md already has rust-analyzer-db section
    if agents_path.exists():
        content = agents_path.read_text(encoding="utf-8")
        if "rust-analyzer-db" in content:
            return False, ""
    
    # Create or append AGENTS.md
    template = _AGENTS_MD_TEMPLATE.replace("rust_code.db", db_filename)
    
    try:
        if agents_path.exists():
            # Append to existing file
            content = agents_path.read_text(encoding="utf-8")
            if not content.endswith("\n"):
                template = "\n" + template
            agents_path.write_text(content + template, encoding="utf-8")
            return True, "Appended MCP documentation to AGENTS.md"
        else:
            # Create new file
            agents_path.write_text(template, encoding="utf-8")
            return True, "Created AGENTS.md with MCP documentation"
    except Exception as e:
        return False, f"Warning: Could not update AGENTS.md: {e}"


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

    IMPORTANT: Call this tool FIRST before using any other analysis tools.
    After scanning, use get_stats() to verify the scan was successful,
    then use other tools like list_items(), search_code(), etc.

    Args:
        path: Absolute path to a Rust file or directory to scan.
        force: If true, re-parse even unchanged files. Default false.

    Returns:
        Summary of the scan results with next-step guidance.
    """
    import hashlib
    import time

    from .cli import _hash_bytes, _store_item, _count_items
    from .exceptions import ParseError
    from .extractor import extract_file, extract_calls

    root = Path(path)
    if not root.exists():
        return (
            f"ERROR: Path not found: {path}\n\n"
            "Please provide a valid absolute path to a Rust file or directory."
        )

    rs_files = [root] if root.is_file() else sorted(root.rglob("*.rs"))
    if not rs_files:
        return (
            f"ERROR: No .rs files found in {path}\n\n"
            "Please provide a path containing Rust source files."
        )

    skip_dirs = {"target", ".git", "node_modules", ".cargo"}
    rs_files = [
        f for f in rs_files
        if not any(part in skip_dirs for part in f.parts)
    ]

    if not rs_files:
        return (
            f"ERROR: No .rs files found after excluding build directories.\n"
            f"Searched in: {path}\n"
            f"Excluded directories: {', '.join(skip_dirs)}"
        )

    db = _get_db()
    total_items = 0
    total_uses = 0
    total_externs = 0
    scanned = 0
    skipped = 0
    errors = 0
    error_details = []
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
            except ParseError as e:
                errors += 1
                error_details.append(f"Parse error in {f.name}: {e.reason}")
                continue
            except Exception as e:
                errors += 1
                error_details.append(f"Error in {f.name}: {str(e)[:100]}")
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
        
        # Build detailed result message
        result_parts = []
        
        if scanned > 0:
            result_parts.append(f"SUCCESS: Scanned {scanned} file(s) successfully.")
            result_parts.append(f"  - Extracted {total_items} code items")
            result_parts.append(f"  - Found {total_uses} use declarations")
            result_parts.append(f"  - Found {total_externs} extern crate declarations")
            if skipped > 0:
                result_parts.append(f"  - Skipped {skipped} unchanged files")
            result_parts.append(f"  - Completed in {elapsed:.1f}s")
            
            # Auto-update .gitignore and AGENTS.md
            db_filename = Path(_db_path).name
            project_dir = root if root.is_dir() else root.parent
            
            gitignore_msg = _ensure_gitignore(project_dir, db_filename)
            if gitignore_msg[0]:
                result_parts.append(f"  - {gitignore_msg[1]}")
            
            agents_msg = _ensure_agents_md(project_dir, db_filename)
            if agents_msg[0]:
                result_parts.append(f"  - {agents_msg[1]}")
            
            # Add next-step guidance
            result_parts.append("")
            result_parts.append("NEXT STEPS:")
            result_parts.append("  1. Use get_stats() to see project overview")
            result_parts.append("  2. Use list_items() to browse code entities")
            result_parts.append("  3. Use search_code() to find specific code")
            result_parts.append("  4. Use complexity_report() to find complex functions")
            result_parts.append("  5. Use call_graph_info() to analyze call relationships")
        else:
            result_parts.append("WARNING: Scan completed but no files were processed.")
            if errors > 0:
                result_parts.append(f"  - {errors} file(s) had errors:")
                for detail in error_details[:5]:  # Show first 5 errors
                    result_parts.append(f"    * {detail}")
            if skipped > 0:
                result_parts.append(f"  - {skipped} file(s) were skipped (unchanged)")
            result_parts.append("")
            result_parts.append("Try using force=true to re-scan all files.")
        
        return "\n".join(result_parts)
    finally:
        db.close()


@mcp.tool()
def list_items(
    kind: Optional[str] = None,
    name: Optional[str] = None,
    target: Optional[str] = None,
    file_pattern: Optional[str] = None,
    is_pub: Optional[bool] = None,
    is_async: Optional[bool] = None,
    is_unsafe: Optional[bool] = None,
    min_complexity: Optional[int] = None,
    max_complexity: Optional[int] = None,
    limit: int = 50,
) -> str:
    """List code items (structs, enums, functions, methods, etc.) with optional filters.

    Args:
        kind: Filter by kind (e.g. 'function', 'struct', 'method', 'trait', 'impl').
        name: Substring match on item name.
        target: Substring match on impl/method target type.
        file_pattern: Substring match on file path.
        is_pub: Filter by public visibility (True for public only).
        is_async: Filter by async functions (True for async only).
        is_unsafe: Filter by unsafe functions (True for unsafe only).
        min_complexity: Minimum cyclomatic complexity threshold.
        max_complexity: Maximum cyclomatic complexity threshold.
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
        
        # Apply additional filters
        if is_pub is not None:
            items = [i for i in items if i.get("is_pub") == int(is_pub)]
        if is_async is not None:
            items = [i for i in items if i.get("is_async") == int(is_async)]
        if is_unsafe is not None:
            items = [i for i in items if i.get("is_unsafe") == int(is_unsafe)]
        if min_complexity is not None:
            items = [i for i in items if i.get("cyclomatic_complexity", 0) >= min_complexity]
        if max_complexity is not None:
            items = [i for i in items if i.get("cyclomatic_complexity", 0) <= max_complexity]
        
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
def search_code(query: str, kind: Optional[str] = None, limit: int = 30, include_source: bool = False) -> str:
    """Full-text search across item names, signatures, doc comments, and source code.

    Uses SQLite FTS5 for fast text search. Supports FTS5 query syntax
    (e.g. 'parse OR tokenize', '"exact phrase"', 'NOT keyword').

    Args:
        query: Search query string (FTS5 syntax supported).
        kind: Optional filter by item kind.
        limit: Maximum results (default 30).
        include_source: If true, include full source code in results (default false).

    Returns:
        JSON string with matching items.
    """
    db = _get_db()
    try:
        rows = db.search(query, kind=kind, limit=limit)
        items = _rows(rows)
        if not items:
            return f"No matches found for '{query}'."
        
        # Optionally strip source code to save tokens
        if not include_source:
            for item in items:
                if "source" in item:
                    item["source"] = item["source"][:200] + "..." if len(item["source"]) > 200 else item["source"]
        
        return json.dumps(items, indent=2, default=str)
    finally:
        db.close()


@mcp.tool()
def get_stats() -> str:
    """Get project statistics: file count, item counts by kind, call graph summary.

    Use this tool AFTER scan_project() to verify the scan was successful.
    If total_items is 0, the scan may have failed or the path was incorrect.

    Returns:
        JSON string with project statistics.
    """
    db = _get_db()
    try:
        n_files, kind_rows = db.stats()
        total_calls, resolved_calls = db.call_graph_stats()
        total_items = sum(r["n"] for r in kind_rows)
        
        result = {
            "files": n_files,
            "total_items": total_items,
            "items_by_kind": {r["kind"]: r["n"] for r in kind_rows},
            "total_call_edges": total_calls,
            "resolved_call_edges": resolved_calls,
        }
        
        # Add helpful status message
        if n_files == 0:
            result["status"] = "NO_DATA"
            result["message"] = "No files in database. Run scan_project() first."
        elif total_items == 0:
            result["status"] = "EMPTY"
            result["message"] = "Files exist but no items extracted. Check if scan_project() completed successfully."
        else:
            result["status"] = "OK"
            result["message"] = f"Project has {n_files} files with {total_items} code items."
        
        return json.dumps(result, indent=2)
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
def call_graph_info(function_name: Optional[str] = None, depth: int = 1) -> str:
    """Get call graph information for a function or the whole project.

    If a function name is provided, shows its callers and callees.
    Otherwise, returns overall call graph statistics.

    Args:
        function_name: Name of a function/method to trace. If empty, shows project-wide stats.
        depth: Depth of call chain to explore (default 1, max 5).

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

        # Limit depth to reasonable range
        depth = max(1, min(depth, 5))
        
        results = []
        for row in rows:
            callees = _rows(db.get_calls_from(row["id"]))
            callers = _rows(db.get_calls_to(row["id"]))
            
            # If depth > 1, recursively get callees
            extended_callees = []
            if depth > 1:
                for callee in callees:
                    if callee.get("callee_id"):
                        sub_callees = _rows(db.get_calls_from(callee["callee_id"]))
                        callee["sub_callees"] = sub_callees
                        extended_callees.append(callee)
                callees = extended_callees if extended_callees else callees
            
            results.append({
                "item": _row(row),
                "calls": callees,
                "called_by": callers,
                "depth": depth,
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
# New Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def get_item_generics(item_id: int) -> str:
    """Get generic type parameters for a specific item.

    Args:
        item_id: The numeric ID of the item.

    Returns:
        JSON string with generic parameters including bounds and defaults.
    """
    db = _get_db()
    try:
        row = db.get_item(item_id)
        if row is None:
            return f"No item found with ID {item_id}."
        rows = db.get_generics(item_id)
        items = _rows(rows)
        return json.dumps({
            "item_id": item_id,
            "item_name": row["name"],
            "item_kind": row["kind"],
            "generic_params": items,
            "count": len(items),
        }, indent=2, default=str)
    finally:
        db.close()


@mcp.tool()
def get_item_lifetimes(item_id: int) -> str:
    """Get lifetime parameters for a specific item.

    Args:
        item_id: The numeric ID of the item.

    Returns:
        JSON string with lifetime parameters including bounds.
    """
    db = _get_db()
    try:
        row = db.get_item(item_id)
        if row is None:
            return f"No item found with ID {item_id}."
        rows = db.get_lifetimes(item_id)
        items = _rows(rows)
        return json.dumps({
            "item_id": item_id,
            "item_name": row["name"],
            "item_kind": row["kind"],
            "lifetime_params": items,
            "count": len(items),
        }, indent=2, default=str)
    finally:
        db.close()


@mcp.tool()
def list_files(limit: int = 100) -> str:
    """List all scanned files with item counts and total lines of code.

    Args:
        limit: Maximum number of files to return (default 100).

    Returns:
        JSON string with file information.
    """
    db = _get_db()
    try:
        rows = db.list_files(limit=min(limit, 500))
        items = _rows(rows)
        if not items:
            return "No files found in the database."
        return json.dumps({
            "files": items,
            "count": len(items),
        }, indent=2, default=str)
    finally:
        db.close()


@mcp.tool()
def get_file_info(file_path: str) -> str:
    """Get detailed information about a specific file.

    Args:
        file_path: Path to the file (substring match on file path).

    Returns:
        JSON string with file details including item count and total LOC.
    """
    db = _get_db()
    try:
        row = db.get_file_by_path(file_path)
        if row is None:
            return f"No file found matching '{file_path}'."
        return json.dumps(_row(row), indent=2, default=str)
    finally:
        db.close()


@mcp.tool()
def callers_of(function_name: str) -> str:
    """Find all functions that call a given function.

    Args:
        function_name: Name of the function to find callers for.

    Returns:
        JSON string with all callers of the specified function.
    """
    db = _get_db()
    try:
        rows = db.list_items(name=function_name, limit=10)
        rows = [r for r in rows if r["kind"] in ("function", "method")]
        if not rows:
            return f"No function/method found matching '{function_name}'."

        results = []
        for row in rows:
            callers = _rows(db.get_calls_to(row["id"]))
            results.append({
                "item": _row(row),
                "callers": callers,
                "caller_count": len(callers),
            })
        return json.dumps(results, indent=2, default=str)
    finally:
        db.close()


@mcp.tool()
def callees_of(function_name: str) -> str:
    """Find all functions called by a given function.

    Args:
        function_name: Name of the function to find callees for.

    Returns:
        JSON string with all callees of the specified function.
    """
    db = _get_db()
    try:
        rows = db.list_items(name=function_name, limit=10)
        rows = [r for r in rows if r["kind"] in ("function", "method")]
        if not rows:
            return f"No function/method found matching '{function_name}'."

        results = []
        for row in rows:
            callees = _rows(db.get_calls_from(row["id"]))
            results.append({
                "item": _row(row),
                "callees": callees,
                "callee_count": len(callees),
            })
        return json.dumps(results, indent=2, default=str)
    finally:
        db.close()


@mcp.tool()
def get_most_complex(limit: int = 20) -> str:
    """Get the most complex functions/methods in the project.

    Returns functions sorted by cyclomatic complexity (highest first).

    Args:
        limit: Maximum number of results (default 20).

    Returns:
        JSON string with complex functions including their metrics.
    """
    db = _get_db()
    try:
        rows = db.most_complex_functions(limit=min(limit, 100))
        items = _rows(rows)
        if not items:
            return "No functions found in the database."
        return json.dumps({
            "functions": items,
            "count": len(items),
        }, indent=2, default=str)
    finally:
        db.close()


@mcp.tool()
def get_largest_files(limit: int = 20) -> str:
    """Get the largest files in the project by lines of code.

    Args:
        limit: Maximum number of results (default 20).

    Returns:
        JSON string with largest files including item counts and LOC.
    """
    db = _get_db()
    try:
        rows = db.largest_files(limit=min(limit, 100))
        items = _rows(rows)
        if not items:
            return "No files found in the database."
        return json.dumps({
            "files": items,
            "count": len(items),
        }, indent=2, default=str)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Analysis Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def find_unused_imports(file_path: Optional[str] = None) -> str:
    """Find use declarations that appear unused in the code.

    A use declaration is considered potentially unused if its imported item
    doesn't appear in any function/method source code in the same file.

    Args:
        file_path: Optional file path to check (substring match). If empty, checks all files.

    Returns:
        JSON string with potentially unused imports.
    """
    db = _get_db()
    try:
        if file_path:
            # Get file ID first
            file_row = db.get_file_by_path(file_path)
            if file_row is None:
                return json.dumps({"error": f"No file found matching '{file_path}'."})
            rows = db.find_unused_imports(file_id=file_row["id"])
        else:
            rows = db.find_unused_imports()
        
        items = _rows(rows)
        return json.dumps({
            "unused_imports": items,
            "count": len(items),
            "note": "These are potentially unused - verify manually as some may be used via macros or re-exports." if items else "No unused imports found.",
        }, indent=2, default=str)
    finally:
        db.close()


@mcp.tool()
def implementors_of_trait(trait_name: str) -> str:
    """Find all types that implement a given trait.

    Args:
        trait_name: Name of the trait to find implementors for (substring match).

    Returns:
        JSON string with all impl blocks for the trait.
    """
    db = _get_db()
    try:
        rows = db.implementors_of_trait(trait_name)
        items = _rows(rows)
        return json.dumps({
            "trait": trait_name,
            "implementations": items,
            "count": len(items),
        }, indent=2, default=str)
    finally:
        db.close()


@mcp.tool()
def module_structure() -> str:
    """Show the module hierarchy of the project.

    Returns all mod declarations to understand the project's module structure.

    Returns:
        JSON string with module items showing the project structure.
    """
    db = _get_db()
    try:
        rows = db.module_structure()
        items = _rows(rows)
        
        # Group by file for better readability
        by_file: dict = {}
        for item in items:
            fp = item.get("file_path", "unknown")
            by_file.setdefault(fp, []).append(item)
        
        return json.dumps({
            "modules": items,
            "by_file": by_file,
            "count": len(items),
        }, indent=2, default=str)
    finally:
        db.close()


@mcp.tool()
def find_dead_code(min_complexity: int = 0) -> str:
    """Find functions/methods with zero callers that could potentially be removed.

    Excludes: public API functions, test functions, main/new/default constructors,
    and functions above the complexity threshold (kept as they may be important).

    Args:
        min_complexity: Keep functions with complexity above this threshold (default 0 = find all dead code).

    Returns:
        JSON string with potentially dead code.
    """
    db = _get_db()
    try:
        rows = db.find_dead_code(min_complexity=min_complexity)
        items = _rows(rows)
        return json.dumps({
            "dead_code": items,
            "count": len(items),
            "note": "These functions have no callers - verify they are not part of public API or used via traits/macros." if items else "No potentially dead code found.",
        }, indent=2, default=str)
    finally:
        db.close()


@mcp.tool()
def file_metrics(file_path: Optional[str] = None) -> str:
    """Get aggregated metrics for files.

    Returns file-level statistics: item count, average complexity, total LOC,
    public items, and type counts.

    Args:
        file_path: Optional file path to filter (substring match). If empty, returns all files.

    Returns:
        JSON string with file-level metrics.
    """
    db = _get_db()
    try:
        rows = db.file_metrics(file_path=file_path)
        items = _rows(rows)
        if not items:
            return "No files found." if file_path else "No files in database."
        return json.dumps({
            "files": items,
            "count": len(items),
        }, indent=2, default=str)
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


@mcp.resource("rust-analyzer://stats")
def project_stats() -> str:
    """Project statistics as a resource for quick access."""
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


@mcp.resource("rust-analyzer://files")
def file_list() -> str:
    """List of scanned files as a resource."""
    db = _get_db()
    try:
        rows = db.list_files(limit=100)
        items = _rows(rows)
        return json.dumps({
            "files": items,
            "count": len(items),
        }, indent=2, default=str)
    finally:
        db.close()


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


@mcp.prompt()
def find_dead_code_prompt() -> str:
    """Prompt for identifying potentially unused code."""
    return (
        "Analyze this Rust project for potentially dead code:\n"
        "1. Use get_stats() to understand the project structure\n"
        "2. Use list_items() to find all public functions and methods\n"
        "3. Use callers_of() for each public function to check if it has callers\n"
        "4. Use callees_of() to identify functions that are never called\n"
        "5. Look for functions with zero callers that are not test functions\n"
        "6. Summarize findings with specific recommendations"
    )


@mcp.prompt()
def refactor_suggestion() -> str:
    """Prompt for refactoring suggestions based on complexity metrics."""
    return (
        "Analyze this Rust project for refactoring opportunities:\n"
        "1. Use get_most_complex() to find the most complex functions\n"
        "2. Use complexity_report() with a threshold of 10\n"
        "3. Use call_graph_info() to understand call chains of complex functions\n"
        "4. Use methods_of() to check if complex functions could be split\n"
        "5. Provide specific refactoring suggestions with code examples"
    )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_server(db_path: str) -> None:
    """Start the MCP server over stdio transport."""
    global _db_path
    _db_path = db_path
    mcp.run(transport="stdio")
