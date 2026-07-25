# rust_analyzer

A Python tool that uses **tree-sitter** to parse Rust source code, extracts
structs, enums, traits, impls, functions/methods, consts, statics, mods, and
macros, and stores everything in a **SQLite** database so you can query it
back later and pull out exact source code.

## Install

```bash
pip install tree-sitter tree-sitter-rust
```

(Python 3.9+, no other dependencies — the DB is plain SQLite, built into Python.)

## Usage

Run as a module from the project root (the folder containing `rust_analyzer/`).

### 1. Scan a project

```bash
python3 -m rust_analyzer.cli scan /path/to/rust/project --db rust_code.db
```

- Recursively finds all `*.rs` files (skips `target/` and `.git/`).
- Also works on a single file: `scan src/lib.rs`.
- Re-running `scan` is **incremental**: unchanged files (by content hash) are
  skipped. Use `--force` to re-parse everything.

### 2. List items

```bash
python3 -m rust_analyzer.cli list --db rust_code.db --kind struct
python3 -m rust_analyzer.cli list --db rust_code.db --kind function --name parse
python3 -m rust_analyzer.cli list --db rust_code.db --target MyStruct
```

Kinds: `struct`, `enum`, `trait`, `impl`, `function`, `function_sig`, `method`,
`method_sig`, `const`, `static`, `mod`, `mod_decl`, `macro`, `type_alias`,
`union`, `assoc_const`, `assoc_type`.

### 3. Show full source code of an item

```bash
python3 -m rust_analyzer.cli show new --db rust_code.db --kind method
```

Prints doc comments, attributes, and the exact source text of every matching
item (name is a substring match).

### 4. List methods on a type or trait

```bash
python3 -m rust_analyzer.cli methods Point --db rust_code.db --full
```

### 5. Full-text search

Uses SQLite FTS5 over name/signature/doc/source:

```bash
python3 -m rust_analyzer.cli search "distance" --db rust_code.db
python3 -m rust_analyzer.cli search "parse OR tokenize" --db rust_code.db --full
```

### 6. Stats

```bash
python3 -m rust_analyzer.cli stats --db rust_code.db
```

## What gets captured per item

- `kind`, `name`, `visibility` (`pub`, `pub(crate)`, ...)
- `target` / `trait_name` for impls and methods (e.g. `impl Display for Point`)
- `signature` (functions/methods, without the body)
- `doc` (`///` / `//!` / `/** */` comments immediately preceding the item)
- `attributes` (`#[derive(...)]` etc. immediately preceding the item)
- `start_line` / `end_line` and the owning file path
- `source` — the exact original source text of the item
- Methods/assoc items are linked to their parent `impl`/`trait` via `parent_id`

## Database schema

Two tables: `files` and `items` (plus an `items_fts` FTS5 index kept in sync
via triggers). See `rust_analyzer/db.py` for the full schema — it's plain
SQLite, so you can also query `rust_code.db` directly with any SQLite client:

```bash
sqlite3 rust_code.db "SELECT kind, name, target FROM items WHERE kind='impl';"
```

## Extending

- Add more node kinds in `extractor.py` (`TOP_LEVEL_KINDS` / the
  `_extract_*` functions) — e.g. `union_item` fields, generics, where-clauses.
- Swap SQLite for Postgres/MySQL by reimplementing `db.py`'s `RustCodeDB`
  with the same method signatures.
