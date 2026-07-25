"""Tests for the graph module."""

import pytest

from rust_analyzer.db import RustCodeDB
from rust_analyzer.graph import CallGraph, build_whole_graph, build_subgraph, to_dot, to_html


@pytest.fixture
def db_with_calls(tmp_path):
    db_path = str(tmp_path / "test.db")
    db = RustCodeDB(db_path)
    fid = db.upsert_file("/test.rs", 0.0, "h")
    fn_main = db.insert_item(
        file_id=fid, kind="function", name="main",
        start_line=1, end_line=5, source="fn main() { foo(); bar(); }",
    )
    fn_foo = db.insert_item(
        file_id=fid, kind="function", name="foo",
        start_line=7, end_line=10, source="fn foo() {}",
    )
    fn_bar = db.insert_item(
        file_id=fid, kind="function", name="bar",
        start_line=12, end_line=15, source="fn bar() {}",
    )
    db.insert_call(fn_main, "foo", 2)
    db.insert_call(fn_main, "bar", 3)
    db.commit()
    db.resolve_calls()
    yield db
    db.close()


class TestCallGraph:
    def test_empty_graph(self):
        g = CallGraph()
        assert len(g.nodes) == 0
        assert len(g.edges) == 0

    def test_add_node(self):
        g = CallGraph()
        g.add_node("n1", "func", "function", 1)
        assert "n1" in g.nodes
        assert g.nodes["n1"]["label"] == "func"

    def test_add_edge(self):
        g = CallGraph()
        g.add_node("n1", "a", "function", 1)
        g.add_node("n2", "b", "function", 2)
        g.add_edge("n1", "n2")
        assert ("n1", "n2") in g.edges

    def test_no_self_edges(self):
        g = CallGraph()
        g.add_node("n1", "a", "function", 1)
        g.add_edge("n1", "n1")
        assert len(g.edges) == 0


class TestBuildWholeGraph:
    def test_builds_graph(self, db_with_calls):
        g = build_whole_graph(db_with_calls, include_unresolved=False)
        assert len(g.nodes) >= 3
        assert len(g.edges) >= 2

    def test_exclude_unresolved(self, db_with_calls):
        g = build_whole_graph(db_with_calls, include_unresolved=False)
        for _, meta in g.nodes.items():
            assert meta["kind"] != "external"


class TestBuildSubgraph:
    def test_builds_subgraph(self, db_with_calls):
        g = build_subgraph(db_with_calls, [1], depth=2, direction="callees")
        assert len(g.nodes) >= 1

    def test_callers_direction(self, db_with_calls):
        g = build_subgraph(db_with_calls, [2], depth=2, direction="callers")
        assert len(g.nodes) >= 1


class TestDotRendering:
    def test_to_dot(self, db_with_calls):
        g = build_whole_graph(db_with_calls)
        dot = to_dot(g, "Test Graph")
        assert "digraph" in dot
        assert "Test Graph" in dot

    def test_to_dot_empty(self):
        g = CallGraph()
        dot = to_dot(g, "Empty")
        assert "digraph" in dot


class TestHtmlRendering:
    def test_to_html(self, db_with_calls):
        g = build_whole_graph(db_with_calls)
        html = to_html(g, "Test Graph")
        assert "vis-network" in html
        assert "Test Graph" in html
        assert "nodesData" in html

    def test_to_html_empty(self):
        g = CallGraph()
        html = to_html(g, "Empty")
        assert "vis-network" in html
