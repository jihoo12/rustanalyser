"""Logging configuration for rust-analyzer-db."""

from __future__ import annotations

import logging
import sys

_configured = False


class _SafeStreamHandler(logging.StreamHandler):
    """StreamHandler that silently drops writes if the stream is closed."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self.stream.closed:
                return
            super().emit(record)
        except ValueError:
            pass


def setup_logging(*, verbose: bool = False, json_output: bool = False) -> None:
    """Configure the package-wide logger.

    Args:
        verbose: Enable DEBUG level output.
        json_output: Format logs as single-line key=value pairs.
    """
    global _configured  # noqa: PLW0603
    if _configured:
        return
    _configured = True

    level = logging.DEBUG if verbose else logging.INFO

    if json_output:
        fmt = '{"level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}'
    else:
        fmt = "%(levelname)s: %(message)s"

    handler = _SafeStreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(fmt))

    root = logging.getLogger("rust_analyzer")
    root.setLevel(level)
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Get a child logger under the ``rust_analyzer`` namespace."""
    return logging.getLogger(f"rust_analyzer.{name}")
