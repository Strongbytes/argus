"""A span exporter that persists OpenTelemetry traces to disk in two shapes.

This is Argus's default sink: the thing that turns the spans an instrumented
run emits into files you can open, diff, and triage later. For every trace it
writes *two* files that share a base name and differ only by a format marker:

* ``<timestamp>_<script>.otlp.json`` -- canonical OTLP/JSON, the exact shape
  the wire protocol uses, so it can be replayed to any OTLP backend.
* ``<timestamp>_<script>.readable.json`` -- a human-readable rendering meant
  for eyeballing: a plain list of spans with embedded JSON payloads unescaped.

The design hinges on one deliberate choice -- **buffer now, write once**.
OpenTelemetry hands spans to :meth:`~FileSpanExporter.export` as they end,
incrementally and out of order, but at that moment we don't yet know how the
run as a whole will turn out. So rather than streaming each span to disk, we
accumulate them in memory, grouped by trace id, and defer the actual write to
:meth:`~FileSpanExporter.emit`. Argus calls that method exactly once, on
process exit, when the run's final outcome is known.

Knowing the outcome up front is what lets the *filename* carry it: a healthy
run lands at ``<timestamp>_<script>.<format>.json`` while a run that died on an
unhandled exception is tagged ``<timestamp>_<script>.error.<format>.json``. The
timestamp is a UTC ``YYYY-MM-DD_HH-MM-SS`` stamp; the date-first layout also
means a directory listing sorts chronologically. Failed runs are therefore
obvious at a glance in a directory listing and never silently discarded. Each
distinct trace gets its own pair of files, and a collision guard keeps two
traces from the same second from clobbering one another.

The OTLP file's *contents* are canonical OTLP/JSON: it is a single
``ExportTraceServiceRequest`` -- the same protobuf message
:class:`~argus.exporters.otlp.OTLPSpanExporter` POSTs to a backend -- rendered
to JSON via :func:`google.protobuf.json_format.MessageToDict`. We reuse
OpenTelemetry's own :func:`~opentelemetry.exporter.otlp.proto.common\
.trace_encoder.encode_spans` rather than hand-rolling the schema, so the file
sink and the remote sink can never drift apart. One conformance detail we
handle explicitly: OTLP/JSON's single documented departure from the proto3 JSON
mapping is that ``traceId``/``spanId`` (and ``parentSpanId``, including the ids
nested under span links) are **hex** strings, not the base64 that
:func:`~google.protobuf.json_format.MessageToDict` emits by default -- so we
re-encode just those fields to hex (see :func:`_hex_encode_ids`). That makes the
file drop-in valid for POSTing straight back as OTLP/JSON, the common replay
path. The rarer path -- rebuilding the protobuf message from this file to send
as OTLP/protobuf -- must therefore hex-decode those id fields rather than lean
on stock proto3 JSON parsing (which would base64-decode them). Every other field
follows the standard camelCase OTLP layout. Encoding relies on the
``opentelemetry-exporter-otlp-proto-common`` package, a core dependency because
this is the default sink -- it is the encoder only, not a network transport.

The readable file trades wire fidelity for legibility. Each span is rendered
with OpenTelemetry's own :meth:`~opentelemetry.sdk.trace.ReadableSpan.to_json`
(snake_case fields, hex ids, ISO timestamps) and then passed through
:func:`~argus.json_utils.expand_embedded_json`, which recursively parses any
attribute value that is itself a JSON string back into structured data -- so a
model's ``output.value`` shows up as a real object instead of an escaped blob.
The file is a plain JSON array of those spans, in generation order.

The remaining methods (:meth:`~FileSpanExporter.force_flush` and
:meth:`~FileSpanExporter.shutdown`) exist only to satisfy the
:class:`~opentelemetry.sdk.trace.export.SpanExporter` contract; because all the
real work is deferred to ``emit`` they are intentionally no-ops.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Sequence

from google.protobuf.json_format import MessageToDict
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from ..json_utils import expand_embedded_json

# Format markers appended (before ``.json``) to a trace's shared base name, so
# each file self-describes its shape: canonical OTLP/JSON vs. the human-readable
# rendering. Kept as a suffix (not a prefix) so the date-first base name still
# sorts a directory listing chronologically.
_OTLP_SUFFIX = ".otlp.json"
_READABLE_SUFFIX = ".readable.json"

# The OTLP/JSON id fields (``bytes`` in protobuf) that must be hex, not the
# base64 the proto3 JSON mapping -- and thus ``MessageToDict`` -- defaults to.
# ``parentSpanId`` and the ids nested inside span links share the same rule.
_ID_JSON_KEYS = frozenset({"traceId", "spanId", "parentSpanId"})


def _hex_encode_ids(node: Any) -> Any:
    """Return ``node`` with every OTLP id field re-encoded from base64 to hex.

    :func:`~google.protobuf.json_format.MessageToDict` renders the ``bytes`` id
    fields as base64 (the proto3 JSON default), but OTLP/JSON mandates hex for
    exactly these fields. We walk the encoded structure and convert them,
    leaving everything else untouched -- so the file is valid to POST back as
    OTLP/JSON. Attribute payloads are safe from accidental rewriting: their
    values live under ``key``/``value`` entries, never these reserved keys.
    """
    if isinstance(node, dict):
        return {
            key: (
                base64.b64decode(value).hex()
                if key in _ID_JSON_KEYS and isinstance(value, str)
                else _hex_encode_ids(value)
            )
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_hex_encode_ids(item) for item in node]
    return node


class FileSpanExporter(SpanExporter):
    """Persist spans to disk, one OTLP/JSON and one readable file per trace."""

    def __init__(self, base_dir: Path, script_name: str) -> None:
        """Prepare an exporter that writes traces under ``base_dir``.

        Args:
            base_dir: Directory traces are written to; created (with parents)
                if it doesn't already exist.
            script_name: Name stamped into each filename, identifying the run
                that produced the trace.

        The two ``_trace_*`` maps are the in-memory buffers keyed by trace id:
        ``_trace_names`` remembers the chosen base name per trace (so both of a
        trace's files keep a stable, matching stem) and ``_trace_spans``
        accumulates the raw :class:`~opentelemetry.sdk.trace.ReadableSpan`
        objects (kept unencoded so :meth:`emit` can render them to either
        shape).
        """
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._script_name = script_name
        self._trace_names: dict[int, str] = {}
        self._trace_spans: dict[int, List[ReadableSpan]] = {}

    def _base_name_for_trace(self, trace_id: int, failed: bool) -> str:
        """Return the shared file stem for ``trace_id``, allocating it once.

        The name encodes the run timestamp (a UTC ``YYYY-MM-DD_HH-MM-SS``
        stamp), the script, and -- when ``failed`` -- an ``.error`` marker. It
        carries no ``.json`` extension or format marker; :meth:`emit` appends
        those to derive the OTLP and readable paths, so both files share this
        exact stem. A numeric suffix is appended if a sibling trace from the
        same run/second already claimed the name, so concurrent traces never
        overwrite each other. The result is memoized so repeated calls for the
        same trace return a stable name.
        """
        if trace_id not in self._trace_names:
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
            outcome = ".error" if failed else ""
            base_name = f"{timestamp}_{self._script_name}{outcome}"
            # Guard against overwriting another trace from the same run/second.
            used = set(self._trace_names.values())
            candidate = base_name
            suffix = 1
            while candidate in used:
                candidate = f"{base_name}_{suffix}"
                suffix += 1
            self._trace_names[trace_id] = candidate
        return self._trace_names[trace_id]

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """Buffer a batch of finished spans, grouped by trace id.

        Called by OpenTelemetry as spans end. We keep the raw
        :class:`~opentelemetry.sdk.trace.ReadableSpan` objects (not a
        serialized form) because :meth:`emit` renders them to both the OTLP and
        readable shapes itself. Nothing touches disk here -- the write is
        deferred so the filename can reflect the run's final outcome -- so this
        always reports success.
        """
        for span in spans:
            trace_id = span.context.trace_id
            self._trace_spans.setdefault(trace_id, []).append(span)
        return SpanExportResult.SUCCESS

    @staticmethod
    def _in_generation_order(
        spans: List[ReadableSpan],
    ) -> List[ReadableSpan]:
        """Return ``spans`` ordered by when each was generated (started).

        Spans arrive from ``export`` in *end-time* order -- a leaf finishes
        before the parent that wraps it -- so the buffer reads roughly
        backwards, with the earliest-started (root) span landing last. We
        restore the run's original chronology by sorting on ``start_time`` (the
        epoch-nanosecond stamp OpenTelemetry records). Sorting on the real
        timestamp rather than blindly reversing keeps siblings and concurrent
        work correctly ordered.

        The sort is stable and tolerates spans without a ``start_time`` (they
        keep their relative arrival order), so nothing is lost if the field is
        ever absent.
        """
        return sorted(spans, key=lambda span: span.start_time or 0)

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        """Write ``payload`` to ``path`` as indented, newline-terminated JSON."""
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")

    @staticmethod
    def _to_readable(spans: List[ReadableSpan]) -> List[dict]:
        """Render ordered spans to the human-readable JSON shape.

        Each span goes through OpenTelemetry's own ``to_json`` (snake_case
        fields, hex ids, ISO timestamps) and then
        :func:`~argus.json_utils.expand_embedded_json`, which unescapes any
        attribute value that is itself a JSON string so nested payloads read as
        structured data rather than an escaped blob.
        """
        return [
            expand_embedded_json(json.loads(span.to_json(indent=None)))
            for span in spans
        ]

    def emit(self, failed: bool = False) -> None:
        """Persist all buffered traces, an OTLP/JSON and a readable file each.

        For every trace we write two files sharing a base name: the ``.otlp.json``
        is a single ``ExportTraceServiceRequest`` (the OTLP wire message) so its
        on-disk shape matches what the remote sink POSTs, and the
        ``.readable.json`` is a plain list of human-readable spans. ``failed``
        tags the run's outcome in the base name so a partial/errored run is still
        captured (and obvious) rather than silently discarded. Spans within each
        trace are encoded in generation order (see :meth:`_in_generation_order`)
        so both files read top-to-bottom as the run unfolded.
        """
        for trace_id, spans in self._trace_spans.items():
            base_name = self._base_name_for_trace(trace_id, failed)
            ordered = self._in_generation_order(spans)

            request = encode_spans(ordered)
            self._write_json(
                self._base_dir / f"{base_name}{_OTLP_SUFFIX}",
                _hex_encode_ids(MessageToDict(request)),
            )
            self._write_json(
                self._base_dir / f"{base_name}{_READABLE_SUFFIX}",
                self._to_readable(ordered),
            )

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """Satisfy the ``SpanExporter`` interface; a no-op here.

        Spans are held in memory until :meth:`emit`, so there is
        nothing to flush on demand. Always reports success.

        Args:
            timeout_millis: Accepted only to match the base
                :class:`~opentelemetry.sdk.trace.export.SpanExporter` signature
                (``force_flush(self, timeout_millis: int = 30000)``). There is
                no asynchronous work to bound, so the value is ignored.
        """
        return True

    def shutdown(self) -> None:
        """Satisfy the ``SpanExporter`` interface; a no-op here.

        The on-exit write is driven explicitly by Argus via
        :meth:`emit`, so no resources need releasing at shutdown.
        """
        pass
