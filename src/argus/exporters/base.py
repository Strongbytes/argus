"""The buffered-exporter contract: buffer in ``export``, act in ``emit``.

Argus's own sinks -- and any exporter a caller writes -- opt into a deferred
lifecycle: spans are buffered as they end and the real work happens in an
``emit(failed=...)`` call Argus drives on flush.
:class:`BufferedSpanExporter` is that contract as a type;
:class:`_DeferredExporter` is the skeleton both built-in sinks share.

See ``docs/design-notes.md`` ("Buffer now, emit once", "Repeat emits: rewrite or
clear") for why the lifecycle is shaped this way.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum, auto
from typing import Protocol, runtime_checkable

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult


class Delivery(Enum):
    """What a :meth:`_DeferredExporter._deliver` call decided about its spans.

    The one decision a buffered-sink subclass makes is whether the buffer holds
    on to the spans it was just handed. Two outcomes drop them -- the run is done
    with them, whether the backend took them (:attr:`CONSUMED`) or will never
    take them (:attr:`DISCARDED`) -- and one keeps them for a later emit to
    rewrite or retry (:attr:`RETAINED`). See ``docs/design-notes.md`` ("Repeat
    emits: rewrite or clear").
    """

    # Delivered and accepted: drop the spans so they are never sent twice.
    CONSUMED = auto()
    # Undeliverable and not worth another attempt (a rejected key, a wrong
    # endpoint): drop the spans so a later flush doesn't retry a send that
    # cannot succeed. The failure has already been surfaced as a warning.
    DISCARDED = auto()
    # A file to rewrite from the whole buffer, or a send that failed transiently
    # and deserves a retry: keep the spans for the next emit.
    RETAINED = auto()


@runtime_checkable
class BufferedSpanExporter(Protocol):
    """A :class:`SpanExporter` that defers its real work to :meth:`emit`.

    Argus checks every exporter in ``exporters=`` against this protocol: one that
    satisfies it is driven through :meth:`emit` and handed the run's outcome, one
    that does not is drained with ``force_flush`` and ``shutdown`` instead.
    Implementations are ordinary :class:`SpanExporter` subclasses that add
    ``emit``; the protocol is runtime-checkable, so nothing needs to inherit from
    it here. See ``docs/design-notes.md`` ("Extension points are protocols").
    """

    def emit(self, failed: bool = False) -> None:
        """Do the deferred work for everything buffered so far.

        Args:
            failed: Whether the run ended in an unhandled exception. A sink with
                no use for the outcome accepts it for parity.

        May be called more than once, so an implementation has to decide what a
        repeat call does with spans it has already handled.
        """
        ...


class _DeferredExporter(SpanExporter):
    """Skeleton for a sink that buffers spans and emits them on demand.

    Holds the buffer, satisfies the parts of the
    :class:`~opentelemetry.sdk.trace.export.SpanExporter` interface that a
    deferred sink has no work for, and templates :meth:`emit`. A subclass
    supplies :meth:`_deliver` alone, whose return value decides whether the
    delivered spans leave the buffer -- the one point where the two built-in
    sinks differ.
    """

    def __init__(self) -> None:
        self._spans: list[ReadableSpan] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """Buffer a batch of finished spans; nothing is written or sent yet.

        Called by OpenTelemetry as spans end. The raw
        :class:`~opentelemetry.sdk.trace.ReadableSpan` objects are kept, so
        :meth:`_deliver` can render them however its destination needs. Nothing
        can fail here, so this always reports success.
        """
        self._spans.extend(spans)
        return SpanExportResult.SUCCESS

    def emit(self, failed: bool = False) -> None:
        """Hand every buffered span to :meth:`_deliver`; an empty buffer is a no-op.

        Args:
            failed: Whether the run ended in an unhandled exception, passed
                through to :meth:`_deliver`.
        """
        if not self._spans:
            return
        outcome = self._deliver(list(self._spans), failed=failed)
        # Anything but RETAINED means the run is done with these spans -- they
        # were delivered or are undeliverable -- so the buffer lets them go.
        if outcome is not Delivery.RETAINED:
            self._spans = []

    def _deliver(self, spans: list[ReadableSpan], *, failed: bool) -> Delivery:
        """Write or send ``spans``; return whether they may leave the buffer.

        Args:
            spans: Every span buffered so far, in arrival order.
            failed: Whether the run ended in an unhandled exception.

        Returns:
            :attr:`Delivery.CONSUMED` if the spans are now consumed and must not
            be delivered again (a POST the backend accepted);
            :attr:`Delivery.DISCARDED` if they cannot be delivered and a retry
            would fail the same way (a rejected key, a wrong endpoint), so they
            are dropped rather than re-attempted; :attr:`Delivery.RETAINED` to
            keep them for the next emit (a file rewritten from the whole buffer,
            or a send that failed transiently and deserves a retry).
        """
        raise NotImplementedError

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """Satisfy the ``SpanExporter`` interface; a no-op here.

        Spans are held in memory until :meth:`emit`, so there is nothing to flush
        on demand and nothing to bound: ``timeout_millis`` is accepted only to
        match the base signature and is ignored. Always reports success.
        """
        return True

    def shutdown(self) -> None:
        """Release the sink's resources; nothing to do unless a subclass says so."""
        pass
