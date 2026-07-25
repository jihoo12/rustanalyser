"""Tests for the web frontend module."""

import os

import pytest
from fastapi.testclient import TestClient

from rust_analyzer.cli import main
from rust_analyzer.web import create_app


SAMPLE_RS = b"""\
use std::collections::HashMap;

pub fn add(a: i32, b: i32) -> i32 {
    a + b
}

pub struct Config {
    pub name: String,
}

impl Config {
    pub fn new(name: &str) -> Self {
        Self { name: name.to_string() }
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

pub fn complex(x: i32) -> String {
    if x > 0 {
        if x > 100 {
            "large".to_string()
        } else {
            "small".to_string()
        }
    } else {
        "negative".to_string()
    }
}
"""


@pytest.fixture
def sample_project(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_bytes(SAMPLE_RS)
    return tmp_path


@pytest.fixture
def db_file(tmp_path, sample_project):
    db = str(tmp_path / "test.db")
    main(["scan", "--db", db, str(sample_project)])
    return db


@pytest.fixture
def client(db_file):
    app = create_app(db_file)
    return TestClient(app)


class TestDashboardPage:
    def test_dashboard_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Dashboard" in resp.text
        assert "card-value" in resp.text

    def test_dashboard_has_stats(self, client):
        resp = client.get("/")
        assert "Files" in resp.text
        assert "Total Items" in resp.text


class TestItemsPage:
    def test_items_200(self, client):
        resp = client.get("/items")
        assert resp.status_code == 200
        assert "All Items" in resp.text

    def test_items_filter_by_kind(self, client):
        resp = client.get("/items?kind=function")
        assert resp.status_code == 200
        assert "function" in resp.text

    def test_items_filter_by_name(self, client):
        resp = client.get("/items?name=add")
        assert resp.status_code == 200

    def test_items_pagination(self, client):
        resp = client.get("/items?page=1")
        assert resp.status_code == 200


class TestComplexityPage:
    def test_complexity_200(self, client):
        resp = client.get("/complexity")
        assert resp.status_code == 200
        assert "Complexity Report" in resp.text

    def test_complexity_custom_min(self, client):
        resp = client.get("/complexity?min=1")
        assert resp.status_code == 200


class TestGraphPage:
    def test_graph_200(self, client):
        resp = client.get("/graph")
        assert resp.status_code == 200
        assert "Call Graph" in resp.text

    def test_graph_with_root(self, client):
        resp = client.get("/graph?root=add&depth=3&direction=callees")
        assert resp.status_code == 200


class TestApiSurfacePage:
    def test_api_surface_200(self, client):
        resp = client.get("/api-surface")
        assert resp.status_code == 200
        assert "API Surface" in resp.text
        assert "Public API" in resp.text


class TestDepsPage:
    def test_deps_200(self, client):
        resp = client.get("/deps")
        assert resp.status_code == 200
        assert "Dependencies" in resp.text


class TestSearchPage:
    def test_search_empty(self, client):
        resp = client.get("/search")
        assert resp.status_code == 200
        assert "Full-Text Search" in resp.text

    def test_search_with_query(self, client):
        resp = client.get("/search?q=Config")
        assert resp.status_code == 200

    def test_search_with_kind(self, client):
        resp = client.get("/search?q=add&kind=function")
        assert resp.status_code == 200


class TestJSONAPI:
    def test_api_item(self, client):
        resp = client.get("/api/item/1")
        assert resp.status_code == 200
        data = resp.json()
        assert "name" in data
        assert "source" in data

    def test_api_item_not_found(self, client):
        resp = client.get("/api/item/99999")
        assert resp.status_code == 200
        data = resp.json()
        assert data["error"] == "not found"

    def test_api_stats(self, client):
        resp = client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "files" in data
        assert "items_by_kind" in data

    def test_api_graph_data(self, client):
        resp = client.get("/api/graph-data")
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert "edges" in data


class TestStaticFiles:
    def test_css_loads(self, client):
        resp = client.get("/static/style.css")
        assert resp.status_code == 200
        assert ":root" in resp.text
