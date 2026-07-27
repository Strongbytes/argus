"""Span exporters: the sinks that decide where captured traces end up.

Argus ships two, and they share one lifecycle: buffer spans in memory as they
end, then do the real work in an ``emit(failed=...)`` call Argus drives on exit.
:class:`~argus.exporters.file.FileSpanExporter` writes each trace to disk twice
-- canonical OTLP/JSON and a human-readable rendering;
:class:`~argus.exporters.otlp.OTLPSpanExporter` POSTs the buffered run to a
remote backend over OTLP/HTTP.

What differs is how each one handles a *second* ``emit``, and the destination is
why. A flush is not terminal -- spans produced after one are emitted by the next
(see :meth:`argus.Session.flush`) -- so both sinks have to stay correct when
called again:

* The file sink keeps its buffer and rewrites each trace's files from scratch,
  so what lands on disk is always the complete trace rather than the slice that
  arrived since the last write.
* The remote sink drops its buffer once the backend confirms the batch, so a
  later emit sends only what has not been ingested -- and nothing at all when
  everything already has.

Both are re-exported here (with the
:func:`~argus.exporters.otlp.make_otlp_exporter` convenience factory) so callers
can reach them as ``argus.exporters.<name>`` regardless of the module layout.
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
