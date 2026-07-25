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
