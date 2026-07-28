"""A span exporter that persists OpenTelemetry traces to disk in two shapes.

This is Argus's default sink. For every trace it writes *two* files that share a
base name and differ only by a format marker:

* ``<timestamp>_<script>.otlp.json`` -- canonical OTLP/JSON, the exact shape the
  wire protocol uses, so it can be replayed to any OTLP backend.
* ``<timestamp>_<script>.readable.json`` -- a human-readable rendering: a plain
  list of spans with embedded JSON payloads unescaped.

Spans are buffered as they end and written when :meth:`FileSpanExporter.emit`
runs, which is what lets the filename carry the run's outcome.
:func:`trace_filename` owns the naming scheme in full.

See ``docs/design-notes.md`` ("Buffer now, emit once", "Two files per trace",
"Trace filenames", "Hex ids in OTLP/JSON", "Generation order") for the reasoning
behind all of that.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from google.protobuf.json_format import MessageToDict
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import ReadableSpan

from ..json_utils import expand_embedded_json
from ..paths import default_traces_dir, detect_script_name
from .base import Delivery, _DeferredExporter

#: Which of a trace's two files is meant; also the file's own format marker.
TraceFormat = Literal["otlp", "readable"]

# The OTLP/JSON id fields (``bytes`` in protobuf) that must be hex, not the
# base64 the proto3 JSON mapping -- and thus ``MessageToDict`` -- defaults to.
_ID_JSON_KEYS = frozenset({"traceId", "spanId", "parentSpanId"})


def trace_filename(
    script_name: str,
    trace_format: TraceFormat,
    *,
    failed: bool = False,
    timestamp: datetime | None = None,
    sequence: int = 0,
) -> str:
    """Return the file name a trace is written under.

    The whole scheme lives here, in one place to read, test, and point at::

        <YYYY-MM-DD_HH-MM-SS>_<script>[.error][_<sequence>].<format>.json

    Args:
        script_name: Identifies the run that produced the trace.
        trace_format: ``"otlp"`` for canonical OTLP/JSON, ``"readable"`` for the
            human-readable rendering. A trace's two files differ in this and
            nothing else.
        failed: Tags a run that died on an unhandled exception, so a failure is
            obvious in a directory listing.
        timestamp: When the trace was captured; defaults to now. Rendered in UTC
            -- an aware value is converted, a naive one is taken as already UTC.
        sequence: Tiebreaker for traces that would otherwise share a name: same
            script, same second, same outcome. ``0`` contributes nothing.

    Returns:
        A bare file name, e.g. ``2026-01-31_14-05-09_my_agent.error.otlp.json``.

    See ``docs/design-notes.md`` ("Trace filenames") for why the parts are
    ordered this way.
    """
    moment = timestamp if timestamp is not None else datetime.now(timezone.utc)
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    stamp = moment.strftime("%Y-%m-%d_%H-%M-%S")
    outcome = ".error" if failed else ""
    tiebreak = f"_{sequence}" if sequence else ""
    return f"{stamp}_{script_name}{outcome}{tiebreak}.{trace_format}.json"


@dataclass(frozen=True)
class _TraceNaming:
    """The naming a trace commits to for the rest of the run.

    Chosen the first time a trace is written and kept, so both of its files --
    and any rewrite from a later emit -- land on the same names.
    """

    script_name: str
    timestamp: datetime
    failed: bool
    sequence: int = 0

    def filename(self, trace_format: TraceFormat) -> str:
        """Return this trace's file name in ``trace_format``."""
        return trace_filename(
            self.script_name,
            trace_format,
            failed=self.failed,
            timestamp=self.timestamp,
            sequence=self.sequence,
        )


def _hex_encode_ids(node: Any) -> Any:
    """Return ``node`` with every OTLP id field re-encoded from base64 to hex.

    Walks the encoded structure, converting the reserved id keys and leaving
    everything else untouched. See ``docs/design-notes.md`` ("Hex ids in
    OTLP/JSON").
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


class FileSpanExporter(_DeferredExporter):
    """Persist spans to disk, one OTLP/JSON and one readable file per trace."""

    def __init__(
        self,
        base_dir: str | Path | None = None,
        script_name: str | None = None,
    ) -> None:
        """Prepare an exporter that writes traces under ``base_dir``.

        Both arguments default to what :func:`argus.init` would derive, so
        ``FileSpanExporter()`` is the default sink -- handy for keeping the trace
        files while adding a sink of your own through ``exporters=``.

        Args:
            base_dir: Directory traces are written to; created (with parents)
                if it doesn't already exist. Defaults to ``<cwd>/traces``.
            script_name: Name stamped into each filename, identifying the run
                that produced the trace. Defaults to the running script's name
                (see :func:`~argus.paths.detect_script_name`).
        """
        super().__init__()
        self._base_dir = (
            Path(base_dir) if base_dir is not None else default_traces_dir()
        )
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._script_name = script_name or detect_script_name()
        self._naming_by_trace: dict[int, _TraceNaming] = {}

    def _naming_for(self, trace_id: int, failed: bool) -> _TraceNaming:
        """Return ``trace_id``'s naming, allocating it on first write.

        The outcome is captured on first write and kept, and ``sequence`` is
        raised until the name is free among those this exporter has allocated, so
        a trace's two files stay together and siblings from the same run and
        second don't collide. The directory isn't consulted, so another process
        writing there can still overwrite these files. See ``docs/design-notes.md``
        ("Trace filenames").
        """
        naming = self._naming_by_trace.get(trace_id)
        if naming is None:
            claimed = {
                taken.filename("otlp")
                for taken in self._naming_by_trace.values()
            }
            naming = _TraceNaming(
                self._script_name, datetime.now(timezone.utc), failed
            )
            while naming.filename("otlp") in claimed:
                naming = replace(naming, sequence=naming.sequence + 1)
            self._naming_by_trace[trace_id] = naming
        return naming

    @staticmethod
    def _group_by_trace(
        spans: list[ReadableSpan],
    ) -> dict[int, list[ReadableSpan]]:
        """Return ``spans`` bucketed by trace id, each bucket in arrival order."""
        traces: dict[int, list[ReadableSpan]] = {}
        for span in spans:
            traces.setdefault(span.context.trace_id, []).append(span)
        return traces

    @staticmethod
    def _in_generation_order(
        spans: list[ReadableSpan],
    ) -> list[ReadableSpan]:
        """Return ``spans`` ordered by when each was generated (started).

        Spans arrive in end-time order, so this restores the run's chronology.
        The sort is stable and tolerates a missing ``start_time``. See
        ``docs/design-notes.md`` ("Generation order").
        """
        return sorted(spans, key=lambda span: span.start_time or 0)

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        """Write ``payload`` to ``path`` as indented, newline-terminated JSON."""
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")

    @staticmethod
    def _to_readable(spans: list[ReadableSpan]) -> list[dict[str, Any]]:
        """Render ordered spans to the human-readable JSON shape.

        Each span goes through OpenTelemetry's own ``to_json`` and then
        :func:`~argus.json_utils.expand_embedded_json`, which unescapes any
        attribute value that is itself a JSON string.
        """
        return [
            expand_embedded_json(json.loads(span.to_json(indent=None)))
            for span in spans
        ]

    def _deliver(self, spans: list[ReadableSpan], *, failed: bool) -> Delivery:
        """Write each buffered trace's pair of files, keeping the buffer.

        Args:
            spans: Every span buffered so far, grouped into traces here.
            failed: Tags the run's outcome into each trace's file names, so a
                crashed run is captured and obvious rather than discarded.

        Returns:
            :attr:`Delivery.RETAINED` always: the buffer is kept so a later emit
            rewrites each file from the complete trace rather than appending a
            fragment. See ``docs/design-notes.md`` ("Repeat emits: rewrite or
            clear").
        """
        for trace_id, trace_spans in self._group_by_trace(spans).items():
            naming = self._naming_for(trace_id, failed)
            ordered = self._in_generation_order(trace_spans)

            self._write_json(
                self._base_dir / naming.filename("otlp"),
                _hex_encode_ids(MessageToDict(encode_spans(ordered))),
            )
            self._write_json(
                self._base_dir / naming.filename("readable"),
                self._to_readable(ordered),
            )
        return Delivery.RETAINED
