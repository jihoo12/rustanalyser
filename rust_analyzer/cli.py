#!/usr/bin/env python3
"""rust-analyzer-db: Professional Rust code analysis tool.

Scans Rust source with tree-sitter, extracts structs/impls/functions/etc.
into a SQLite DB, and provides query, search, call-graph, complexity
analysis, and public-API surface commands.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

from .db import RustCodeDB
from .exceptions import AnalysisError, ParseError
from .extractor import ExtractedItem, FileExtraction, extract_file, extract_calls
from . import graph as graphmod
from .logging import get_logger, setup_logging

log = get_logger("cli")

DEFAULT_DB = "rust_code.db"


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _store_item(
    db: RustCodeDB,
    file_id: int,
    item: ExtractedItem,
    src: bytes,
    parent_id: int | None = None,
) -> int:
    complexity = item.complexity
    item_id = db.insert_item(
        file_id=file_id,
        kind=item.kind,
        name=item.name,
        start_line=item.start_line,
        end_line=item.end_line,
        source=item.source,
        target=item.target,
        trait_name=item.trait_name,
        visibility=item.visibility,
        signature=item.signature,
        doc=item.doc,
        attributes=item.attributes,
        parent_id=parent_id,
        is_pub=item.is_pub,
        is_const_fn=item.is_const_fn,
        is_async=item.is_async,
        is_unsafe=item.is_unsafe,
        cyclomatic_complexity=complexity.cyclomatic if complexity else 1,
        cognitive_complexity=complexity.cognitive if complexity else 0,
        nesting_depth=complexity.nesting_depth if complexity else 0,
        num_branches=complexity.num_branches if complexity else 0,
        num_function_calls=complexity.num_function_calls if complexity else 0,
        lines_of_code=complexity.lines_of_code if complexity else 0,
    )
    # store generic params
    if item.generic_params:
        db.insert_generic_params(
            item_id,
            [(g.name, ", ".join(g.bounds), g.default) for g in item.generic_params],
        )
    # store lifetime params
    if item.lifetime_params:
        db.insert_lifetime_params(
            item_id,
            [(l.name, ", ".join(l.bounds)) for l in item.lifetime_params],
        )
    # extract calls from function/method bodies
    if item.body_node is not None and item.kind in ("function", "method"):
        for edge in extract_calls(item.body_node, src):
            db.insert_call(
                caller_id=item_id,
                callee_name=edge.callee_name,
                line=edge.line,
                receiver=edge.receiver,
                is_method_call=edge.is_method_call,
            )
    for child in item.children:
        _store_item(db, file_id, child, src, parent_id=item_id)
    return item_id


def _output_json(data: Any) -> None:
    """Print data as JSON to stdout."""
    print(json.dumps(data, indent=2, default=str))


# ------------------------------------------------------------------
# Auto .gitignore / AGENTS.md helpers
# ------------------------------------------------------------------

_AGENTS_MD_TEMPLATE = """\
# MCP Tools: rust-analyzer-db

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

### Code Querying
| Tool | Description |
|------|-------------|
| `search_code` | Search code by name, kind, pattern |
| `list_items` | List code items (with rich filtering) |
| `get_item` | Get specific code item details |
| `get_item_children` | Get child items (methods in impl, etc.) |
| `get_item_source` | Get source code of a code item |
| `get_item_generics` | Get generic params of a code item |
| `get_item_lifetimes` | Get lifetime params of a code item |
| `list_use_decls` | List use declarations |
| `list_extern_crates` | List extern crate declarations |

### Analysis
| Tool | Description |
|------|-------------|
| `complexity_report` | Find complex code |
| `most_complex_items` | Most complex items in project |
| `get_item_complexity` | Get complexity details |
| `call_graph_info` | Call graph analysis |
| `api_surface` | Public API surface |
| `module_structure` | Module hierarchy |

### Finding
| Tool | Description |
|------|-------------|
| `find_item` | Find item by name (fuzzy) |
| `callers_of` | Find functions that call a given function |
| `callees_of` | Find functions called by a given function |
| `find_unused_imports` | Find use declarations not referenced |
| `implementors_of_trait` | Find types implementing a trait |

### Large/Complex
| Tool | Description |
|------|-------------|
| `get_largest_files` | Largest files by LOC |
| `get_most_complex` | Most complex items |
| `find_dead_code` | Find unreferenced functions/classes |
| `file_metrics` | File-level metrics |

## Typical Flow

1. `scan_project(path="/path/to/rust/project")` → populate DB
2. `get_stats()` → verify extraction
3. `search_code(query="fn", kind="function")` → browse functions
4. `complexity_report(threshold=5)` → find hotspots
5. `call_graph_info(name="main")` → understand call flow
6. `api_surface()` → review public API

## Tips

- Use `kind` filter: "function", "method", "struct", "impl", "trait", etc.
- Results are paginated — use `offset` and `limit`.
- `call_graph_info` shows callers and callees (use `depth` 1-5).
- `complexity_report` sorts by cyclomatic complexity.
"""


def _ensure_gitignore(project_path: Path, db_filename: str) -> tuple[bool, str]:
    """Add db_filename to .gitignore if not already present. Returns (was_added, message)."""
    gitignore_path = project_path / ".gitignore"

    if gitignore_path.exists():
        content = gitignore_path.read_text(encoding="utf-8")
        if db_filename in content:
            return False, ""
    else:
        content = ""

    entry = f"\n# rust-analyzer-db database\n{db_filename}\n"

    try:
        if content and not content.endswith("\n"):
            entry = "\n" + entry
        gitignore_path.write_text(content + entry, encoding="utf-8")
        return True, f"Added {db_filename} to .gitignore"
    except Exception as e:
        return False, f"Warning: Could not update .gitignore: {e}"


def _ensure_agents_md(project_path: Path, db_filename: str) -> tuple[bool, str]:
    """Create or append AGENTS.md with MCP tool documentation. Returns (was_added, message)."""
    agents_path = project_path / "AGENTS.md"

    if agents_path.exists():
        existing = agents_path.read_text(encoding="utf-8")
        if "rust-analyzer-db" in existing:
            return False, ""

        append = f"\n\n---\n\n<!-- code-review-graph MCP tools -->\n{_AGENTS_MD_TEMPLATE}"
        try:
            if not existing.endswith("\n"):
                append = "\n" + append
            agents_path.write_text(existing + append, encoding="utf-8")
            return True, "Appended rust-analyzer-db section to AGENTS.md"
        except Exception as e:
            return False, f"Warning: Could not update AGENTS.md: {e}"
    else:
        try:
            agents_path.write_text(_AGENTS_MD_TEMPLATE, encoding="utf-8")
            return True, "Created AGENTS.md with MCP documentation"
        except Exception as e:
            return False, f"Warning: Could not create AGENTS.md: {e}"


def _output_rows(rows: list[Any], fields: list[str] | None = None) -> list[dict[str, Any]]:
    """Convert rows to list of dicts."""
    return [dict(r) for r in rows]


# ------------------------------------------------------------------
# Commands
# ------------------------------------------------------------------

def cmd_scan(args: argparse.Namespace) -> int:
    """Scan Rust source files and populate the database."""
    root = Path(args.path)
    if not root.exists():
        log.error("Path not found: %s", root)
        return 1

    rs_files = [root] if root.is_file() else sorted(root.rglob("*.rs"))
    if not rs_files:
        log.info("No .rs files found.")
        return 0

    skip_dirs = {"target", ".git", "node_modules", ".cargo"}
    rs_files = [
        f for f in rs_files
        if not any(part in skip_dirs for part in f.parts)
    ]

    db = RustCodeDB(args.db)
    total_items = 0
    total_uses = 0
    total_externs = 0
    scanned = 0
    skipped = 0
    errors = 0
    start_time = time.monotonic()

    for i, f in enumerate(rs_files):
        if not args.quiet:
            progress = f"[{i+1}/{len(rs_files)}]" if len(rs_files) > 1 else ""
            print(f"\r  {progress} Scanning {f}...", end="", flush=True) if len(rs_files) > 10 else None

        data = f.read_bytes()
        file_hash = _hash_bytes(data)
        abs_path = str(f.resolve())
        if not args.force and db.file_unchanged(abs_path, file_hash):
            skipped += 1
            continue

        file_id = db.upsert_file(abs_path, f.stat().st_mtime, file_hash)
        try:
            extraction = extract_file(data)
        except ParseError as e:
            log.warning("Parse error in %s: %s", f, e.reason)
            errors += 1
            continue
        except Exception as e:
            log.error("Failed to parse %s: %s", f, e)
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
        if not args.quiet and len(rs_files) <= 10:
            log.info("  %s -> %d items (%d LOC)", f, count, extraction.total_lines)

    elapsed = time.monotonic() - start_time

    if scanned:
        total, resolved = db.resolve_calls()
        log.info("Resolved %d/%d call edges.", resolved, total)

    if not args.quiet:
        print()  # newline after progress
    log.info(
        "Scanned %d file(s), skipped %d unchanged, errors %d, "
        "extracted %d items, %d use decls, %d extern crates. "
        "DB: %s (%.1fs)",
        scanned, skipped, errors, total_items, total_uses, total_externs,
        args.db, elapsed,
    )

    # Auto-update .gitignore and AGENTS.md
    if scanned > 0:
        db_filename = Path(args.db).name
        project_dir = root if root.is_dir() else root.parent
        gitignore_added, gitignore_msg = _ensure_gitignore(project_dir, db_filename)
        agents_added, agents_msg = _ensure_agents_md(project_dir, db_filename)
        if gitignore_added:
            log.info("  %s", gitignore_msg)
        if agents_added:
            log.info("  %s", agents_msg)

    db.close()
    return 0


def _count_items(items: list[ExtractedItem]) -> int:
    """Recursively count items including children."""
    count = len(items)
    for item in items:
        count += _count_items(item.children)
    return count


def _print_row(row: Any, show_source: bool = False) -> None:
    loc = f"{row['file_path']}:{row['start_line']}-{row['end_line']}"
    header = f"[{row['id']}] {row['kind']:<12} {row['name']}"
    if row["target"] and row["kind"] not in ("impl",):
        header += f"  (impl for {row['target']})"
    vis = row["visibility"] or ""
    cx = ""
    if row["cyclomatic_complexity"] and row["cyclomatic_complexity"] > 1:
        cx = f"  CC={row['cyclomatic_complexity']}"
    print(f"{header}   {vis}{cx}   {loc}")
    if show_source:
        print("-" * 70)
        print(row["source"])
        print("-" * 70)


def cmd_list(args: argparse.Namespace) -> int:
    """List items with optional filters."""
    db = RustCodeDB(args.db)
    rows = db.list_items(
        kind=args.kind, name=args.name, target=args.target,
        file_like=args.file, limit=args.limit,
    )
    if not rows:
        log.info("No matching items.")
        return 0

    if args.json:
        _output_json(_output_rows(rows))
    else:
        for row in rows:
            _print_row(row)
        log.info("%d item(s).", len(rows))
    db.close()
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """Show full source code of matching items."""
    db = RustCodeDB(args.db)
    rows = db.list_items(kind=args.kind, name=args.name, limit=args.limit)
    if not rows:
        log.info("No matching items.")
        return 0

    if args.json:
        _output_json(_output_rows(rows))
    else:
        for row in rows:
            if row["doc"]:
                print(row["doc"])
            if row["attributes"]:
                print(row["attributes"])
            _print_row(row, show_source=True)
            print()
    db.close()
    return 0


def cmd_methods(args: argparse.Namespace) -> int:
    """List methods on a type or trait."""
    db = RustCodeDB(args.db)
    rows = db.get_methods_of(args.target)
    if not rows:
        log.info("No methods found for '%s'.", args.target)
        return 0

    if args.json:
        _output_json(_output_rows(rows))
    else:
        for row in rows:
            _print_row(row, show_source=args.full)
        log.info("%d method(s) on types matching '%s'.", len(rows), args.target)
    db.close()
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """Full-text search over name/signature/doc/source."""
    db = RustCodeDB(args.db)
    try:
        rows = db.search(args.query, kind=args.kind, limit=args.limit)
    except Exception as e:
        log.error("Search error (try quoting the query): %s", e)
        return 1
    if not rows:
        log.info("No matches.")
        return 0

    if args.json:
        _output_json(_output_rows(rows))
    else:
        for row in rows:
            _print_row(row, show_source=args.full)
        log.info("%d match(es).", len(rows))
    db.close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Start the web frontend server."""
    try:
        import uvicorn
    except ImportError:
        log.error("uvicorn is required for the web server. Install with: pip install uvicorn")
        return 1

    from .web import create_app

    if not Path(args.db).exists():
        log.error("Database not found: %s. Run 'scan' first.", args.db)
        return 1

    app = create_app(args.db)
    log.info("Starting web server at http://%s:%d", args.host, args.port)
    log.info("Database: %s", args.db)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    """Start the MCP server over stdio transport."""
    from .mcp_server import run_server

    if not Path(args.db).exists():
        log.error("Database not found: %s. Run 'scan' first.", args.db)
        return 1

    log.info("Starting MCP server (db: %s)", args.db)
    run_server(args.db)
    return 0


def cmd_graph(args: argparse.Namespace) -> int:
    """Render a call graph."""
    db = RustCodeDB(args.db)

    if args.root:
        rows = db.list_items(name=args.root, limit=50)
        rows = [r for r in rows if r["kind"] in ("function", "method")]
        if not rows:
            log.error("No function/method found matching '%s'.", args.root)
            return 1
        root_ids = [r["id"] for r in rows]
        names = ", ".join(f"{r['name']} ({r['file_path']}:{r['start_line']})" for r in rows)
        log.info("Root node(s): %s", names)
        g = graphmod.build_subgraph(
            db, root_ids, depth=args.depth, direction=args.direction,
            include_unresolved=not args.no_unresolved,
        )
        title = f"Call graph: {args.root} (depth {args.depth}, {args.direction})"
    else:
        kinds = set(args.kind.split(",")) if args.kind else None
        g = graphmod.build_whole_graph(db, include_unresolved=not args.no_unresolved, kinds=kinds)
        title = "Call graph (whole project)"

    if not g.nodes:
        log.info("No call edges found. Did you run `scan` on this project?")
        return 0

    log.info("Graph: %d node(s), %d edge(s).", len(g.nodes), len(g.edges))
    if len(g.nodes) > 400:
        log.warning(
            "Large graph — use --root <function> to focus or --no-unresolved "
            "to drop external calls."
        )

    fmt = args.format
    out = args.output

    if args.json:
        _output_json({
            "title": title,
            "node_count": len(g.nodes),
            "edge_count": len(g.edges),
            "nodes": [{"id": nid, **meta} for nid, meta in g.nodes.items()],
            "edges": [{"from": s, "to": d} for s, d in sorted(g.edges)],
        })
        db.close()
        return 0

    if fmt == "dot":
        dot_str = graphmod.to_dot(g, title)
        Path(out).write_text(dot_str)
        log.info("Wrote DOT file: %s", out)
    elif fmt == "html":
        html_str = graphmod.to_html(g, title)
        Path(out).write_text(html_str)
        log.info("Wrote interactive HTML: %s (open in a browser)", out)
    elif fmt in ("svg", "png", "pdf"):
        dot_str = graphmod.to_dot(g, title)
        try:
            ok = graphmod.render_dot(dot_str, out, fmt)
        except RuntimeError as e:
            log.error("Graphviz render failed: %s", e)
            return 1
        if not ok:
            fallback = str(Path(out).with_suffix(".dot"))
            Path(fallback).write_text(dot_str)
            log.warning("Graphviz 'dot' not found; wrote DOT to %s", fallback)
        else:
            log.info("Wrote %s: %s", fmt.upper(), out)
    db.close()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """Show project statistics."""
    db = RustCodeDB(args.db)
    n_files, rows = db.stats()
    total, resolved = db.call_graph_stats()

    if args.json:
        data = {
            "files": n_files,
            "items_by_kind": {r["kind"]: r["n"] for r in rows},
            "total_calls": total,
            "resolved_calls": resolved,
        }
        _output_json(data)
    else:
        total_items = sum(r["n"] for r in rows)
        print(f"Files scanned:     {n_files}")
        print(f"Total items:       {total_items}")
        print(f"Call edges:        {total} ({resolved} resolved)")
        print()
        print("Items by kind:")
        for row in rows:
            bar = "█" * min(row["n"], 50)
            print(f"  {row['kind']:<14} {row['n']:>6}  {bar}")
    db.close()
    return 0


def cmd_complexity(args: argparse.Namespace) -> int:
    """Show complexity report for functions/methods."""
    db = RustCodeDB(args.db)
    rows = db.complexity_report(min_complexity=args.min_complexity)

    if not rows:
        log.info("No functions exceed complexity threshold %d.", args.min_complexity)
        return 0

    if args.json:
        _output_json(_output_rows(rows))
    else:
        print(f"{'Kind':<10} {'Name':<35} {'CC':>4} {'Cog':>4} {'Nest':>5} {'LOC':>5}  Location")
        print("-" * 90)
        for row in rows:
            loc = f"{row['file_path']}:{row['start_line']}"
            print(
                f"{row['kind']:<10} {row['name']:<35} "
                f"{row['cyclomatic_complexity']:>4} {row['cognitive_complexity']:>4} "
                f"{row['nesting_depth']:>5} {row['lines_of_code']:>5}  {loc}"
            )
        log.info("%d function(s) above complexity threshold.", len(rows))
    db.close()
    return 0


def cmd_api(args: argparse.Namespace) -> int:
    """Show the public API surface of the project."""
    db = RustCodeDB(args.db)
    rows = db.api_surface()

    if not rows:
        log.info("No public items found.")
        return 0

    if args.json:
        _output_json(_output_rows(rows))
    else:
        by_kind: dict[str, list[Any]] = {}
        for row in rows:
            by_kind.setdefault(row["kind"], []).append(row)
        for kind in ("struct", "enum", "trait", "function", "method", "const", "type_alias", "union"):
            items = by_kind.get(kind, [])
            if not items:
                continue
            print(f"\n{kind.upper()} ({len(items)}):")
            for row in items:
                vis = row["visibility"] or ""
                loc = f"{row['file_path']}:{row['start_line']}"
                sig = f"  {row['signature']}" if row["signature"] else ""
                print(f"  {vis} {row['name']}{sig}  -- {loc}")
        log.info("%d public item(s) total.", len(rows))
    db.close()
    return 0


def cmd_deps(args: argparse.Namespace) -> int:
    """Show external dependencies (use/extern crate)."""
    db = RustCodeDB(args.db)

    externs = db.all_extern_crates()
    uses = db.get_use_declarations()

    if args.json:
        _output_json({
            "extern_crates": [dict(r) for r in externs],
            "use_declarations": [dict(r) for r in uses],
        })
    else:
        if externs:
            print("External crates:")
            for r in externs:
                alias = f" as {r['alias']}" if r["alias"] else ""
                print(f"  {r['name']}{alias}")
        else:
            print("No extern crate declarations found.")

        # Group use paths by top-level crate
        crate_uses: dict[str, list[str]] = {}
        for r in uses:
            path = r["path"]
            top = path.split("::")[0]
            crate_uses.setdefault(top, []).append(path)

        if crate_uses:
            print(f"\nUse declarations ({len(crate_uses)} top-level crates):")
            for crate_name in sorted(crate_uses):
                paths = crate_uses[crate_name]
                print(f"  {crate_name} ({len(paths)} imports)")
                if args.full:
                    for p in sorted(paths):
                        print(f"    use {p};")
    db.close()
    return 0


# ------------------------------------------------------------------
# CLI parser
# ------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    db_parent = argparse.ArgumentParser(add_help=False)
    db_parent.add_argument("--db", default=DEFAULT_DB, help=f"SQLite DB path (default: {DEFAULT_DB})")

    output_parent = argparse.ArgumentParser(add_help=False)
    output_parent.add_argument("--json", action="store_true", help="Output as JSON")

    p = argparse.ArgumentParser(
        prog="rust-analyzer-db",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[db_parent],
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    sub = p.add_subparsers(dest="command", required=True)

    # scan
    s = sub.add_parser("scan", help="Scan a file or directory of .rs files", parents=[db_parent])
    s.add_argument("path", help="File or directory to scan")
    s.add_argument("--force", action="store_true", help="Re-parse even unchanged files")
    s.add_argument("-q", "--quiet", action="store_true", help="Suppress progress output")
    s.set_defaults(func=cmd_scan)

    # list
    l = sub.add_parser("list", help="List items, optionally filtered", parents=[db_parent, output_parent])
    l.add_argument("--kind", help="struct|enum|trait|impl|function|method|const|static|mod|...")
    l.add_argument("--name", help="Substring match on name")
    l.add_argument("--target", help="Substring match on impl/method target type")
    l.add_argument("--file", help="Substring match on file path")
    l.add_argument("--limit", type=int, default=200)
    l.set_defaults(func=cmd_list)

    # show
    sh = sub.add_parser("show", help="Show full source of matching item(s)", parents=[db_parent, output_parent])
    sh.add_argument("name", help="Exact-ish name (substring match)")
    sh.add_argument("--kind", help="Restrict to a kind")
    sh.add_argument("--limit", type=int, default=20)
    sh.set_defaults(func=cmd_show)

    # methods
    m = sub.add_parser("methods", help="List methods on a type/trait", parents=[db_parent, output_parent])
    m.add_argument("target", help="Type name, e.g. MyStruct")
    m.add_argument("--full", action="store_true", help="Print full source of each method")
    m.set_defaults(func=cmd_methods)

    # search
    se = sub.add_parser("search", help="Full-text search over name/signature/doc/source", parents=[db_parent, output_parent])
    se.add_argument("query", help="FTS5 query, e.g. 'parse OR tokenize'")
    se.add_argument("--kind", help="Restrict to a kind")
    se.add_argument("--limit", type=int, default=50)
    se.add_argument("--full", action="store_true", help="Print full source of each match")
    se.set_defaults(func=cmd_search)

    # stats
    st = sub.add_parser("stats", help="Show summary counts", parents=[db_parent, output_parent])
    st.set_defaults(func=cmd_stats)

    # complexity
    cx = sub.add_parser("complexity", help="Show cyclomatic complexity report", parents=[db_parent, output_parent])
    cx.add_argument("--min", dest="min_complexity", type=int, default=5,
                    help="Minimum complexity to report (default 5)")
    cx.set_defaults(func=cmd_complexity)

    # api
    api = sub.add_parser("api", help="Show public API surface", parents=[db_parent, output_parent])
    api.set_defaults(func=cmd_api)

    # deps
    dp = sub.add_parser("deps", help="Show external dependencies (use/extern crate)", parents=[db_parent, output_parent])
    dp.add_argument("--full", action="store_true", help="Show all use paths (not just crate summary)")
    dp.set_defaults(func=cmd_deps)

    # graph
    g = sub.add_parser("graph", help="Render a call graph (execution flow)", parents=[db_parent, output_parent])
    g.add_argument("--root", help="Function/method name to center on (substring match)")
    g.add_argument("--depth", type=int, default=2, help="BFS depth from --root (default 2)")
    g.add_argument("--direction", choices=["callees", "callers", "both"], default="both",
                   help="With --root: callees, callers, or both (default both)")
    g.add_argument("--kind", help="Whole-graph only: comma-separated caller kinds")
    g.add_argument("--no-unresolved", action="store_true",
                   help="Omit calls that couldn't be resolved")
    g.add_argument("--format", choices=["svg", "png", "pdf", "dot", "html"], default="svg",
                   help="Output format (default svg)")
    g.add_argument("-o", "--output", default="callgraph.svg", help="Output file path")
    g.set_defaults(func=cmd_graph)

    # serve
    sv = sub.add_parser("serve", help="Start the web frontend server", parents=[db_parent])
    sv.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    sv.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    sv.set_defaults(func=cmd_serve)

    # mcp
    mc = sub.add_parser("mcp", help="Start the MCP server (Model Context Protocol)", parents=[db_parent])
    mc.set_defaults(func=cmd_mcp)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(verbose=getattr(args, "verbose", False))
    try:
        return args.func(args)
    except AnalysisError as e:
        log.error("%s", e)
        return 1
    except KeyboardInterrupt:
        log.warning("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
