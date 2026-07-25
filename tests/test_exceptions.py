"""Tests for the exceptions module."""

import pytest

from rust_analyzer.exceptions import (
    AnalysisError,
    DatabaseError,
    GraphError,
    ParseError,
    QueryError,
    RenderError,
)


class TestExceptions:
    def test_analysis_error(self):
        with pytest.raises(AnalysisError):
            raise AnalysisError("test")

    def test_parse_error(self):
        e = ParseError("foo.rs", "syntax error")
        assert e.file_path == "foo.rs"
        assert e.reason == "syntax error"
        assert "foo.rs" in str(e)
        assert isinstance(e, AnalysisError)

    def test_database_error(self):
        with pytest.raises(DatabaseError):
            raise DatabaseError("db failed")
        assert issubclass(DatabaseError, AnalysisError)

    def test_query_error(self):
        e = QueryError("SELECT *", "bad query")
        assert e.query == "SELECT *"
        assert isinstance(e, DatabaseError)

    def test_graph_error(self):
        with pytest.raises(GraphError):
            raise GraphError("graph failed")
        assert issubclass(GraphError, AnalysisError)

    def test_render_error(self):
        e = RenderError("dot not found")
        assert e.reason == "dot not found"
        assert isinstance(e, GraphError)
