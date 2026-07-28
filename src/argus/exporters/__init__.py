"""Span exporters: the sinks that decide where captured traces end up.

Argus ships two, and they share one lifecycle: buffer spans in memory as they
end, then do the real work in an ``emit(failed=...)`` call Argus drives on flush.
:class:`~argus.exporters.file.FileSpanExporter` writes each trace to disk twice
-- canonical OTLP/JSON and a human-readable rendering;
:class:`~argus.exporters.otlp.BufferedOTLPExporter` POSTs the buffered run to a
remote backend over OTLP/HTTP, configured by
:class:`~argus.exporters.otlp.OtlpConfig`.

:class:`~argus.exporters.base.BufferedSpanExporter` is that lifecycle expressed
as a :class:`typing.Protocol`, and the typed extension point for a sink of your
own: any exporter satisfying it is driven through ``emit`` (and handed the run's
outcome) instead of ``force_flush``.

:func:`~argus.exporters.file.trace_filename` is the on-disk naming scheme in one
function, for code that has to reproduce or parse what the file sink writes;
:data:`~argus.exporters.file.TraceFormat` is the ``"otlp"``/``"readable"``
literal it takes to say which of a trace's two files is meant.

See ``docs/design-notes.md`` ("Buffer now, emit once", "Repeat emits: rewrite or
clear", "Exporters Argus does not own").
"""

from .base import BufferedSpanExporter
from .file import FileSpanExporter, TraceFormat, trace_filename
from .otlp import BufferedOTLPExporter, OtlpConfig

__all__ = [
    "BufferedOTLPExporter",
    "BufferedSpanExporter",
    "FileSpanExporter",
    "OtlpConfig",
    "TraceFormat",
    "trace_filename",
]
