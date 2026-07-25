# rust-analyzer-db

A professional Python tool that uses **tree-sitter** to parse Rust source code,
extracts code entities, computes static metrics, and stores everything in a
**SQLite** database for query, search, call-graph analysis, and reporting.

## Features

- **Full extraction**: structs, enums, traits, impls, functions/methods, consts,
  statics, mods, macros, type aliases, unions, `use` declarations, `extern crate`
- **Static metrics**: cyclomatic complexity, cognitive complexity, nesting depth,
  branch count, lines of code per function
- **Generic parameters & lifetimes**: extracted from structs, functions, impls
- **Call graph**: whole-project or focused subgraph with Graphviz DOT/SVG/PNG/HTML
  rendering and interactive vis-network visualization
- **Public API surface**: identify all public items in a project
- **Dependency tracking**: `use` declarations and `extern crate` declarations
- **Complexity report**: find the most complex functions in your codebase
- **Full-text search**: SQLite FTS5 over name, signature, doc, and source
- **JSON output**: all commands support `--json` for machine-readable output
- **Incremental scanning**: unchanged files skipped by content hash
- **Structured logging**: configurable verbosity and format
- **Web frontend**: master-panel dashboard with interactive call graph, complexity
  report, API surface, dependency analysis, and search

## Install

```bash
pip install -e ".[dev]"
```

Or with just dependencies:

```bash
pip install tree-sitter tree-sitter-rust fastapi uvicorn jinja2 python-multipart
```

Python 3.10+ required.

## Usage

### 1. Scan a project

```bash
rust-analyzer-db scan /path/to/rust/project --db rust_code.db
```

- Recursively finds all `*.rs` files (skips `target/`, `.git/`, `node_modules/`)
- Works on a single file: `rust-analyzer-db scan src/lib.rs`
- Re-running is **incremental** (unchanged files skipped)
- Use `--force` to re-parse everything, `-q` to suppress progress

### 2. List items

```bash
rust-analyzer-db list --kind struct --db rust_code.db
rust-analyzer-db list --kind function --name parse --db rust_code.db
rust-analyzer-db list --json --db rust_code.db
```

### 3. Show full source

```bash
rust-analyzer-db show new --db rust_code.db --kind method
```

### 4. List methods on a type

```bash
rust-analyzer-db methods Point --db rust_code.db --full
```

### 5. Full-text search

```bash
rust-analyzer-db search "distance" --db rust_code.db
rust-analyzer-db search "parse OR tokenize" --db rust_code.db --full
```

### 6. Project statistics

```bash
rust-analyzer-db stats --db rust_code.db
rust-analyzer-db stats --json --db rust_code.db
```

### 7. Complexity report

```bash
rust-analyzer-db complexity --db rust_code.db
rust-analyzer-db complexity --min 10 --db rust_code.db
```

Shows cyclomatic complexity, cognitive complexity, nesting depth, and LOC
for every function/method above the threshold.

### 8. Public API surface

```bash
rust-analyzer-db api --db rust_code.db
rust-analyzer-db api --json --db rust_code.db
```

Lists all `pub` items grouped by kind.

### 9. Dependency analysis

```bash
rust-analyzer-db deps --db rust_code.db
rust-analyzer-db deps --full --db rust_code.db  # show all use paths
```

### 10. Call graph

```bash
# Whole project
rust-analyzer-db graph --db rust_code.db -o callgraph.svg

# Focused on one function
rust-analyzer-db graph --db rust_code.db --root run_demo --depth 3 -o run_demo.svg

# JSON output
rust-analyzer-db graph --db rust_code.db --json
```

Flags: `--direction callees|callers|both`, `--no-unresolved`,
`--format svg|png|pdf|dot|html`.

### 11. Web frontend

```bash
# Start the web UI (default: http://localhost:8000)
rust-analyzer-db serve --db rust_code.db

# Custom host/port
rust-analyzer-db serve --db rust_code.db --host 0.0.0.0 --port 8080
```

The web frontend provides a master-panel style dashboard with:

- **Dashboard** — project stats, top complex functions, item kind distribution,
  complexity histogram, largest files
- **Items** — filterable/sortable/paginated browser for all extracted code
  entities, with source view modals
- **Complexity** — ranked complexity report with adjustable threshold and
  progress-bar indicators
- **Call graph** — interactive vis-network visualization with root/depth/direction
  controls
- **API surface** — public items grouped by kind
- **Dependencies** — extern crates and `use` declaration tree
- **Search** — full-text search across names, signatures, docs, and source

All pages also expose JSON endpoints at `/api/stats`, `/api/item/{id}`,
and `/api/graph-data`.

## What gets captured per item

- `kind`, `name`, `visibility`, `target`, `trait_name`
- `signature` (functions/methods, without body)
- `doc` (doc comments), `attributes` (`#[derive(...)]` etc.)
- `start_line` / `end_line`, `file_path`
- `source` — exact original source text
- `is_pub`, `is_const_fn`, `is_async`, `is_unsafe`
- `cyclomatic_complexity`, `cognitive_complexity`, `nesting_depth`,
  `num_branches`, `num_function_calls`, `lines_of_code`
- Generic parameters and lifetime parameters
- Methods linked to parent impl/trait via `parent_id`

## Database schema

Two main tables (`files`, `items`) plus `calls`, `use_declarations`,
`extern_crates`, `generic_params`, `lifetime_params`, and `items_fts`
(FTS5 index). See `rust_analyzer/db.py` for the full schema.

Query directly:

```bash
sqlite3 rust_code.db "SELECT kind, name, cyclomatic_complexity FROM items WHERE cyclomatic_complexity > 5;"
```

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Type checking
mypy rust_analyzer/

# Linting
ruff check rust_analyzer/
```

## Architecture

```
rust_analyzer/
  __init__.py       - Package version
  cli.py            - CLI commands and argument parsing
  db.py             - SQLite storage layer (schema + queries)
  extractor.py      - tree-sitter extraction + metrics
  graph.py          - Call graph construction + rendering
  web.py            - FastAPI web frontend + JSON API
  exceptions.py     - Exception hierarchy
  logging.py        - Structured logging configuration
  static/
    style.css       - Dark master-panel theme
  templates/
    base.html       - Shared layout with sidebar navigation
    dashboard.html  - Stats cards + charts
    items.html      - Filterable item browser
    complexity.html - Complexity report
    graph.html      - Interactive vis-network call graph
    api_surface.html - Public API grouped by kind
    deps.html       - Dependencies tree
    search.html     - Full-text search
```
