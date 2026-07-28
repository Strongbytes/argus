"""Tests for the shared buffered-exporter contract in :mod:`argus.exporters.base`.

Both built-in sinks are built on ``_DeferredExporter``, so what is exercised here
is what neither exporter's own tests should have to re-prove: spans accumulate in
``export``, ``emit`` hands the whole buffer over, and the buffer is kept or
dropped according to what ``_deliver`` reports. That last decision is the one
place the two sinks differ, and having it live in one template is what stops them
from drifting apart.
"""

from __future__ import annotations

from typing import Any

from opentelemetry.sdk.trace.export import SpanExportResult

from argus.exporters import BufferedSpanExporter, FileSpanExporter
from argus.exporters.base import Delivery, _DeferredExporter

from tests.factories import PlainSpanExporter, make_span


class _Sink(_DeferredExporter):
    """A minimal sink recording each delivery, with configurable consumption.

    ``outcome`` is what ``_deliver`` reports back: :attr:`Delivery.CONSUMED` for
    a destination that cannot take the same spans twice (the remote sink after a
    confirmed POST), :attr:`Delivery.RETAINED` for one that is rewritten from the
    whole buffer (the file sink) or that failed and wants a retry.
    """

    def __init__(self, outcome: Delivery = Delivery.CONSUMED) -> None:
        super().__init__()
        self.outcome = outcome
        self.deliveries: list[tuple[list[Any], bool]] = []

    def _deliver(self, spans, *, failed):
        self.deliveries.append((list(spans), failed))
        return self.outcome


class TestProtocol:
    """``BufferedSpanExporter`` is how Argus tells the two kinds of sink apart."""

    def test_a_builtin_sink_satisfies_it(self, traces_dir):
        assert isinstance(
            FileSpanExporter(traces_dir, script_name="s"), BufferedSpanExporter
        )

    def test_a_stock_exporter_does_not(self):
        # The other half of the ``exporters=`` contract: no emit hook, so Argus
        # must drive it with force_flush/shutdown instead.
        assert not isinstance(PlainSpanExporter(), BufferedSpanExporter)

    def test_no_inheritance_is_required(self):
        # It is a runtime-checkable Protocol, so a caller's exporter opts in by
        # defining emit -- not by importing anything from Argus.
        class OwnSink:
            def emit(self, failed: bool = False) -> None:
                pass

        assert isinstance(OwnSink(), BufferedSpanExporter)


class TestBuffering:
    def test_export_buffers_without_delivering(self):
        sink = _Sink()

        result = sink.export([make_span(), make_span()])

        assert result is SpanExportResult.SUCCESS
        assert sink.deliveries == []

    def test_emit_delivers_the_whole_buffer_with_the_outcome(self):
        sink = _Sink()
        first, second = make_span(), make_span()
        sink.export([first])
        sink.export([second])

        sink.emit(failed=True)

        # One delivery carrying every span buffered so far, in arrival order.
        assert sink.deliveries == [([first, second], True)]

    def test_emit_with_an_empty_buffer_delivers_nothing(self):
        sink = _Sink()

        sink.emit()

        assert sink.deliveries == []

    def test_consumed_spans_are_not_delivered_again(self):
        sink = _Sink(outcome=Delivery.CONSUMED)
        sink.export([make_span()])

        sink.emit()
        sink.emit()

        # The remote sink's shape: an accepted batch leaves the buffer, so a
        # repeat emit has nothing to send.
        assert len(sink.deliveries) == 1

    def test_retained_spans_are_delivered_again(self):
        sink = _Sink(outcome=Delivery.RETAINED)
        first = make_span()
        sink.export([first])
        sink.emit()

        second = make_span()
        sink.export([second])
        sink.emit()

        # The file sink's shape: the buffer is kept, so the second delivery
        # carries the complete trace rather than only what is new.
        assert sink.deliveries == [([first], False), ([first, second], False)]

    def test_force_flush_and_shutdown_are_noops(self):
        sink = _Sink()

        # Nothing is held anywhere but the buffer until emit, so there is
        # neither work to force nor a resource to release.
        assert sink.force_flush() is True
        assert sink.shutdown() is None
