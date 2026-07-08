"""A span exporter that persists OpenTelemetry traces to disk as JSON.

This is Argus's default sink: the thing that turns the spans an instrumented
run emits into human-readable files you can open, diff, and triage later.

The design hinges on one deliberate choice -- **buffer now, write once**.
OpenTelemetry hands spans to :meth:`~FileSpanExporter.export` as they end,
incrementally and out of order, but at that moment we don't yet know how the
run as a whole will turn out. So rather than streaming each span to disk, we
accumulate them in memory, grouped by trace id, and defer the actual write to
:meth:`~FileSpanExporter.write_to_disk`. Argus calls that method exactly once,
on process exit, when the run's final outcome is known.

Knowing the outcome up front is what lets the *filename* carry it: a healthy
run lands at ``<timestamp>_<script>.json`` while a run that died on an
unhandled exception is tagged ``<timestamp>_<script>.error.json``. Failed runs
are therefore obvious at a glance in a directory listing and never silently
discarded. Each distinct trace gets its own file, and a collision guard keeps
two traces from the same second from clobbering one another.

The remaining methods (:meth:`~FileSpanExporter.force_flush` and
:meth:`~FileSpanExporter.shutdown`) exist only to satisfy the
:class:`~opentelemetry.sdk.trace.export.SpanExporter` contract; because all the
real work is deferred to ``write_to_disk`` they are intentionally no-ops.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from ..json_utils import expand_embedded_json


class FileSpanExporter(SpanExporter):
    """Persist spans to disk as an indented JSON array, one file per trace."""

    def __init__(self, base_dir: Path, script_name: str) -> None:
        """Prepare an exporter that writes traces under ``base_dir``.

        Args:
            base_dir: Directory traces are written to; created (with parents)
                if it doesn't already exist.
            script_name: Name stamped into each filename, identifying the run
                that produced the trace.

        The two ``_trace_*`` maps are the in-memory buffers keyed by trace id:
        ``_trace_files`` remembers the chosen path per trace (so a trace keeps
        a stable name) and ``_trace_spans`` accumulates the spans themselves.
        """
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._script_name = script_name
        self._trace_files: dict[int, Path] = {}
        self._trace_spans: dict[int, list[dict]] = {}

    def _file_for_trace(self, trace_id: int, failed: bool) -> Path:
        """Return the output path for ``trace_id``, allocating it on first use.

        The name encodes the run timestamp, the script, and -- when ``failed``
        -- an ``.error`` marker. A numeric suffix is appended if a sibling
        trace from the same run/second already claimed the name, so concurrent
        traces never overwrite each other. The result is memoized so repeated
        calls for the same trace return a stable path.
        """
        if trace_id not in self._trace_files:
            timestamp = datetime.now(timezone.utc).strftime("%d-%m-%y_%H-%M-%S")
            outcome = ".error" if failed else ""
            base_name = f"{timestamp}_{self._script_name}{outcome}"
            path = self._base_dir / f"{base_name}.json"
            # Guard against overwriting another trace from the same run/second.
            used = set(self._trace_files.values())
            suffix = 1
            while path in used:
                path = self._base_dir / f"{base_name}_{suffix}.json"
                suffix += 1
            self._trace_files[trace_id] = path
        return self._trace_files[trace_id]

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """Buffer a batch of finished spans, grouped by trace id.

        Called by OpenTelemetry as spans end. Each span is serialized to a
        plain dict (with any embedded JSON strings expanded for readability)
        and appended to its trace's buffer. Nothing touches disk here -- the
        write is deferred to :meth:`write_to_disk` so the filename can reflect
        the run's final outcome -- so this always reports success.
        """
        for span in spans:
            trace_id = span.context.trace_id
            self._trace_spans.setdefault(trace_id, []).append(
                expand_embedded_json(json.loads(span.to_json(indent=None)))
            )
        return SpanExportResult.SUCCESS

    @staticmethod
    def _in_generation_order(spans: list[dict]) -> list[dict]:
        """Return ``spans`` ordered by when each was generated (started).

        Spans arrive from ``export`` in *end-time* order -- a leaf finishes
        before the parent that wraps it -- so the buffer reads roughly
        backwards, with the earliest-started (root) span landing last. We
        restore the run's original chronology by sorting on the ``start_time``
        stamped by OpenTelemetry (an ISO-8601 UTC string, hence directly
        comparable). Sorting on the real timestamp rather than blindly
        reversing keeps siblings and concurrent work correctly ordered.

        The sort is stable and tolerates spans without a ``start_time`` (they
        keep their relative arrival order), so nothing is lost if the field is
        ever absent.
        """
        return sorted(spans, key=lambda span: span.get("start_time") or "")

    def write_to_disk(self, failed: bool = False) -> None:
        """Persist all buffered traces, one indented JSON file per trace.

        ``failed`` tags the run's outcome in the filename so a partial/errored
        run is still captured (and obvious) rather than silently discarded.
        Spans within each trace are written in generation order (see
        :meth:`_in_generation_order`) so the file reads top-to-bottom as the
        run unfolded.
        """
        for trace_id, spans in self._trace_spans.items():
            path = self._file_for_trace(trace_id, failed)
            with path.open("w", encoding="utf-8") as handle:
                json.dump(self._in_generation_order(spans), handle, indent=2)
                handle.write("\n")

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """Satisfy the ``SpanExporter`` interface; a no-op here.

        Spans are held in memory until :meth:`write_to_disk`, so there is
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
        :meth:`write_to_disk`, so no resources need releasing at shutdown.
        """
        pass
