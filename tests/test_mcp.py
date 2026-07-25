"""Tests for the MCP server module."""

import json
import os
import tempfile

import pytest

from rust_analyzer.cli import main
from rust_analyzer.mcp_server import (
    mcp,
    scan_project,
    list_items,
    get_item,
    search_code,
    get_stats,
    complexity_report,
    api_surface,
    dependencies,
    call_graph_info,
    methods_of,
    run_server,
    _db_path,
)
import rust_analyzer.mcp_server as mcp_mod


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


@pytest.fixture()
def db_with_data(tmp_path: str) -> str:
    """Create a scanned test database."""
    src_dir = str(tmp_path)
    rs_file = os.path.join(src_dir, "lib.rs")
    with open(rs_file, "wb") as f:
        f.write(SAMPLE_RS)

    db_path = os.path.join(src_dir, "test.db")
    main(["scan", "--db", db_path, src_dir])
    return db_path


@pytest.fixture()
def set_db(db_with_data: str) -> str:
    """Set the global db path for MCP tools."""
    old = mcp_mod._db_path
    mcp_mod._db_path = db_with_data
    yield db_with_data
    mcp_mod._db_path = old


class TestScanProject:
    def test_scan_directory(self, tmp_path: str) -> None:
        src_dir = str(tmp_path)
        rs_file = os.path.join(src_dir, "lib.rs")
        with open(rs_file, "wb") as f:
            f.write(SAMPLE_RS)

        db_path = os.path.join(src_dir, "test.db")
        mcp_mod._db_path = db_path
        result = scan_project(src_dir)
        assert "Scanned 1 file(s)" in result
        assert "items" in result

    def test_scan_nonexistent(self) -> None:
        result = scan_project("/nonexistent/path")
        assert "Error" in result

    def test_scan_single_file(self, tmp_path: str) -> None:
        rs_file = os.path.join(str(tmp_path), "lib.rs")
        with open(rs_file, "wb") as f:
            f.write(SAMPLE_RS)

        db_path = os.path.join(str(tmp_path), "test.db")
        mcp_mod._db_path = db_path
        result = scan_project(rs_file)
        assert "Scanned 1 file(s)" in result


class TestListItems:
    def test_list_all(self, set_db: str) -> None:
        result = list_items()
        data = json.loads(result)
        assert isinstance(data, list)
        assert len(data) > 0

    def test_list_by_kind(self, set_db: str) -> None:
        result = list_items(kind="function")
        data = json.loads(result)
        assert all(item["kind"] == "function" for item in data)

    def test_list_by_name(self, set_db: str) -> None:
        result = list_items(name="add")
        data = json.loads(result)
        assert any("add" in item["name"] for item in data)

    def test_list_empty(self, set_db: str) -> None:
        result = list_items(kind="nonexistent")
        assert "No matching items" in result


class TestGetItem:
    def test_get_existing(self, set_db: str) -> None:
        items = json.loads(list_items(kind="function", limit=1))
        item_id = items[0]["id"]
        result = get_item(item_id)
        data = json.loads(result)
        assert data["id"] == item_id
        assert "source" in data

    def test_get_nonexistent(self, set_db: str) -> None:
        result = get_item(999999)
        assert "No item found" in result


class TestSearchCode:
    def test_search(self, set_db: str) -> None:
        result = search_code("add")
        data = json.loads(result)
        assert isinstance(data, list)
        assert len(data) > 0

    def test_search_no_matches(self, set_db: str) -> None:
        result = search_code("zzzznonexistent")
        assert "No matches" in result


class TestGetStats:
    def test_stats(self, set_db: str) -> None:
        result = get_stats()
        data = json.loads(result)
        assert "files" in data
        assert "total_items" in data
        assert "items_by_kind" in data
        assert data["files"] > 0
        assert data["total_items"] > 0

    def test_stats_keys(self, set_db: str) -> None:
        data = json.loads(get_stats())
        assert "total_call_edges" in data
        assert "resolved_call_edges" in data


class TestComplexityReport:
    def test_report(self, set_db: str) -> None:
        result = complexity_report(min_complexity=1)
        data = json.loads(result)
        assert isinstance(data, list)

    def test_report_high_threshold(self, set_db: str) -> None:
        result = complexity_report(min_complexity=100)
        assert "No functions exceed" in result


class TestApiSurface:
    def test_api(self, set_db: str) -> None:
        result = api_surface()
        data = json.loads(result)
        assert isinstance(data, dict)
        # Should have pub items
        total = sum(len(v) for v in data.values())
        assert total > 0

    def test_api_has_pub_functions(self, set_db: str) -> None:
        data = json.loads(api_surface())
        assert "function" in data


class TestDependencies:
    def test_deps(self, set_db: str) -> None:
        result = dependencies()
        data = json.loads(result)
        assert "use_groups" in data
        assert "extern_crates" in data
        assert "total_use_declarations" in data
        assert data["total_use_declarations"] > 0


class TestCallGraphInfo:
    def test_project_stats(self, set_db: str) -> None:
        result = call_graph_info()
        data = json.loads(result)
        assert "total_call_edges" in data
        assert "resolved_call_edges" in data

    def test_function_trace(self, set_db: str) -> None:
        result = call_graph_info("add")
        data = json.loads(result)
        assert isinstance(data, list)
        if data:
            assert "item" in data[0]
            assert "calls" in data[0]
            assert "called_by" in data[0]

    def test_nonexistent_function(self, set_db: str) -> None:
        result = call_graph_info("zzzznonexistent")
        assert "No function/method found" in result


class TestMethodsOf:
    def test_methods(self, set_db: str) -> None:
        result = methods_of("Config")
        data = json.loads(result)
        assert isinstance(data, list)
        assert len(data) > 0
        assert all(item["kind"] == "method" for item in data)

    def test_no_methods(self, set_db: str) -> None:
        result = methods_of("zzzznonexistent")
        assert "No methods found" in result


class TestMCPServerObject:
    def test_server_has_tools(self) -> None:
        tools = list(mcp._tool_manager._tools.keys())
        expected = [
            "scan_project", "list_items", "get_item", "search_code",
            "get_stats", "complexity_report", "api_surface", "dependencies",
            "call_graph_info", "methods_of",
        ]
        for name in expected:
            assert name in tools, f"Missing tool: {name}"

    def test_server_has_resources(self) -> None:
        resources = list(mcp._resource_manager._resources.keys())
        assert "rust-analyzer://schema" in resources

    def test_server_has_prompts(self) -> None:
        prompts = list(mcp._prompt_manager._prompts.keys())
        assert "review_function" in prompts
        assert "analyze_project" in prompts
