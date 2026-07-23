"""Span exporters: the sinks that decide where captured traces end up.

Argus ships two, and they share one lifecycle -- buffer spans in memory, then
emit once on exit. :class:`~argus.exporters.file.FileSpanExporter` writes
readable JSON to disk; :class:`~argus.exporters.otlp.OTLPSpanExporter` POSTs the
buffered run to a remote backend over OTLP/HTTP. Both are re-exported here (with
the :func:`~argus.exporters.otlp.make_otlp_exporter` convenience factory) so
callers can reach them as ``argus.exporters.<name>`` regardless of the module
layout.
"""

from .file import FileSpanExporter
from .otlp import (
    OTLPSpanExporter,
    make_otlp_exporter,
)

__all__ = [
    "FileSpanExporter",
    "OTLPSpanExporter",
    "make_otlp_exporter",
]
