"""Tests for the CLI module."""

import os
import subprocess
import sys
import tempfile

import pytest

from rust_analyzer.cli import main, build_parser
from rust_analyzer.db import RustCodeDB


SAMPLE_RS = b"""\
use std::collections::HashMap;

/// A simple function.
pub fn add(a: i32, b: i32) -> i32 {
    a + b
}

pub struct Config {
    pub name: String,
    pub debug: bool,
}

impl Config {
    pub fn new(name: &str) -> Self {
        Self {
            name: name.to_string(),
            debug: false,
        }
    }

    pub fn toggle_debug(&mut self) {
        self.debug = !self.debug;
    }
}

pub trait Greet {
    fn greet(&self) -> String;
}

impl Greet for Config {
    fn greet(&self) -> String {
        format!("Hello from {}", self.name)
    }
}
"""


@pytest.fixture
def sample_project(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_bytes(SAMPLE_RS)
    (src / "main.rs").write_bytes(b'fn main() { println!("hello"); }\n')
    return tmp_path


@pytest.fixture
def db_file(tmp_path):
    return str(tmp_path / "test.db")


def _cli(*args: str) -> int:
    """Run CLI with --db after the subcommand to avoid parser default overwrite."""
    return main(list(args))


class TestScanCommand:
    def test_scan_directory(self, sample_project, db_file):
        rc = _cli("scan", "--db", db_file, str(sample_project))
        assert rc == 0
        db = RustCodeDB(db_file)
        n_files, rows = db.stats()
        assert n_files == 2
        db.close()

    def test_scan_single_file(self, sample_project, db_file):
        lib_rs = sample_project / "src" / "lib.rs"
        rc = _cli("scan", "--db", db_file, str(lib_rs))
        assert rc == 0
        db = RustCodeDB(db_file)
        n_files, _ = db.stats()
        assert n_files == 1
        db.close()

    def test_scan_nonexistent(self, tmp_path, db_file):
        rc = _cli("scan", "--db", db_file, str(tmp_path / "nope"))
        assert rc == 1

    def test_scan_skip_dirs(self, sample_project, db_file):
        target = sample_project / "target" / "debug"
        target.mkdir(parents=True)
        (target / "build.rs").write_bytes(b"fn build() {}\n")
        rc = _cli("scan", "--db", db_file, str(sample_project))
        assert rc == 0
        db = RustCodeDB(db_file)
        n_files, _ = db.stats()
        assert n_files == 2
        db.close()

    def test_scan_incremental(self, sample_project, db_file):
        rc = _cli("scan", "--db", db_file, str(sample_project))
        assert rc == 0
        rc = _cli("scan", "--db", db_file, str(sample_project))
        assert rc == 0

    def test_scan_force(self, sample_project, db_file):
        _cli("scan", "--db", db_file, str(sample_project))
        rc = _cli("scan", "--db", db_file, "--force", str(sample_project))
        assert rc == 0


class TestListCommand:
    def test_list_all(self, sample_project, db_file):
        _cli("scan", "--db", db_file, str(sample_project))
        rc = _cli("list", "--db", db_file)
        assert rc == 0

    def test_list_by_kind(self, sample_project, db_file):
        _cli("scan", "--db", db_file, str(sample_project))
        rc = _cli("list", "--db", db_file, "--kind", "struct")
        assert rc == 0

    def test_list_by_name(self, sample_project, db_file):
        _cli("scan", "--db", db_file, str(sample_project))
        rc = _cli("list", "--db", db_file, "--name", "Config")
        assert rc == 0

    def test_list_json(self, sample_project, db_file):
        _cli("scan", "--db", db_file, str(sample_project))
        rc = _cli("list", "--db", db_file, "--json")
        assert rc == 0


class TestShowCommand:
    def test_show_item(self, sample_project, db_file):
        _cli("scan", "--db", db_file, str(sample_project))
        rc = _cli("show", "--db", db_file, "add")
        assert rc == 0


class TestMethodsCommand:
    def test_methods(self, sample_project, db_file):
        _cli("scan", "--db", db_file, str(sample_project))
        rc = _cli("methods", "--db", db_file, "Config")
        assert rc == 0


class TestSearchCommand:
    def test_search(self, sample_project, db_file):
        _cli("scan", "--db", db_file, str(sample_project))
        rc = _cli("search", "--db", db_file, "Config")
        assert rc == 0

    def test_search_json(self, sample_project, db_file):
        _cli("scan", "--db", db_file, str(sample_project))
        rc = _cli("search", "--db", db_file, "--json", "Config")
        assert rc == 0


class TestStatsCommand:
    def test_stats(self, sample_project, db_file):
        _cli("scan", "--db", db_file, str(sample_project))
        rc = _cli("stats", "--db", db_file)
        assert rc == 0

    def test_stats_json(self, sample_project, db_file):
        _cli("scan", "--db", db_file, str(sample_project))
        rc = _cli("stats", "--db", db_file, "--json")
        assert rc == 0


class TestComplexityCommand:
    def test_complexity(self, sample_project, db_file):
        _cli("scan", "--db", db_file, str(sample_project))
        rc = _cli("complexity", "--db", db_file)
        assert rc == 0


class TestApiCommand:
    def test_api(self, sample_project, db_file):
        _cli("scan", "--db", db_file, str(sample_project))
        rc = _cli("api", "--db", db_file)
        assert rc == 0

    def test_api_json(self, sample_project, db_file):
        _cli("scan", "--db", db_file, str(sample_project))
        rc = _cli("api", "--db", db_file, "--json")
        assert rc == 0


class TestDepsCommand:
    def test_deps(self, sample_project, db_file):
        _cli("scan", "--db", db_file, str(sample_project))
        rc = _cli("deps", "--db", db_file)
        assert rc == 0

    def test_deps_full(self, sample_project, db_file):
        _cli("scan", "--db", db_file, str(sample_project))
        rc = _cli("deps", "--db", db_file, "--full")
        assert rc == 0


class TestGraphCommand:
    def test_graph_whole(self, sample_project, db_file, tmp_path):
        _cli("scan", "--db", db_file, str(sample_project))
        out = str(tmp_path / "graph.dot")
        rc = _cli("graph", "--db", db_file, "--format", "dot", "-o", out)
        assert rc == 0
        assert os.path.exists(out)

    def test_graph_root(self, sample_project, db_file, tmp_path):
        _cli("scan", "--db", db_file, str(sample_project))
        out = str(tmp_path / "graph.dot")
        rc = _cli("graph", "--db", db_file, "--root", "add", "--format", "dot", "-o", out)
        assert rc == 0

    def test_graph_json(self, sample_project, db_file):
        _cli("scan", "--db", db_file, str(sample_project))
        rc = _cli("graph", "--db", db_file, "--json")
        assert rc == 0

    def test_graph_no_root_found(self, sample_project, db_file, tmp_path):
        _cli("scan", "--db", db_file, str(sample_project))
        out = str(tmp_path / "graph.dot")
        rc = _cli("graph", "--db", db_file, "--root", "nonexistent", "-o", out)
        assert rc == 1


class TestBuildParser:
    def test_build_parser(self):
        parser = build_parser()
        assert parser is not None

    def test_parser_help(self):
        parser = build_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--help"])
        assert exc_info.value.code == 0
