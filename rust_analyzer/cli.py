#!/usr/bin/env python3
"""rust-analyzer-db: scan Rust source with tree-sitter, store structs/impls/
functions/etc. in a SQLite DB, and query them back later.

Usage:
    python -m rust_analyzer.cli scan <path> [--db rust_code.db]
    python -m rust_analyzer.cli list [--kind struct|enum|trait|impl|function|method] [--name X] [--target X]
    python -m rust_analyzer.cli show <name> [--kind K] [--full]
    python -m rust_analyzer.cli methods <TypeName>
    python -m rust_analyzer.cli search <query>
    python -m rust_analyzer.cli stats
"""

import argparse
import hashlib
import sys
from pathlib import Path

from .db import RustCodeDB
from .extractor import extract_file, ExtractedItem

DEFAULT_DB = "rust_code.db"


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _store_item(db: RustCodeDB, file_id: int, item: ExtractedItem, parent_id=None):
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
    )
    for child in item.children:
        _store_item(db, file_id, child, parent_id=item_id)
    return item_id


def cmd_scan(args):
    root = Path(args.path)
    if not root.exists():
        print(f"Path not found: {root}", file=sys.stderr)
        return 1

    rs_files = [root] if root.is_file() else sorted(root.rglob("*.rs"))
    if not rs_files:
        print("No .rs files found.")
        return 0

    skip_dirs = {"target", ".git"}
    rs_files = [
        f for f in rs_files
        if not any(part in skip_dirs for part in f.parts)
    ]

    db = RustCodeDB(args.db)
    total_items = 0
    scanned = 0
    skipped = 0
    for f in rs_files:
        data = f.read_bytes()
        file_hash = _hash_bytes(data)
        abs_path = str(f.resolve())
        if not args.force and db.file_unchanged(abs_path, file_hash):
            skipped += 1
            continue
        file_id = db.upsert_file(abs_path, f.stat().st_mtime, file_hash)
        try:
            items = extract_file(data)
        except Exception as e:
            print(f"  ! failed to parse {f}: {e}", file=sys.stderr)
            continue
        for item in items:
            _store_item(db, file_id, item)
        db.commit()
        count = sum(1 + len(i.children) for i in items)
        total_items += count
        scanned += 1
        print(f"  {f} -> {count} items")

    print(f"\nScanned {scanned} file(s), skipped {skipped} unchanged, "
          f"extracted {total_items} item(s). DB: {args.db}")
    db.close()
    return 0


def _print_row(row, show_source=False):
    loc = f"{row['file_path']}:{row['start_line']}-{row['end_line']}"
    header = f"[{row['id']}] {row['kind']:<10} {row['name']}"
    if row["target"] and row["kind"] not in ("impl",):
        header += f"  (impl for {row['target']})"
    vis = row["visibility"] or ""
    print(f"{header}   {vis}   {loc}")
    if show_source:
        print("-" * 70)
        print(row["source"])
        print("-" * 70)


def cmd_list(args):
    db = RustCodeDB(args.db)
    rows = db.list_items(
        kind=args.kind, name=args.name, target=args.target,
        file_like=args.file, limit=args.limit,
    )
    if not rows:
        print("No matching items.")
        return 0
    for row in rows:
        _print_row(row)
    print(f"\n{len(rows)} item(s).")
    db.close()
    return 0


def cmd_show(args):
    db = RustCodeDB(args.db)
    rows = db.list_items(kind=args.kind, name=args.name, limit=args.limit)
    if not rows:
        print("No matching items.")
        return 0
    for row in rows:
        if row["doc"]:
            print(row["doc"])
        if row["attributes"]:
            print(row["attributes"])
        _print_row(row, show_source=True)
        print()
    db.close()
    return 0


def cmd_methods(args):
    db = RustCodeDB(args.db)
    rows = db.get_methods_of(args.target)
    if not rows:
        print(f"No methods found for '{args.target}'.")
        return 0
    for row in rows:
        _print_row(row, show_source=args.full)
    print(f"\n{len(rows)} method(s) on types matching '{args.target}'.")
    db.close()
    return 0


def cmd_search(args):
    db = RustCodeDB(args.db)
    try:
        rows = db.search(args.query, kind=args.kind, limit=args.limit)
    except Exception as e:
        print(f"Search error (try quoting the query): {e}", file=sys.stderr)
        return 1
    if not rows:
        print("No matches.")
        return 0
    for row in rows:
        _print_row(row, show_source=args.full)
    print(f"\n{len(rows)} match(es).")
    db.close()
    return 0


def cmd_stats(args):
    db = RustCodeDB(args.db)
    n_files, rows = db.stats()
    print(f"Files scanned: {n_files}")
    print("Items by kind:")
    for row in rows:
        print(f"  {row['kind']:<12} {row['n']}")
    db.close()
    return 0


def build_parser():
    db_parent = argparse.ArgumentParser(add_help=False)
    db_parent.add_argument("--db", default=DEFAULT_DB, help=f"SQLite DB path (default: {DEFAULT_DB})")

    p = argparse.ArgumentParser(prog="rust_analyzer", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 parents=[db_parent])
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="Scan a file or directory of .rs files into the DB", parents=[db_parent])
    s.add_argument("path", help="File or directory to scan")
    s.add_argument("--force", action="store_true", help="Re-parse even unchanged files")
    s.set_defaults(func=cmd_scan)

    l = sub.add_parser("list", help="List items, optionally filtered", parents=[db_parent])
    l.add_argument("--kind", help="struct|enum|trait|impl|function|method|const|static|mod|...")
    l.add_argument("--name", help="Substring match on name")
    l.add_argument("--target", help="Substring match on impl/method target type")
    l.add_argument("--file", help="Substring match on file path")
    l.add_argument("--limit", type=int, default=200)
    l.set_defaults(func=cmd_list)

    sh = sub.add_parser("show", help="Show full source of matching item(s)", parents=[db_parent])
    sh.add_argument("name", help="Exact-ish name (substring match)")
    sh.add_argument("--kind", help="Restrict to a kind")
    sh.add_argument("--limit", type=int, default=20)
    sh.set_defaults(func=cmd_show)

    m = sub.add_parser("methods", help="List methods implemented on a given type/trait target", parents=[db_parent])
    m.add_argument("target", help="Type name, e.g. MyStruct")
    m.add_argument("--full", action="store_true", help="Print full source of each method")
    m.set_defaults(func=cmd_methods)

    se = sub.add_parser("search", help="Full-text search over name/signature/doc/source", parents=[db_parent])
    se.add_argument("query", help="FTS5 query, e.g. 'parse OR tokenize'")
    se.add_argument("--kind", help="Restrict to a kind")
    se.add_argument("--limit", type=int, default=50)
    se.add_argument("--full", action="store_true", help="Print full source of each match")
    se.set_defaults(func=cmd_search)

    st = sub.add_parser("stats", help="Show summary counts", parents=[db_parent])
    st.set_defaults(func=cmd_stats)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
