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
    # New tools
    get_item_generics,
    get_item_lifetimes,
    list_files,
    get_file_info,
    callers_of,
    callees_of,
    get_most_complex,
    get_largest_files,
    # Analysis tools
    find_unused_imports,
    implementors_of_trait,
    module_structure,
    find_dead_code,
    file_metrics,
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
        assert "SUCCESS" in result
        assert "Scanned 1 file(s)" in result
        assert "NEXT STEPS" in result

    def test_scan_nonexistent(self) -> None:
        result = scan_project("/nonexistent/path")
        assert "ERROR" in result
        assert "Path not found" in result

    def test_scan_single_file(self, tmp_path: str) -> None:
        rs_file = os.path.join(str(tmp_path), "lib.rs")
        with open(rs_file, "wb") as f:
            f.write(SAMPLE_RS)

        db_path = os.path.join(str(tmp_path), "test.db")
        mcp_mod._db_path = db_path
        result = scan_project(rs_file)
        assert "SUCCESS" in result
        assert "Scanned 1 file(s)" in result

    def test_scan_empty_directory(self, tmp_path: str) -> None:
        db_path = os.path.join(str(tmp_path), "test.db")
        mcp_mod._db_path = db_path
        result = scan_project(str(tmp_path))
        assert "ERROR" in result
        assert "No .rs files found" in result

    def test_scan_creates_gitignore(self, tmp_path: str) -> None:
        src_dir = str(tmp_path)
        rs_file = os.path.join(src_dir, "lib.rs")
        with open(rs_file, "wb") as f:
            f.write(SAMPLE_RS)

        db_path = os.path.join(src_dir, "test.db")
        mcp_mod._db_path = db_path
        result = scan_project(src_dir)
        
        # Check that .gitignore was created
        gitignore_path = os.path.join(src_dir, ".gitignore")
        assert os.path.exists(gitignore_path)
        with open(gitignore_path, "r") as f:
            content = f.read()
        assert "test.db" in content

    def test_scan_creates_agents_md(self, tmp_path: str) -> None:
        src_dir = str(tmp_path)
        rs_file = os.path.join(src_dir, "lib.rs")
        with open(rs_file, "wb") as f:
            f.write(SAMPLE_RS)

        db_path = os.path.join(src_dir, "test.db")
        mcp_mod._db_path = db_path
        result = scan_project(src_dir)
        
        # Check that AGENTS.md was created
        agents_path = os.path.join(src_dir, "AGENTS.md")
        assert os.path.exists(agents_path)
        with open(agents_path, "r") as f:
            content = f.read()
        assert "rust-analyzer-db" in content
        assert "scan_project" in content


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

    def test_list_by_pub(self, set_db: str) -> None:
        result = list_items(is_pub=True)
        data = json.loads(result)
        assert all(item["is_pub"] == 1 for item in data)

    def test_list_by_complexity(self, set_db: str) -> None:
        result = list_items(min_complexity=1)
        data = json.loads(result)
        assert all(item["cyclomatic_complexity"] >= 1 for item in data)


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

    def test_search_with_source(self, set_db: str) -> None:
        result = search_code("add", include_source=True)
        data = json.loads(result)
        assert isinstance(data, list)
        if data:
            assert "source" in data[0]


class TestGetStats:
    def test_stats(self, set_db: str) -> None:
        result = get_stats()
        data = json.loads(result)
        assert "files" in data
        assert "total_items" in data
        assert "items_by_kind" in data
        assert "status" in data
        assert data["files"] > 0
        assert data["total_items"] > 0
        assert data["status"] == "OK"

    def test_stats_keys(self, set_db: str) -> None:
        data = json.loads(get_stats())
        assert "total_call_edges" in data
        assert "resolved_call_edges" in data
        assert "message" in data


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

    def test_with_depth(self, set_db: str) -> None:
        result = call_graph_info("add", depth=2)
        data = json.loads(result)
        assert isinstance(data, list)
        if data:
            assert "depth" in data[0]


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


# New tool tests


class TestGetItemGenerics:
    def test_get_generics(self, set_db: str) -> None:
        items = json.loads(list_items(kind="function", limit=1))
        item_id = items[0]["id"]
        result = get_item_generics(item_id)
        data = json.loads(result)
        assert "item_id" in data
        assert "generic_params" in data
        assert "count" in data

    def test_nonexistent_item(self, set_db: str) -> None:
        result = get_item_generics(999999)
        assert "No item found" in result


class TestGetItemLifetimes:
    def test_get_lifetimes(self, set_db: str) -> None:
        items = json.loads(list_items(kind="function", limit=1))
        item_id = items[0]["id"]
        result = get_item_lifetimes(item_id)
        data = json.loads(result)
        assert "item_id" in data
        assert "lifetime_params" in data
        assert "count" in data

    def test_nonexistent_item(self, set_db: str) -> None:
        result = get_item_lifetimes(999999)
        assert "No item found" in result


class TestListFiles:
    def test_list_files(self, set_db: str) -> None:
        result = list_files()
        data = json.loads(result)
        assert "files" in data
        assert "count" in data
        assert data["count"] > 0

    def test_list_files_with_limit(self, set_db: str) -> None:
        result = list_files(limit=1)
        data = json.loads(result)
        assert data["count"] <= 1


class TestGetFileInfo:
    def test_get_file_info(self, set_db: str) -> None:
        result = get_file_info("lib.rs")
        data = json.loads(result)
        assert "path" in data
        assert "item_count" in data

    def test_nonexistent_file(self, set_db: str) -> None:
        result = get_file_info("nonexistent.rs")
        assert "No file found" in result


class TestCallersOf:
    def test_callers(self, set_db: str) -> None:
        result = callers_of("add")
        data = json.loads(result)
        assert isinstance(data, list)
        if data:
            assert "item" in data[0]
            assert "callers" in data[0]
            assert "caller_count" in data[0]

    def test_no_callers(self, set_db: str) -> None:
        result = callers_of("zzzznonexistent")
        assert "No function/method found" in result


class TestCalleesOf:
    def test_callees(self, set_db: str) -> None:
        result = callees_of("add")
        data = json.loads(result)
        assert isinstance(data, list)
        if data:
            assert "item" in data[0]
            assert "callees" in data[0]
            assert "callee_count" in data[0]

    def test_no_callees(self, set_db: str) -> None:
        result = callees_of("zzzznonexistent")
        assert "No function/method found" in result


class TestGetMostComplex:
    def test_most_complex(self, set_db: str) -> None:
        result = get_most_complex()
        data = json.loads(result)
        assert "functions" in data
        assert "count" in data

    def test_with_limit(self, set_db: str) -> None:
        result = get_most_complex(limit=5)
        data = json.loads(result)
        assert data["count"] <= 5


class TestGetLargestFiles:
    def test_largest_files(self, set_db: str) -> None:
        result = get_largest_files()
        data = json.loads(result)
        assert "files" in data
        assert "count" in data
        assert data["count"] > 0

    def test_with_limit(self, set_db: str) -> None:
        result = get_largest_files(limit=1)
        data = json.loads(result)
        assert data["count"] <= 1


# Analysis tool tests


class TestFindUnusedImports:
    def test_unused_imports(self, set_db: str) -> None:
        result = find_unused_imports()
        data = json.loads(result)
        assert "unused_imports" in data
        assert "count" in data

    def test_unused_imports_for_file(self, set_db: str) -> None:
        result = find_unused_imports(file_path="lib.rs")
        data = json.loads(result)
        assert "unused_imports" in data

    def test_nonexistent_file(self, set_db: str) -> None:
        result = find_unused_imports(file_path="nonexistent.rs")
        assert "No file found" in result


class TestImplementorsOfTrait:
    def test_implementors(self, set_db: str) -> None:
        result = implementors_of_trait("Greet")
        data = json.loads(result)
        assert "trait" in data
        assert "implementations" in data
        assert "count" in data

    def test_no_implementors(self, set_db: str) -> None:
        result = implementors_of_trait("zzzzNonexistent")
        data = json.loads(result)
        assert data["count"] == 0
        assert data["implementations"] == []


class TestModuleStructure:
    def test_modules(self, set_db: str) -> None:
        result = module_structure()
        data = json.loads(result)
        assert "modules" in data
        assert "by_file" in data
        assert "count" in data


class TestFindDeadCode:
    def test_dead_code(self, set_db: str) -> None:
        result = find_dead_code()
        data = json.loads(result)
        assert "dead_code" in data
        assert "count" in data

    def test_dead_code_with_complexity(self, set_db: str) -> None:
        result = find_dead_code(min_complexity=10)
        data = json.loads(result)
        assert "dead_code" in data


class TestFileMetrics:
    def test_all_files(self, set_db: str) -> None:
        result = file_metrics()
        data = json.loads(result)
        assert "files" in data
        assert "count" in data
        assert data["count"] > 0
        # Check that metrics are present
        if data["files"]:
            file_data = data["files"][0]
            assert "item_count" in file_data
            assert "avg_complexity" in file_data

    def test_specific_file(self, set_db: str) -> None:
        result = file_metrics(file_path="lib.rs")
        data = json.loads(result)
        assert "files" in data
        assert data["count"] >= 1

    def test_nonexistent_file(self, set_db: str) -> None:
        result = file_metrics(file_path="nonexistent.rs")
        assert "No files found" in result


class TestMCPServerObject:
    def test_server_has_tools(self) -> None:
        tools = list(mcp._tool_manager._tools.keys())
        expected = [
            "scan_project", "list_items", "get_item", "search_code",
            "get_stats", "complexity_report", "api_surface", "dependencies",
            "call_graph_info", "methods_of",
            # New tools
            "get_item_generics", "get_item_lifetimes", "list_files",
            "get_file_info", "callers_of", "callees_of",
            "get_most_complex", "get_largest_files",
            # Analysis tools
            "find_unused_imports", "implementors_of_trait", "module_structure",
            "find_dead_code", "file_metrics",
        ]
        for name in expected:
            assert name in tools, f"Missing tool: {name}"

    def test_server_has_resources(self) -> None:
        resources = list(mcp._resource_manager._resources.keys())
        assert "rust-analyzer://schema" in resources
        assert "rust-analyzer://stats" in resources
        assert "rust-analyzer://files" in resources

    def test_server_has_prompts(self) -> None:
        prompts = list(mcp._prompt_manager._prompts.keys())
        assert "review_function" in prompts
        assert "analyze_project" in prompts
        assert "find_dead_code_prompt" in prompts
        assert "refactor_suggestion" in prompts
