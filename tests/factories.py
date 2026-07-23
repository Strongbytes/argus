"""Test factories for Argus.

Argus's real instrumentors are optional third-party packages and OpenTelemetry
exporters do real I/O, so the suite never touches either. Instead these
factories build lightweight stand-ins that mimic exactly the surface Argus
relies on:

* :class:`FakeInstrumentor` -- the slice of the OpenTelemetry
  ``BaseInstrumentor`` API that :func:`argus.init` and :func:`argus._reset`
  call (``instrument`` / ``uninstrument``), with call recording.
* :class:`RecordingExporter` -- a real :class:`SpanExporter` that records the
  spans it is handed and the ``emit`` flushes Argus drives on exit.
* :class:`FakeSpan` -- the minimal ``ReadableSpan`` shape
  :class:`~argus.exporters.file.FileSpanExporter` consumes
  (``context.trace_id`` + ``to_json``).
* :func:`patch_resolve_instrumentors` -- swaps the framework-detection seam so
  ``init`` turns on the fakes we hand it instead of probing the environment.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence

from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult


class FakeInstrumentor:
    """Stand-in for an OpenInference/OpenTelemetry ``BaseInstrumentor``.

    Records the ``tracer_provider`` passed to each ``instrument`` call and how
    often ``uninstrument`` ran, so tests can assert that Argus wires a
    framework exactly once and tears it down on reset.
    """

    def __init__(self, name: str = "FakeInstrumentor") -> None:
        self.name = name
        self.instrument_calls: List[Any] = []
        self.uninstrument_count = 0

    @property
    def instrumented(self) -> bool:
        return len(self.instrument_calls) > self.uninstrument_count

    def instrument(self, *, tracer_provider: Any = None, **_: Any) -> None:
        self.instrument_calls.append(tracer_provider)

    def uninstrument(self, **_: Any) -> None:
        self.uninstrument_count += 1


def make_instrumentor(name: str = "FakeInstrumentor") -> FakeInstrumentor:
    """Return a fresh :class:`FakeInstrumentor`."""
    return FakeInstrumentor(name=name)


class RaisingUninstrumentor(FakeInstrumentor):
    """A fake whose ``uninstrument`` raises, to test reset's resilience."""

    def uninstrument(self, **_: Any) -> None:
        self.uninstrument_count += 1
        raise RuntimeError("uninstrument boom")


class RecordingExporter(SpanExporter):
    """A real ``SpanExporter`` that records exports and emit flushes.

    Implements the ``emit(failed=...)`` hook that
    :meth:`argus.Session.flush` looks for, so tests can assert the failure flag
    propagates without writing any files or hitting the network.
    """

    def __init__(self) -> None:
        self.exported_spans: List[Any] = []
        self.emit_calls: List[bool] = []
        self.shutdown_count = 0

    def export(self, spans: Sequence[Any]) -> SpanExportResult:
        self.exported_spans.extend(spans)
        return SpanExportResult.SUCCESS

    def emit(self, failed: bool = False) -> None:
        self.emit_calls.append(failed)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True

    def shutdown(self) -> None:
        self.shutdown_count += 1


@dataclass(frozen=True)
class _FakeSpanContext:
    trace_id: int


_TRACE_IDS = itertools.count(1)


@dataclass
class FakeSpan:
    """Minimal ``ReadableSpan`` stand-in for the file exporter.

    The exporter only reads ``context.trace_id`` and calls ``to_json`` before
    re-parsing, so we expose just those.
    """

    trace_id: int
    payload: dict = field(default_factory=dict)

    @property
    def context(self) -> _FakeSpanContext:
        return _FakeSpanContext(self.trace_id)

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.payload, indent=indent)


def make_span(trace_id: Optional[int] = None, **payload: Any) -> FakeSpan:
    """Return a :class:`FakeSpan`, auto-assigning a trace id when omitted."""
    return FakeSpan(
        trace_id=next(_TRACE_IDS) if trace_id is None else trace_id,
        payload=payload,
    )


def patch_resolve_instrumentors(
    monkeypatch, instances: Sequence[Any]
) -> List[Any]:
    """Make :func:`argus.init` turn on ``instances`` instead of real detection.

    Patches the ``resolve_instrumentors`` name as imported into
    ``argus.session`` and returns the recorded ``instrument`` argument list so a
    test can assert what selection ``init`` requested.
    """
    received: List[Any] = []

    def fake_resolve(instrument):
        received.append(instrument)
        return list(instances)

    monkeypatch.setattr("argus.session.resolve_instrumentors", fake_resolve)
    return received
