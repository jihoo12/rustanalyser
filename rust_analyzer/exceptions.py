"""Exception hierarchy for rust-analyzer-db."""

from __future__ import annotations


class AnalysisError(Exception):
    """Base exception for all analysis errors."""


class ParseError(AnalysisError):
    """Raised when tree-sitter fails to parse a Rust source file."""

    def __init__(self, file_path: str, reason: str) -> None:
        self.file_path = file_path
        self.reason = reason
        super().__init__(f"Failed to parse {file_path}: {reason}")


class DatabaseError(AnalysisError):
    """Raised on database operation failures."""


class QueryError(DatabaseError):
    """Raised when a database query is malformed or fails."""

    def __init__(self, query: str, reason: str) -> None:
        self.query = query
        self.reason = reason
        super().__init__(f"Query failed: {reason}")


class GraphError(AnalysisError):
    """Raised when graph construction or rendering fails."""


class RenderError(GraphError):
    """Raised when Graphviz rendering fails."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Render failed: {reason}")
