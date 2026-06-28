"""A span exporter that persists OpenTelemetry traces to disk as JSON.

Spans are buffered in memory as they end and written out in one shot by
:meth:`FileSpanExporter.write_to_disk`, which Argus calls on process exit. The
run's outcome (``ok`` vs ``error``) is encoded in the filename so failed runs
are easy to spot and triage: a failed run produces ``<base>.error.json``.
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
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._script_name = script_name
        self._trace_files: dict[int, Path] = {}
        self._trace_spans: dict[int, list[dict]] = {}

    def _file_for_trace(self, trace_id: int, failed: bool) -> Path:
        if trace_id not in self._trace_files:
            timestamp = datetime.now(timezone.utc).strftime("%d-%m-%y_%H:%M:%S")
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
        # Buffer spans in memory only. The actual write happens in
        # write_to_disk() so the filename can reflect the run's final outcome.
        for span in spans:
            trace_id = span.context.trace_id
            self._trace_spans.setdefault(trace_id, []).append(
                expand_embedded_json(json.loads(span.to_json(indent=None)))
            )
        return SpanExportResult.SUCCESS

    def write_to_disk(self, failed: bool = False) -> None:
        """Persist all buffered traces, one indented JSON file per trace.

        ``failed`` tags the run's outcome in the filename so a partial/errored
        run is still captured (and obvious) rather than silently discarded.
        """
        for trace_id, spans in self._trace_spans.items():
            path = self._file_for_trace(trace_id, failed)
            with path.open("w", encoding="utf-8") as handle:
                json.dump(spans, handle, indent=2)
                handle.write("\n")

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True

    def shutdown(self) -> None:
        pass
