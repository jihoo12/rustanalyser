"""SQLite storage layer for extracted Rust code items."""

import sqlite3
from pathlib import Path
from typing import Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    mtime REAL,
    hash TEXT
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,          -- struct, enum, trait, impl, function, method, const, static, mod, macro, type_alias
    name TEXT NOT NULL,          -- item name (e.g. struct name, fn name)
    target TEXT,                 -- for impl/method: the type or trait being implemented on
    trait_name TEXT,             -- for impl: the trait name if `impl Trait for Type`
    visibility TEXT,             -- pub, pub(crate), private, etc.
    signature TEXT,              -- one-line signature (for functions/methods)
    doc TEXT,                    -- doc comment attached to the item
    attributes TEXT,             -- derive/attribute lines attached to the item
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    source TEXT NOT NULL,        -- full source text of the item
    parent_id INTEGER REFERENCES items(id) ON DELETE CASCADE  -- e.g. method's parent impl block
);

CREATE INDEX IF NOT EXISTS idx_items_name ON items(name);
CREATE INDEX IF NOT EXISTS idx_items_kind ON items(kind);
CREATE INDEX IF NOT EXISTS idx_items_target ON items(target);
CREATE INDEX IF NOT EXISTS idx_items_file ON items(file_id);

CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    callee_name TEXT NOT NULL,   -- simple function/method name as written
    receiver TEXT,               -- e.g. "self", a var name, or a Type/module path
    is_method_call INTEGER NOT NULL,  -- 1 for `x.foo()`, 0 for `foo()` / `Type::foo()`
    line INTEGER,
    callee_id INTEGER REFERENCES items(id) ON DELETE SET NULL  -- resolved target, if found
);

CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller_id);
CREATE INDEX IF NOT EXISTS idx_calls_callee ON calls(callee_id);
CREATE INDEX IF NOT EXISTS idx_calls_callee_name ON calls(callee_name);

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
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- file management -------------------------------------------------

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
        file_id = row["id"]
        if row["hash"] != file_hash:
            # file changed: clear old items, refresh metadata
            cur.execute("DELETE FROM items WHERE file_id = ?", (file_id,))
            cur.execute(
                "UPDATE files SET mtime = ?, hash = ? WHERE id = ?",
                (mtime, file_hash, file_id),
            )
            self.conn.commit()
        return file_id

    def file_unchanged(self, path: str, file_hash: str) -> bool:
        row = self.conn.execute(
            "SELECT hash FROM files WHERE path = ?", (path,)
        ).fetchone()
        return row is not None and row["hash"] == file_hash

    # -- item management --------------------------------------------------

    def insert_item(
        self,
        file_id: int,
        kind: str,
        name: str,
        start_line: int,
        end_line: int,
        source: str,
        target: Optional[str] = None,
        trait_name: Optional[str] = None,
        visibility: Optional[str] = None,
        signature: Optional[str] = None,
        doc: Optional[str] = None,
        attributes: Optional[str] = None,
        parent_id: Optional[int] = None,
    ) -> int:
        cur = self.conn.cursor()
        cur.execute(
            """INSERT INTO items
               (file_id, kind, name, target, trait_name, visibility, signature,
                doc, attributes, start_line, end_line, source, parent_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                file_id, kind, name, target, trait_name, visibility, signature,
                doc, attributes, start_line, end_line, source, parent_id,
            ),
        )
        return cur.lastrowid

    def commit(self):
        self.conn.commit()

    # -- call graph -----------------------------------------------------

    def clear_calls_for_file(self, file_id: int):
        self.conn.execute(
            "DELETE FROM calls WHERE caller_id IN (SELECT id FROM items WHERE file_id = ?)",
            (file_id,),
        )

    def insert_call(
        self, caller_id: int, callee_name: str, line: int,
        receiver: Optional[str] = None, is_method_call: bool = False,
    ) -> int:
        cur = self.conn.cursor()
        cur.execute(
            """INSERT INTO calls (caller_id, callee_name, receiver, is_method_call, line)
               VALUES (?, ?, ?, ?, ?)""",
            (caller_id, callee_name, receiver, int(is_method_call), line),
        )
        return cur.lastrowid

    def resolve_calls(self):
        """Best-effort static resolution of call edges to concrete items.
        Heuristics (no full type inference, since Rust dispatch can be
        dynamic/generic):
          - method calls (`x.foo()`): prefer a method on the same `target`
            type as the caller's impl when receiver == 'self'; otherwise
            match by method name (picks first match if ambiguous).
          - plain/scoped calls (`foo()` / `Type::foo()`): prefer a function
            or associated method matching `receiver` as the target type,
            else fall back to any function with that name.
        Unresolved calls (external crate, trait object dispatch, etc.) are
        left with callee_id = NULL.
        """
        cur = self.conn.cursor()
        calls = cur.execute(
            """SELECT calls.id, calls.callee_name, calls.receiver, calls.is_method_call,
                      items.target AS caller_target
               FROM calls JOIN items ON calls.caller_id = items.id
               WHERE calls.callee_id IS NULL"""
        ).fetchall()

        for call in calls:
            callee_id = None
            if call["is_method_call"]:
                if call["receiver"] == "self" and call["caller_target"]:
                    row = cur.execute(
                        """SELECT id FROM items
                           WHERE kind IN ('method') AND name = ? AND target = ?
                           LIMIT 1""",
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
                        """SELECT id FROM items
                           WHERE kind = 'method' AND name = ? AND target = ? LIMIT 1""",
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
        self.conn.commit()

    def get_calls_from(self, item_id: int):
        return self.conn.execute(
            """SELECT calls.*, callee.name AS callee_resolved_name, callee.kind AS callee_kind,
                      callee.target AS callee_target
               FROM calls LEFT JOIN items AS callee ON calls.callee_id = callee.id
               WHERE calls.caller_id = ?""",
            (item_id,),
        ).fetchall()

    def get_calls_to(self, item_id: int):
        return self.conn.execute(
            """SELECT calls.*, caller.name AS caller_name, caller.kind AS caller_kind,
                      caller.target AS caller_target
               FROM calls JOIN items AS caller ON calls.caller_id = caller.id
               WHERE calls.callee_id = ?""",
            (item_id,),
        ).fetchall()

    def find_callable(self, name: str, target: Optional[str] = None):
        """Find function/method items matching a name (and optionally a target type)."""
        q = "SELECT * FROM items WHERE kind IN ('function','method') AND name = ?"
        params = [name]
        if target:
            q += " AND (target = ? OR target IS NULL)"
            params.append(target)
        return self.conn.execute(q, params).fetchall()

    def all_call_edges(self):
        """All resolved edges as (caller_id, caller_name, caller_target, callee_id,
        callee_name, callee_target, is_method_call)."""
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

    def call_graph_stats(self):
        total = self.conn.execute("SELECT COUNT(*) AS n FROM calls").fetchone()["n"]
        resolved = self.conn.execute(
            "SELECT COUNT(*) AS n FROM calls WHERE callee_id IS NOT NULL"
        ).fetchone()["n"]
        return total, resolved

    # -- queries ------------------------------------------------------------

    def list_items(
        self,
        kind: Optional[str] = None,
        name: Optional[str] = None,
        target: Optional[str] = None,
        file_like: Optional[str] = None,
        limit: int = 200,
    ):
        q = """SELECT items.*, files.path AS file_path
               FROM items JOIN files ON items.file_id = files.id
               WHERE 1=1"""
        params = []
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

    def get_item(self, item_id: int):
        return self.conn.execute(
            """SELECT items.*, files.path AS file_path
               FROM items JOIN files ON items.file_id = files.id
               WHERE items.id = ?""",
            (item_id,),
        ).fetchone()

    def get_methods_of(self, target: str):
        return self.conn.execute(
            """SELECT items.*, files.path AS file_path
               FROM items JOIN files ON items.file_id = files.id
               WHERE items.kind = 'method' AND items.target LIKE ?
               ORDER BY files.path, items.start_line""",
            (f"%{target}%",),
        ).fetchall()

    def search(self, query: str, kind: Optional[str] = None, limit: int = 100):
        q = """SELECT items.*, files.path AS file_path
               FROM items_fts
               JOIN items ON items.id = items_fts.rowid
               JOIN files ON items.file_id = files.id
               WHERE items_fts MATCH ?"""
        params = [query]
        if kind:
            q += " AND items.kind = ?"
            params.append(kind)
        q += " ORDER BY rank LIMIT ?"
        params.append(limit)
        return self.conn.execute(q, params).fetchall()

    def stats(self):
        rows = self.conn.execute(
            "SELECT kind, COUNT(*) as n FROM items GROUP BY kind ORDER BY n DESC"
        ).fetchall()
        n_files = self.conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
        return n_files, rows
