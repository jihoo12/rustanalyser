"""SQLite storage layer for extracted Rust code items.

Schema version 2: adds use_declarations, extern_crates, generic_params,
lifetime_params, and complexity_metrics tables for deeper analysis.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional

from .exceptions import DatabaseError
from .logging import get_logger

log = get_logger("db")

SCHEMA_VERSION = 2

SCHEMA = """
-- Files table
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    mtime REAL,
    hash TEXT,
    total_lines INTEGER DEFAULT 0,
    scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Items table (structs, enums, functions, methods, etc.)
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    target TEXT,
    trait_name TEXT,
    visibility TEXT,
    signature TEXT,
    doc TEXT,
    attributes TEXT,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    source TEXT NOT NULL,
    parent_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
    -- new analysis columns
    is_pub INTEGER DEFAULT 0,
    is_const_fn INTEGER DEFAULT 0,
    is_async INTEGER DEFAULT 0,
    is_unsafe INTEGER DEFAULT 0,
    cyclomatic_complexity INTEGER DEFAULT 1,
    cognitive_complexity INTEGER DEFAULT 0,
    nesting_depth INTEGER DEFAULT 0,
    num_branches INTEGER DEFAULT 0,
    num_function_calls INTEGER DEFAULT 0,
    lines_of_code INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_items_name ON items(name);
CREATE INDEX IF NOT EXISTS idx_items_kind ON items(kind);
CREATE INDEX IF NOT EXISTS idx_items_target ON items(target);
CREATE INDEX IF NOT EXISTS idx_items_file ON items(file_id);
CREATE INDEX IF NOT EXISTS idx_items_parent ON items(parent_id);

-- Call edges (function/method call resolution)
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    callee_name TEXT NOT NULL,
    receiver TEXT,
    is_method_call INTEGER NOT NULL,
    line INTEGER,
    callee_id INTEGER REFERENCES items(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller_id);
CREATE INDEX IF NOT EXISTS idx_calls_callee ON calls(callee_id);
CREATE INDEX IF NOT EXISTS idx_calls_callee_name ON calls(callee_name);

-- Use declarations
CREATE TABLE IF NOT EXISTS use_declarations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    alias TEXT,
    is_glob INTEGER DEFAULT 0,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_uses_file ON use_declarations(file_id);
CREATE INDEX IF NOT EXISTS idx_uses_path ON use_declarations(path);

-- Extern crate declarations
CREATE TABLE IF NOT EXISTS extern_crates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    alias TEXT,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_extern_file ON extern_crates(file_id);

-- Generic type parameters
CREATE TABLE IF NOT EXISTS generic_params (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    bounds TEXT,
    default_val TEXT,
    kind TEXT DEFAULT 'type'  -- 'type' or 'const'
);

CREATE INDEX IF NOT EXISTS idx_generics_item ON generic_params(item_id);

-- Lifetime parameters
CREATE TABLE IF NOT EXISTS lifetime_params (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    bounds TEXT
);

CREATE INDEX IF NOT EXISTS idx_lifetimes_item ON lifetime_params(item_id);

-- FTS5 index for full-text search
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    name, target, signature, doc, source, content='items', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, name, target, signature, doc, source)
    VALUES (new.id, new.name, new.target, new.signature, new.doc, new.source);
END;

CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, name, target, signature, doc, source)
    VALUES ('delete', old.id, old.name, old.target, old.signature, old.doc, old.source);
END;

CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, name, target, signature, doc, source)
    VALUES ('delete', old.id, old.name, old.target, old.signature, old.doc, old.source);
    INSERT INTO items_fts(rowid, name, target, signature, doc, source)
    VALUES (new.id, new.name, new.target, new.signature, new.doc, new.source);
END;
"""


class RustCodeDB:
    """SQLite-backed storage for Rust code analysis data."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        try:
            self.conn = sqlite3.connect(db_path)
        except sqlite3.Error as e:
            raise DatabaseError(f"Cannot open database {db_path}: {e}") from e
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> RustCodeDB:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # File management
    # ------------------------------------------------------------------

    def upsert_file(self, path: str, mtime: float, file_hash: str) -> int:
        cur = self.conn.cursor()
        row = cur.execute("SELECT id, hash FROM files WHERE path = ?", (path,)).fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO files(path, mtime, hash) VALUES (?, ?, ?)",
                (path, mtime, file_hash),
            )
            self.conn.commit()
            return cur.lastrowid
        file_id: int = row["id"]
        if row["hash"] != file_hash:
            cur.execute("DELETE FROM items WHERE file_id = ?", (file_id,))
            cur.execute(
                "UPDATE files SET mtime = ?, hash = ? WHERE id = ?",
                (mtime, file_hash, file_id),
            )
            self.conn.commit()
        return file_id

    def update_file_lines(self, file_id: int, total_lines: int) -> None:
        self.conn.execute(
            "UPDATE files SET total_lines = ? WHERE id = ?",
            (total_lines, file_id),
        )

    def file_unchanged(self, path: str, file_hash: str) -> bool:
        row = self.conn.execute(
            "SELECT hash FROM files WHERE path = ?", (path,)
        ).fetchone()
        return row is not None and row["hash"] == file_hash

    # ------------------------------------------------------------------
    # Item management
    # ------------------------------------------------------------------

    def insert_item(
        self,
        file_id: int,
        kind: str,
        name: str,
        start_line: int,
        end_line: int,
        source: str,
        *,
        target: Optional[str] = None,
        trait_name: Optional[str] = None,
        visibility: Optional[str] = None,
        signature: Optional[str] = None,
        doc: Optional[str] = None,
        attributes: Optional[str] = None,
        parent_id: Optional[int] = None,
        is_pub: bool = False,
        is_const_fn: bool = False,
        is_async: bool = False,
        is_unsafe: bool = False,
        cyclomatic_complexity: int = 1,
        cognitive_complexity: int = 0,
        nesting_depth: int = 0,
        num_branches: int = 0,
        num_function_calls: int = 0,
        lines_of_code: int = 0,
    ) -> int:
        cur = self.conn.cursor()
        cur.execute(
            """INSERT INTO items
               (file_id, kind, name, target, trait_name, visibility, signature,
                doc, attributes, start_line, end_line, source, parent_id,
                is_pub, is_const_fn, is_async, is_unsafe,
                cyclomatic_complexity, cognitive_complexity, nesting_depth,
                num_branches, num_function_calls, lines_of_code)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                file_id, kind, name, target, trait_name, visibility, signature,
                doc, attributes, start_line, end_line, source, parent_id,
                int(is_pub), int(is_const_fn), int(is_async), int(is_unsafe),
                cyclomatic_complexity, cognitive_complexity, nesting_depth,
                num_branches, num_function_calls, lines_of_code,
            ),
        )
        return cur.lastrowid

    def insert_generic_params(
        self, item_id: int, generics: list[tuple[str, str, Optional[str]]]
    ) -> None:
        """Insert generic parameters. Each tuple: (name, bounds, default)."""
        cur = self.conn.cursor()
        for name, bounds, default in generics:
            cur.execute(
                "INSERT INTO generic_params (item_id, name, bounds, default_val) VALUES (?,?,?,?)",
                (item_id, name, bounds, default),
            )

    def insert_lifetime_params(
        self, item_id: int, lifetimes: list[tuple[str, str]]
    ) -> None:
        """Insert lifetime parameters. Each tuple: (name, bounds)."""
        cur = self.conn.cursor()
        for name, bounds in lifetimes:
            cur.execute(
                "INSERT INTO lifetime_params (item_id, name, bounds) VALUES (?,?,?)",
                (item_id, name, bounds),
            )

    def commit(self) -> None:
        self.conn.commit()

    # ------------------------------------------------------------------
    # Use declarations / extern crates
    # ------------------------------------------------------------------

    def insert_use_decl(
        self, file_id: int, path: str, alias: Optional[str],
        is_glob: bool, start_line: int, end_line: int,
    ) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO use_declarations (file_id, path, alias, is_glob, start_line, end_line) "
            "VALUES (?,?,?,?,?,?)",
            (file_id, path, alias, int(is_glob), start_line, end_line),
        )
        return cur.lastrowid

    def insert_extern_crate(
        self, file_id: int, name: str, alias: Optional[str],
        start_line: int, end_line: int,
    ) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO extern_crates (file_id, name, alias, start_line, end_line) "
            "VALUES (?,?,?,?,?)",
            (file_id, name, alias, start_line, end_line),
        )
        return cur.lastrowid

    def get_use_declarations(self, file_id: Optional[int] = None) -> list[sqlite3.Row]:
        if file_id is not None:
            return self.conn.execute(
                "SELECT u.*, f.path AS file_path FROM use_declarations u "
                "JOIN files f ON u.file_id = f.id WHERE u.file_id = ? ORDER BY u.start_line",
                (file_id,),
            ).fetchall()
        return self.conn.execute(
            "SELECT u.*, f.path AS file_path FROM use_declarations u "
            "JOIN files f ON u.file_id = f.id ORDER BY f.path, u.start_line"
        ).fetchall()

    def get_extern_crates(self, file_id: Optional[int] = None) -> list[sqlite3.Row]:
        if file_id is not None:
            return self.conn.execute(
                "SELECT e.*, f.path AS file_path FROM extern_crates e "
                "JOIN files f ON e.file_id = f.id WHERE e.file_id = ? ORDER BY e.start_line",
                (file_id,),
            ).fetchall()
        return self.conn.execute(
            "SELECT e.*, f.path AS file_path FROM extern_crates e "
            "JOIN files f ON e.file_id = f.id ORDER BY f.path, e.start_line"
        ).fetchall()

    def all_extern_crates(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT DISTINCT name, alias FROM extern_crates ORDER BY name"
        ).fetchall()

    # ------------------------------------------------------------------
    # Call graph
    # ------------------------------------------------------------------

    def clear_calls_for_file(self, file_id: int) -> None:
        self.conn.execute(
            "DELETE FROM calls WHERE caller_id IN (SELECT id FROM items WHERE file_id = ?)",
            (file_id,),
        )

    def insert_call(
        self,
        caller_id: int,
        callee_name: str,
        line: int,
        receiver: Optional[str] = None,
        is_method_call: bool = False,
    ) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO calls (caller_id, callee_name, receiver, is_method_call, line) "
            "VALUES (?, ?, ?, ?, ?)",
            (caller_id, callee_name, receiver, int(is_method_call), line),
        )
        return cur.lastrowid

    def resolve_calls(self) -> tuple[int, int]:
        """Best-effort static resolution of call edges.

        Returns (total, resolved) counts.
        """
        cur = self.conn.cursor()
        calls = cur.execute(
            """SELECT calls.id, calls.callee_name, calls.receiver, calls.is_method_call,
                      items.target AS caller_target
               FROM calls JOIN items ON calls.caller_id = items.id
               WHERE calls.callee_id IS NULL"""
        ).fetchall()

        resolved = 0
        for call in calls:
            callee_id = None
            if call["is_method_call"]:
                if call["receiver"] == "self" and call["caller_target"]:
                    row = cur.execute(
                        "SELECT id FROM items "
                        "WHERE kind IN ('method') AND name = ? AND target = ? LIMIT 1",
                        (call["callee_name"], call["caller_target"]),
                    ).fetchone()
                    if row:
                        callee_id = row["id"]
                if callee_id is None:
                    row = cur.execute(
                        "SELECT id FROM items WHERE kind = 'method' AND name = ? LIMIT 1",
                        (call["callee_name"],),
                    ).fetchone()
                    if row:
                        callee_id = row["id"]
            else:
                if call["receiver"]:
                    short_receiver = call["receiver"].split("::")[-1]
                    row = cur.execute(
                        "SELECT id FROM items "
                        "WHERE kind = 'method' AND name = ? AND target = ? LIMIT 1",
                        (call["callee_name"], short_receiver),
                    ).fetchone()
                    if row:
                        callee_id = row["id"]
                if callee_id is None:
                    row = cur.execute(
                        "SELECT id FROM items WHERE kind = 'function' AND name = ? LIMIT 1",
                        (call["callee_name"],),
                    ).fetchone()
                    if row:
                        callee_id = row["id"]
            if callee_id is not None:
                cur.execute("UPDATE calls SET callee_id = ? WHERE id = ?", (callee_id, call["id"]))
                resolved += 1
        self.conn.commit()
        total = len(calls) + self.conn.execute(
            "SELECT COUNT(*) AS n FROM calls WHERE callee_id IS NOT NULL"
        ).fetchone()["n"]
        return total, resolved

    def get_calls_from(self, item_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT calls.*, callee.name AS callee_resolved_name, callee.kind AS callee_kind,
                      callee.target AS callee_target
               FROM calls LEFT JOIN items AS callee ON calls.callee_id = callee.id
               WHERE calls.caller_id = ?""",
            (item_id,),
        ).fetchall()

    def get_calls_to(self, item_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT calls.*, caller.name AS caller_name, caller.kind AS caller_kind,
                      caller.target AS caller_target
               FROM calls JOIN items AS caller ON calls.caller_id = caller.id
               WHERE calls.callee_id = ?""",
            (item_id,),
        ).fetchall()

    def find_callable(self, name: str, target: Optional[str] = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM items WHERE kind IN ('function','method') AND name = ?"
        params: list[Any] = [name]
        if target:
            q += " AND (target = ? OR target IS NULL)"
            params.append(target)
        return self.conn.execute(q, params).fetchall()

    def all_call_edges(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT calls.id, calls.caller_id, caller.name AS caller_name,
                      caller.target AS caller_target, caller.kind AS caller_kind,
                      calls.callee_id, calls.callee_name,
                      callee.target AS callee_target, callee.kind AS callee_kind,
                      calls.is_method_call, calls.receiver
               FROM calls
               JOIN items AS caller ON calls.caller_id = caller.id
               LEFT JOIN items AS callee ON calls.callee_id = callee.id"""
        ).fetchall()

    def call_graph_stats(self) -> tuple[int, int]:
        total = self.conn.execute("SELECT COUNT(*) AS n FROM calls").fetchone()["n"]
        resolved = self.conn.execute(
            "SELECT COUNT(*) AS n FROM calls WHERE callee_id IS NOT NULL"
        ).fetchone()["n"]
        return total, resolved

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def list_items(
        self,
        kind: Optional[str] = None,
        name: Optional[str] = None,
        target: Optional[str] = None,
        file_like: Optional[str] = None,
        limit: int = 200,
    ) -> list[sqlite3.Row]:
        q = """SELECT items.*, files.path AS file_path
               FROM items JOIN files ON items.file_id = files.id
               WHERE 1=1"""
        params: list[Any] = []
        if kind:
            q += " AND items.kind = ?"
            params.append(kind)
        if name:
            q += " AND items.name LIKE ?"
            params.append(f"%{name}%")
        if target:
            q += " AND items.target LIKE ?"
            params.append(f"%{target}%")
        if file_like:
            q += " AND files.path LIKE ?"
            params.append(f"%{file_like}%")
        q += " ORDER BY files.path, items.start_line LIMIT ?"
        params.append(limit)
        return self.conn.execute(q, params).fetchall()

    def get_item(self, item_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            """SELECT items.*, files.path AS file_path
               FROM items JOIN files ON items.file_id = files.id
               WHERE items.id = ?""",
            (item_id,),
        ).fetchone()

    def get_methods_of(self, target: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT items.*, files.path AS file_path
               FROM items JOIN files ON items.file_id = files.id
               WHERE items.kind = 'method' AND items.target LIKE ?
               ORDER BY files.path, items.start_line""",
            (f"%{target}%",),
        ).fetchall()

    def search(self, query: str, kind: Optional[str] = None, limit: int = 100) -> list[sqlite3.Row]:
        q = """SELECT items.*, files.path AS file_path
               FROM items_fts
               JOIN items ON items.id = items_fts.rowid
               JOIN files ON items.file_id = files.id
               WHERE items_fts MATCH ?"""
        params: list[Any] = [query]
        if kind:
            q += " AND items.kind = ?"
            params.append(kind)
        q += " ORDER BY rank LIMIT ?"
        params.append(limit)
        return self.conn.execute(q, params).fetchall()

    def stats(self) -> tuple[int, list[sqlite3.Row]]:
        rows = self.conn.execute(
            "SELECT kind, COUNT(*) as n FROM items GROUP BY kind ORDER BY n DESC"
        ).fetchall()
        n_files = self.conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
        return n_files, rows

    def complexity_report(self, min_complexity: int = 5) -> list[sqlite3.Row]:
        """Return functions/methods with cyclomatic complexity >= threshold."""
        return self.conn.execute(
            """SELECT items.*, files.path AS file_path
               FROM items JOIN files ON items.file_id = files.id
               WHERE items.kind IN ('function', 'method')
                 AND items.cyclomatic_complexity >= ?
               ORDER BY items.cyclomatic_complexity DESC""",
            (min_complexity,),
        ).fetchall()

    def api_surface(self) -> list[sqlite3.Row]:
        """Return all public items (the public API surface)."""
        return self.conn.execute(
            """SELECT items.*, files.path AS file_path
               FROM items JOIN files ON items.file_id = files.id
               WHERE items.is_pub = 1
               ORDER BY items.kind, items.name"""
        ).fetchall()

    def get_generics(self, item_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM generic_params WHERE item_id = ? ORDER BY name",
            (item_id,),
        ).fetchall()

    def get_lifetimes(self, item_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM lifetime_params WHERE item_id = ? ORDER BY name",
            (item_id,),
        ).fetchall()

    def most_complex_functions(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT items.*, files.path AS file_path
               FROM items JOIN files ON items.file_id = files.id
               WHERE items.kind IN ('function', 'method')
               ORDER BY items.cyclomatic_complexity DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()

    def largest_files(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT files.*, COUNT(items.id) AS item_count,
                      SUM(items.lines_of_code) AS total_loc
               FROM files
               LEFT JOIN items ON items.file_id = files.id
               GROUP BY files.id
               ORDER BY total_loc DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()

    def list_files(self, limit: int = 100) -> list[sqlite3.Row]:
        """List all scanned files with item counts and total LOC."""
        return self.conn.execute(
            """SELECT files.*, COUNT(items.id) AS item_count,
                      COALESCE(SUM(items.lines_of_code), 0) AS total_loc
               FROM files
               LEFT JOIN items ON items.file_id = files.id
               GROUP BY files.id
               ORDER BY files.path
               LIMIT ?""",
            (limit,),
        ).fetchall()

    def get_file(self, file_id: int) -> Optional[sqlite3.Row]:
        """Get file info by ID with item count."""
        return self.conn.execute(
            """SELECT files.*, COUNT(items.id) AS item_count,
                      COALESCE(SUM(items.lines_of_code), 0) AS total_loc
               FROM files
               LEFT JOIN items ON items.file_id = files.id
               WHERE files.id = ?
               GROUP BY files.id""",
            (file_id,),
        ).fetchone()

    def get_file_by_path(self, path: str) -> Optional[sqlite3.Row]:
        """Get file info by path with item count."""
        return self.conn.execute(
            """SELECT files.*, COUNT(items.id) AS item_count,
                      COALESCE(SUM(items.lines_of_code), 0) AS total_loc
               FROM files
               LEFT JOIN items ON items.file_id = files.id
               WHERE files.path LIKE ?
               GROUP BY files.id""",
            (f"%{path}%",),
        ).fetchone()

    # ------------------------------------------------------------------
    # New analysis queries
    # ------------------------------------------------------------------

    def find_unused_imports(self, file_id: Optional[int] = None) -> list[sqlite3.Row]:
        """Find use declarations that appear unused in source code.
        
        A use declaration is considered potentially unused if its last path
        component doesn't appear in any item's source code in the same file.
        This is a heuristic - false positives are possible.
        """
        if file_id is not None:
            uses = self.conn.execute(
                """SELECT u.*, f.path AS file_path FROM use_declarations u
                   JOIN files f ON u.file_id = f.id WHERE u.file_id = ?""",
                (file_id,),
            ).fetchall()
        else:
            uses = self.conn.execute(
                """SELECT u.*, f.path AS file_path FROM use_declarations u
                   JOIN files f ON u.file_id = f.id"""
            ).fetchall()

        unused = []
        for use in uses:
            path = use["path"]
            # Get the last component of the use path (the item being imported)
            last_component = path.split("::")[-1].strip()
            if not last_component or last_component == "*":
                continue

            # Check if this component appears in any source code in the same file
            fid = use["file_id"]
            count = self.conn.execute(
                """SELECT COUNT(*) AS n FROM items 
                   WHERE file_id = ? AND source LIKE ?""",
                (fid, f"%{last_component}%"),
            ).fetchone()["n"]
            
            if count == 0:
                unused.append(use)
        
        return unused

    def implementors_of_trait(self, trait_name: str) -> list[sqlite3.Row]:
        """Find all types that implement a given trait.
        
        Returns impl blocks and the types they implement the trait for.
        """
        return self.conn.execute(
            """SELECT items.*, files.path AS file_path
               FROM items JOIN files ON items.file_id = files.id
               WHERE items.kind = 'impl' AND items.trait_name LIKE ?
               ORDER BY items.target""",
            (f"%{trait_name}%",),
        ).fetchall()

    def module_structure(self) -> list[sqlite3.Row]:
        """Return all mod items to show module hierarchy."""
        return self.conn.execute(
            """SELECT items.*, files.path AS file_path
               FROM items JOIN files ON items.file_id = files.id
               WHERE items.kind = 'mod'
               ORDER BY files.path, items.start_line"""
        ).fetchall()

    def find_dead_code(self, min_complexity: int = 0) -> list[sqlite3.Row]:
        """Find functions/methods with zero callers that could potentially be removed.
        
        Excludes: pub functions (part of API), test functions, main functions,
        and functions with complexity above threshold (might be important).
        """
        return self.conn.execute(
            """SELECT items.*, files.path AS file_path
               FROM items JOIN files ON items.file_id = files.id
               WHERE items.kind IN ('function', 'method')
                 AND items.name NOT IN ('main', 'new', 'default')
                 AND items.name NOT LIKE 'test_%'
                 AND items.name NOT LIKE '%_test'
                 AND (items.is_pub = 0 OR items.cyclomatic_complexity <= ?)
                 AND items.id NOT IN (
                     SELECT DISTINCT callee_id FROM calls 
                     WHERE callee_id IS NOT NULL
                 )
               ORDER BY items.cyclomatic_complexity DESC""",
            (min_complexity,),
        ).fetchall()

    def file_metrics(self, file_path: Optional[str] = None) -> list[sqlite3.Row]:
        """Get aggregated metrics for files.
        
        Returns file-level stats: item count, avg complexity, total LOC, etc.
        """
        if file_path:
            return self.conn.execute(
                """SELECT files.path, files.total_lines,
                          COUNT(items.id) AS item_count,
                          AVG(items.cyclomatic_complexity) AS avg_complexity,
                          MAX(items.cyclomatic_complexity) AS max_complexity,
                          SUM(items.lines_of_code) AS total_item_loc,
                          SUM(CASE WHEN items.is_pub = 1 THEN 1 ELSE 0 END) AS pub_items,
                          SUM(CASE WHEN items.kind = 'function' THEN 1 ELSE 0 END) AS functions,
                          SUM(CASE WHEN items.kind = 'method' THEN 1 ELSE 0 END) AS methods,
                          SUM(CASE WHEN items.kind IN ('struct', 'enum') THEN 1 ELSE 0 END) AS types
                   FROM files
                   LEFT JOIN items ON items.file_id = files.id
                   WHERE files.path LIKE ?
                   GROUP BY files.id""",
                (f"%{file_path}%",),
            ).fetchall()
        return self.conn.execute(
            """SELECT files.path, files.total_lines,
                      COUNT(items.id) AS item_count,
                      AVG(items.cyclomatic_complexity) AS avg_complexity,
                      MAX(items.cyclomatic_complexity) AS max_complexity,
                      SUM(items.lines_of_code) AS total_item_loc,
                      SUM(CASE WHEN items.is_pub = 1 THEN 1 ELSE 0 END) AS pub_items,
                      SUM(CASE WHEN items.kind = 'function' THEN 1 ELSE 0 END) AS functions,
                      SUM(CASE WHEN items.kind = 'method' THEN 1 ELSE 0 END) AS methods,
                      SUM(CASE WHEN items.kind IN ('struct', 'enum') THEN 1 ELSE 0 END) AS types
               FROM files
               LEFT JOIN items ON items.file_id = files.id
               GROUP BY files.id
               ORDER BY total_item_loc DESC"""
        ).fetchall()
