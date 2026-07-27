"""Test factories for Argus.

Argus's real instrumentors are optional third-party packages and OpenTelemetry
exporters do real I/O, so the suite never touches either. Instead these
factories build lightweight stand-ins that mimic exactly the surface Argus
relies on:

* :class:`FakeInstrumentor` (built by :func:`make_instrumentor`) -- the slice of
  the OpenTelemetry ``BaseInstrumentor`` API that :func:`argus.init` and
  :func:`argus.reset` call (``instrument`` / ``uninstrument``), with call
  recording.
* :class:`RaisingUninstrumentor` -- the same fake with a failing
  ``uninstrument``, so a test can assert that :func:`argus.reset` tears the
  session down anyway rather than propagating a sink's teardown error.
* :class:`RecordingExporter` -- a real :class:`SpanExporter` that records the
  spans it is handed and the ``emit`` flushes Argus drives on exit.
* :class:`PlainSpanExporter` -- the same, minus the ``emit`` hook, standing in
  for any third-party exporter passed via ``exporters=``.
* :func:`make_span` -- a real :class:`~opentelemetry.sdk.trace.ReadableSpan`
  with a caller-chosen trace id, start time, and attributes. It has to be
  genuine (context, resource, scope, timings) because the file exporter encodes
  buffered spans with the OTLP span encoder, which a hand-rolled stand-in cannot
  satisfy.
* :func:`patch_resolve_instrumentors` -- swaps the framework-detection seam so
  ``init`` turns on the fakes we hand it instead of probing the environment.
"""

from __future__ import annotations

import itertools
from typing import Any, List, Optional, Sequence

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import SpanContext, SpanKind, TraceFlags


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


class PlainSpanExporter(SpanExporter):
    """A stock ``SpanExporter`` with no ``emit`` hook, as a third party's would be.

    :class:`RecordingExporter` covers exporters that opt into Argus's
    buffer-now/emit-once lifecycle; this covers the other half of the
    ``exporters=`` contract. Argus must drive it the ordinary OpenTelemetry way
    -- spans handed over as they end, ``force_flush`` when the session flushes,
    ``shutdown`` at process exit -- because nothing else in the pipeline will
    (Argus disables the provider's own shutdown handler).
    """

    def __init__(self) -> None:
        self.exported_spans: List[Any] = []
        self.force_flush_count = 0
        self.shutdown_count = 0

    def export(self, spans: Sequence[Any]) -> SpanExportResult:
        self.exported_spans.extend(spans)
        return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        self.force_flush_count += 1
        return True

    def shutdown(self) -> None:
        self.shutdown_count += 1


_TRACE_IDS = itertools.count(1)
# Every span needs a unique, non-zero span id for the OTLP encoder; a shared
# counter keeps them distinct across a whole test run.
_SPAN_IDS = itertools.count(1)

# One resource/scope shared by all fabricated spans: the file exporter groups by
# trace id, not by these, and reusing them keeps the encoded output tidy.
_TEST_RESOURCE = Resource.create({"service.name": "test"})
_TEST_SCOPE = InstrumentationScope("argus-tests")


def make_span(
    trace_id: Optional[int] = None,
    *,
    name: str = "span",
    start_time: Optional[int] = None,
    **attributes: Any,
) -> ReadableSpan:
    """Return a real :class:`ReadableSpan` the OTLP encoder can serialize.

    Auto-assigns a trace id (and always a fresh span id) when omitted. Extra
    keyword arguments become span attributes, so ``make_span(output="x")``
    stamps ``{"output": "x"}`` onto the span. ``start_time`` is an epoch
    nanosecond stamp used to exercise generation-order sorting; when omitted the
    span carries no start time (matching the "field absent" path).
    """
    trace_id = next(_TRACE_IDS) if trace_id is None else trace_id
    context = SpanContext(
        trace_id=trace_id,
        span_id=next(_SPAN_IDS),
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    return ReadableSpan(
        name=name,
        context=context,
        resource=_TEST_RESOURCE,
        instrumentation_scope=_TEST_SCOPE,
        attributes=dict(attributes),
        kind=SpanKind.INTERNAL,
        start_time=start_time,
        end_time=start_time,
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
