"""A span exporter that ships OpenTelemetry traces to a remote endpoint.

This is the remote sibling of :class:`~argus.exporters.file.FileSpanExporter`,
and it deliberately shares that sink's whole philosophy: **buffer now, emit
once**. Spans are accumulated in memory as they end and nothing leaves the
process until Argus calls :meth:`~OTLPSpanExporter.emit` exactly once, on exit,
when the run's outcome is known. Where the file exporter's ``emit`` writes a
JSON file, this one POSTs every buffered span to a backend in a single
OTLP/HTTP (protobuf) request.

Mirroring the file sink -- rather than adopting OpenTelemetry's usual streaming
model, where a ``BatchSpanProcessor`` trickles spans out mid-run -- buys three
things:

* **Symmetry.** Both sinks are plain buffered exporters driven by the same
  ``emit(failed=...)`` hook and the same :class:`SimpleSpanProcessor`. There is
  no second delivery model to special-case in :func:`argus.init`.
* **One request per run.** The backend is hit once, at the end, instead of
  absorbing a stream of mid-run batches -- fewer connections and per-request
  overhead (at the cost of a single larger payload).
* **Outcome at emit time.** Like the file name's ``.error`` marker, the emit is
  driven by the known final outcome rather than sent blind mid-run.

The trade-off is the same one the file exporter already makes: a hard kill
(``SIGKILL``, power loss) before ``emit`` loses the trace, since nothing was
sent yet. A normal exception is fine -- Argus's ``atexit``/excepthook path still
flushes and tags the run.

We do not reimplement the wire protocol. OpenTelemetry's own OTLP/HTTP exporter
(the "transport" here) already handles protobuf encoding, gzip, retries, and the
standard ``OTEL_EXPORTER_OTLP_*`` environment variables; we simply hand it every
buffered span in one ``export`` call at the end. That transport lives in the
optional ``argus-trace[otlp]`` extra, so it is imported when an exporter is
constructed (i.e. when you actually ask for OTLP) -- a missing extra fails loudly
and early at :func:`argus.init` time rather than silently at exit.
"""

from __future__ import annotations

import os
from typing import List, Mapping, Optional, Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

# Standard OpenTelemetry env var naming the traces endpoint. Honored so an
# operator can point exports at their own backend without touching code -- and,
# deliberately, the *only* fallback: Argus ships no built-in default endpoint
# (see :func:`_resolve_endpoint`).
_OTLP_TRACES_ENDPOINT_ENV_VAR = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"


def _resolve_endpoint(endpoint: Optional[str]) -> str:
    """Decide the URL spans are POSTed to, most explicit source winning.

    Precedence: an explicit ``endpoint`` argument, then the standard
    ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` environment variable. There is no
    built-in default. Argus is a library anyone may install, so a hardcoded
    endpoint would either silently ship a stranger's traces to someone else's
    backend or point at a machine-local address that is meaningless to the
    caller; when neither source supplies an endpoint we raise instead of
    guessing. Kept separate from the exporter so the resolution rules can be
    exercised without importing the optional OTLP dependency.

    Raises:
        ValueError: If neither an explicit ``endpoint`` nor the
            ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` environment variable is set.
    """
    resolved = endpoint or os.environ.get(_OTLP_TRACES_ENDPOINT_ENV_VAR)
    if not resolved:
        raise ValueError(
            "No OTLP endpoint configured. Pass one explicitly (e.g. "
            "argus.init(..., otlp='https://your-backend/v1/traces')) or set "
            f"the {_OTLP_TRACES_ENDPOINT_ENV_VAR} environment variable."
        )
    return resolved


def _build_transport(
    endpoint: str,
    headers: Optional[Mapping[str, str]],
    timeout: Optional[int],
) -> SpanExporter:
    """Construct the underlying OpenTelemetry OTLP/HTTP exporter.

    This is the only place the optional dependency is touched. It's a module
    function (rather than inline in :meth:`OTLPSpanExporter.__init__`) so the
    test suite can substitute a fake transport and never needs the real
    ``argus-trace[otlp]`` extra installed.

    Raises:
        ImportError: If the extra is not installed, re-raised with a message
            naming the exact ``pip install`` needed.
    """
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter as _HTTPExporter,
        )
    except ImportError as exc:
        raise ImportError(
            "The OTLP exporter requires the optional 'otlp' extra. Install it "
            "with: pip install 'argus-trace[otlp]'"
        ) from exc

    kwargs: dict = {"endpoint": endpoint}
    if headers is not None:
        kwargs["headers"] = dict(headers)
    if timeout is not None:
        kwargs["timeout"] = timeout
    return _HTTPExporter(**kwargs)


class OTLPSpanExporter(SpanExporter):
    """Buffer spans and POST them to a backend in one OTLP/HTTP request on exit.

    The remote counterpart to :class:`~argus.exporters.file.FileSpanExporter`:
    same buffer-now/emit-once lifecycle, same :meth:`emit` hook Argus drives on
    process exit -- only the destination differs.
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        *,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> None:
        """Prepare an exporter pointed at a backend ingest endpoint.

        Args:
            endpoint: Full URL to POST spans to. When omitted, falls back to
                the ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` env var; if that is
                unset too a :class:`ValueError` is raised, since Argus has no
                default endpoint. Used verbatim -- no ``/v1/traces`` path is
                appended -- so it must be the complete route.
            headers: Extra HTTP headers sent with the export (e.g. an auth
                token). May also be supplied via the standard
                ``OTEL_EXPORTER_OTLP_TRACES_HEADERS`` env var, which the
                transport reads on its own.
            timeout: Per-export timeout in seconds; falls back to the
                transport's own default (honoring
                ``OTEL_EXPORTER_OTLP_TRACES_TIMEOUT``) when omitted.

        The transport (the real OpenTelemetry OTLP exporter) is built here, so
        a missing ``argus-trace[otlp]`` extra raises immediately rather than at
        exit. ``_spans`` is the in-memory buffer filled by :meth:`export`.

        Raises:
            ValueError: If no ``endpoint`` is given and the
                ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` env var is unset.
            ImportError: If the optional ``otlp`` extra is not installed.
        """
        self._endpoint = _resolve_endpoint(endpoint)
        self._transport = _build_transport(self._endpoint, headers, timeout)
        self._spans: List[ReadableSpan] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """Buffer a batch of finished spans; nothing leaves the process yet.

        Called by OpenTelemetry as spans end. We keep the raw
        :class:`ReadableSpan` objects (not a serialized form) because the
        transport encodes them to protobuf itself at :meth:`emit` time. The
        real send is deferred so it happens once, with the run's outcome known,
        so this always reports success.
        """
        self._spans.extend(spans)
        return SpanExportResult.SUCCESS

    def emit(self, failed: bool = False) -> None:
        """POST every buffered span to the endpoint in a single request.

        Argus calls this exactly once, on exit. All buffered spans (across every
        trace in the run) go out in one OTLP request; an empty buffer sends
        nothing. The buffer is cleared so a stray second call is a no-op.

        ``failed`` is accepted for parity with the buffered-exporter contract
        (the same hook the file sink uses to tag its filename). The remote
        backend reads a run's outcome from each span's own status rather than
        from a run-level flag, so it is not encoded separately here.
        """
        if not self._spans:
            return
        spans, self._spans = self._spans, []
        self._transport.export(spans)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """Satisfy the ``SpanExporter`` interface; a no-op here.

        Spans are held in memory until :meth:`emit`, so there is nothing to
        flush on demand. ``timeout_millis`` is accepted only to match the base
        signature and is ignored. Always reports success.
        """
        return True

    def shutdown(self) -> None:
        """Release the transport's resources (e.g. its HTTP session)."""
        self._transport.shutdown()


def make_otlp_exporter(
    endpoint: Optional[str] = None,
    *,
    headers: Optional[Mapping[str, str]] = None,
    timeout: Optional[int] = None,
) -> OTLPSpanExporter:
    """Build an :class:`OTLPSpanExporter` (a small convenience over the class).

    Handy for the ``exporters=[...]`` path and mirrors how :func:`argus.init`
    constructs the exporter internally. See :class:`OTLPSpanExporter` for the
    argument meanings.

    Raises:
        ValueError: If no ``endpoint`` is given and the
            ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` env var is unset.
        ImportError: If the optional ``argus-trace[otlp]`` extra is not
            installed.
    """
    return OTLPSpanExporter(endpoint, headers=headers, timeout=timeout)
