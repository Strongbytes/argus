"""Span exporters: the sinks that decide where captured traces end up.

Currently Argus ships one, :class:`~argus.exporters.file.FileSpanExporter`,
which writes readable JSON to disk. Re-exported here so callers can reach it as
``argus.exporters.FileSpanExporter`` regardless of the module layout, and so
future exporters (e.g. a remote OTLP sink) have an obvious home.
"""

from .file import FileSpanExporter

__all__ = ["FileSpanExporter"]
