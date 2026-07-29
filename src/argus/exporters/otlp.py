"""A span exporter that ships OpenTelemetry traces to a remote endpoint.

The remote sibling of :class:`~argus.exporters.file.FileSpanExporter`: spans are
buffered as they end and POSTed to a backend in a single OTLP/HTTP (protobuf)
request when :meth:`BufferedOTLPExporter.emit` runs on exit. The wire work is
OpenTelemetry's own OTLP/HTTP exporter -- the "transport" below -- which lives in
the optional ``argus-trace[otlp]`` extra.

:class:`OtlpConfig` is how a caller asks :func:`argus.init` for all of this; the
exporter is the object it builds. Both the endpoint and the API key are resolved
when that exporter is constructed, so a misconfiguration surfaces at ``init``
rather than at interpreter shutdown with the run's whole trace already buffered.

See ``docs/design-notes.md`` ("Buffer now, emit once", "Remote export is one
argument", "No default OTLP endpoint", "Credentials resolved at construction",
"Delivery failures warn, never raise") for the reasoning behind all of that.
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Mapping
from dataclasses import dataclass

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from .base import Delivery, _DeferredExporter

# Standard OpenTelemetry env vars naming the endpoint. The traces-specific one is
# a complete URL; the generic one is a base shared by every signal, to which the
# traces path below is appended.
_OTLP_TRACES_ENDPOINT_ENV_VAR = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
_OTLP_ENDPOINT_ENV_VAR = "OTEL_EXPORTER_OTLP_ENDPOINT"
_TRACES_EXPORT_PATH = "v1/traces"

# Where the API key comes from when it isn't passed explicitly, and the header it
# travels in. Named for Aegis because the credential is issued and validated by
# the Aegis backend that receives the traces; Argus only presents it.
_API_KEY_ENV_VAR = "AEGIS_API_KEY"
_AUTH_HEADER = "Authorization"


@dataclass(frozen=True)
class OtlpConfig:
    """Remote-export settings -- :func:`argus.init`'s ``otlp`` argument.

    Every field is optional, so a bare ``OtlpConfig()`` says "export remotely,
    taking the endpoint and key from the environment", which is what
    ``otlp=True`` is shorthand for. Grouping the settings here is what keeps
    ``init`` from carrying arguments that mean nothing unless remote export is on
    (see ``docs/design-notes.md``, "Remote export is one argument").

    Attributes:
        endpoint: Full URL to POST spans to, used verbatim. Omit it to read the
            standard OTel endpoint env vars (see ``README.md``, "Remote export
            over OTLP", for the resolution order).
        api_key: Key authenticating the export, sent as
            ``Authorization: Bearer <key>``. Omit it to read ``AEGIS_API_KEY``
            (see ``README.md``, "Authentication").
        headers: Extra HTTP headers sent alongside the credential.
        timeout: Per-export timeout in seconds; the transport's own default
            applies when omitted.
    """

    endpoint: str | None = None
    api_key: str | None = None
    headers: Mapping[str, str] | None = None
    timeout: float | None = None


def _resolve_endpoint(endpoint: str | None) -> str:
    """Decide the URL spans are POSTed to, most explicit source winning.

    Precedence follows OpenTelemetry's own: an explicit ``endpoint`` argument,
    then ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT``, then the all-signals
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` with ``v1/traces`` appended. An explicit
    endpoint is stripped of surrounding whitespace. Argus ships no default, and
    this is kept out of the exporter class so the rules can be tested without the
    optional OTLP dependency. See ``docs/design-notes.md`` ("No default OTLP
    endpoint").

    Raises:
        ValueError: If ``endpoint`` is given but blank (which reads as failed to
            resolve, not "use the environment"), or if it is omitted and neither
            environment variable is set.
    """
    if endpoint is not None:
        endpoint = endpoint.strip()
        if not endpoint:
            raise ValueError(
                "An empty OTLP endpoint was configured. Omit the endpoint to "
                "read it from the standard OTel environment variables "
                f"({_OTLP_TRACES_ENDPOINT_ENV_VAR} or "
                f"{_OTLP_ENDPOINT_ENV_VAR}), or pass a complete URL."
            )
        return endpoint
    traces_endpoint = os.environ.get(_OTLP_TRACES_ENDPOINT_ENV_VAR)
    if traces_endpoint:
        return traces_endpoint
    base = os.environ.get(_OTLP_ENDPOINT_ENV_VAR)
    if base:
        separator = "" if base.endswith("/") else "/"
        return f"{base}{separator}{_TRACES_EXPORT_PATH}"
    raise ValueError(
        "No OTLP endpoint configured. Pass one explicitly, e.g. "
        "argus.init(..., otlp=OtlpConfig('https://your-backend/v1/traces')), "
        f"or set the {_OTLP_TRACES_ENDPOINT_ENV_VAR} environment variable (or "
        f"{_OTLP_ENDPOINT_ENV_VAR}, to which /{_TRACES_EXPORT_PATH} is "
        "appended)."
    )


def _resolve_auth_headers(
    api_key: str | None,
    headers: Mapping[str, str] | None,
) -> Mapping[str, str]:
    """Turn an API key into the ``Authorization`` header the backend expects.

    An explicit ``api_key`` wins over the ``AEGIS_API_KEY`` environment
    variable, and any ``headers`` given are merged around the resolved
    ``Authorization: Bearer <key>`` rather than replacing it. A key is mandatory.
    See ``docs/design-notes.md`` ("Credentials resolved at construction").

    Returns:
        The headers to hand the transport -- the caller's, plus the bearer
        credential.

    Raises:
        ValueError: If no key is resolvable, if the resolved key contains
            whitespace, or if ``headers`` already carries an ``Authorization``
            entry.
    """
    resolved = (
        api_key if api_key is not None else os.environ.get(_API_KEY_ENV_VAR)
    )
    resolved = resolved.strip() if resolved is not None else None
    if not resolved:
        raise ValueError(
            "No Aegis API key configured. Set the "
            f"{_API_KEY_ENV_VAR} environment variable (a .env file in or above "
            "your working directory is read for you) or pass one explicitly, "
            "e.g. argus.init(..., otlp=OtlpConfig(api_key='sk_...'))."
        )
    if any(char.isspace() for char in resolved):
        raise ValueError(
            "The Aegis API key contains whitespace, which cannot be sent in an "
            f"{_AUTH_HEADER} header. Check the value passed to api_key or held "
            f"in {_API_KEY_ENV_VAR}."
        )
    merged = dict(headers or {})
    if any(name.lower() == _AUTH_HEADER.lower() for name in merged):
        raise ValueError(
            f"An {_AUTH_HEADER} header was passed in headers, but the api_key "
            "is what authenticates the export and would overwrite it. Drop the "
            "header and let the key set it."
        )
    merged[_AUTH_HEADER] = f"Bearer {resolved}"
    return merged


def _describe_failure(
    status_code: int | None, error: BaseException | None
) -> str:
    """Explain why a delivery failed, in one clause fit to drop into a warning.

    The transport reports a rejected batch only as a failed return value -- the
    HTTP status and any connection error it saw are logged and then discarded --
    so on its own a delivery warning can say no more than "not delivered". Given
    the status code the capturing transport kept (see :func:`_build_transport`),
    or the exception a connection-level failure raised, this turns that outcome
    into an actionable reason: a rejected key reads as a 401, an unreachable host
    as the error it failed with. See ``docs/design-notes.md`` ("Delivery failures
    name their cause").

    Kept a module function, like :func:`_resolve_endpoint`, so the mapping can be
    tested without importing the optional OTLP dependency.

    Args:
        status_code: The HTTP status the backend returned, or ``None`` when the
            batch never got a response (a connection-level failure, or a status
            the transport did not surface).
        error: The exception a connection-level failure raised, or ``None``.

    Returns:
        A lower-case clause naming the cause, e.g. ``"the backend rejected the
        API key (401 Unauthorized) ..."``. The API key is never included. When
        neither a status code nor an error is known, a generic "rejected the
        batch" clause preserves today's behaviour rather than inventing detail.
    """
    if status_code is not None:
        if status_code == 401:
            return (
                "the backend rejected the API key (401 Unauthorized) -- the key "
                f"in {_API_KEY_ENV_VAR} or api_key is missing, incorrect, or "
                "revoked"
            )
        if status_code == 403:
            return (
                "the backend refused the API key (403 Forbidden) -- it was "
                "recognized but is not authorized for this endpoint or project"
            )
        if status_code == 404:
            return (
                "the backend returned 404 Not Found -- the endpoint URL or path "
                "is likely wrong"
            )
        if status_code in (400, 422):
            return (
                "the backend rejected the batch as malformed "
                f"(HTTP {status_code}) -- this usually means the endpoint or "
                "OTLP protocol version does not match"
            )
        if status_code == 413:
            return (
                "the backend rejected the batch as too large "
                "(413 Payload Too Large)"
            )
        if status_code == 429:
            return (
                "the backend is rate limiting the export "
                "(429 Too Many Requests)"
            )
        if 500 <= status_code < 600:
            return (
                f"the backend reported a server error (HTTP {status_code}) -- "
                "this is usually transient"
            )
        return f"the backend rejected the batch (HTTP {status_code})"
    if error is not None:
        return (
            "the backend could not be reached "
            f"({type(error).__name__}: {error})"
        )
    return "the backend rejected the batch"


def _is_permanent_status(status_code: int) -> bool:
    """Whether an HTTP status would fail identically if re-sent on a later flush.

    A client error (4xx) means the request itself is wrong -- a rejected key, a
    wrong endpoint, a malformed batch -- so POSTing the same spans to the same
    endpoint again can only fail the same way; those spans are dropped rather
    than retried. Two 4xx statuses are excluded because they can clear on their
    own between flushes: ``408 Request Timeout`` and ``429 Too Many Requests``.
    Everything else -- a ``5xx``, a connection error, or any status with no
    dedicated meaning -- is treated as transient and kept for a retry. See
    ``docs/design-notes.md`` ("Permanent rejections are not retried").

    Kept a module function, like :func:`_describe_failure`, so the rule is
    testable without the optional OTLP dependency.
    """
    return 400 <= status_code < 500 and status_code not in (408, 429)


def _build_transport(
    endpoint: str,
    headers: Mapping[str, str],
    timeout: float | None,
) -> SpanExporter:
    """Construct the underlying OpenTelemetry OTLP/HTTP exporter.

    This is the only place the optional dependency is touched, and a module
    function (rather than inline construction) so the test suite can substitute a
    fake transport and never needs the real extra installed. Omitting ``timeout``
    leaves the transport on its own default, which does read
    ``OTEL_EXPORTER_OTLP_TRACES_TIMEOUT``.

    The transport is a thin subclass that remembers the outcome of its last
    send, so a rejected batch can be described (a 401, an unreachable host)
    rather than reported as a bare failure -- OpenTelemetry's own exporter keeps
    none of that once ``export`` returns. See ``docs/design-notes.md``
    ("Delivery failures name their cause").

    Raises:
        ImportError: If the extra is not installed, re-raised with a message
            naming the exact ``pip install`` needed.
    """
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
    except ImportError as exc:
        raise ImportError(
            "The OTLP exporter requires the optional 'otlp' extra. Install it "
            "with: pip install 'argus-trace[otlp]'"
        ) from exc

    class _CapturingOTLPSpanExporter(OTLPSpanExporter):  # type: ignore[misc,valid-type]
        """OTLP transport that remembers why its last export failed.

        OpenTelemetry's exporter reports a rejected batch only as a failed
        return value; the HTTP status and any connection error are logged and
        then dropped. Overriding the private, per-attempt ``_export`` hook lets
        the outcome survive: ``last_status_code`` holds the status of the final
        attempt, ``last_error`` the exception a connection-level failure raised.
        :meth:`BufferedOTLPExporter._deliver` reads them to name the cause.

        The coupling is deliberately narrow. ``_export`` is delegated to
        verbatim through ``*args``, so a change to its signature passes straight
        through; and if a future release drops or renames it, the override simply
        never runs, the attributes stay ``None``, and the warning falls back to
        its generic form -- degraded, never broken.
        """

        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            self.last_status_code: int | None = None
            self.last_error: BaseException | None = None

        def _export(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            self.last_status_code = None
            self.last_error = None
            try:
                response = super()._export(*args, **kwargs)
            except Exception as exc:
                self.last_error = exc
                raise
            self.last_status_code = getattr(response, "status_code", None)
            return response

    kwargs: dict = {"endpoint": endpoint, "headers": dict(headers)}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return _CapturingOTLPSpanExporter(**kwargs)


class BufferedOTLPExporter(_DeferredExporter):
    """Buffer spans and POST them to a backend in one OTLP/HTTP request on exit.

    The remote counterpart to :class:`~argus.exporters.file.FileSpanExporter`:
    same buffer-now/emit-once lifecycle, same :meth:`emit` hook Argus drives on
    process exit. Because spans cannot be un-POSTed, accepted spans leave the
    buffer -- where the file sink keeps its own (see ``docs/design-notes.md``,
    "Repeat emits: rewrite or clear").

    Usually you don't construct this yourself: ``argus.init(otlp=True)`` or
    ``argus.init(otlp=OtlpConfig(...))`` builds it for you and runs it alongside
    the trace files. The name says "buffered" rather than mirroring
    OpenTelemetry's own ``OTLPSpanExporter``, which streams (see
    ``docs/design-notes.md``, "Names that don't shadow OpenTelemetry's").
    """

    def __init__(
        self,
        endpoint: str | None = None,
        *,
        api_key: str | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> None:
        """Prepare an exporter pointed at a backend ingest endpoint.

        The arguments are :class:`OtlpConfig`'s fields, which is what
        :func:`argus.init` passes through.

        Args:
            endpoint: Full URL to POST spans to. An explicit value is used
                verbatim -- no ``/v1/traces`` path is appended. When omitted,
                falls back to the standard OTel endpoint env vars, where the
                all-signals ``OTEL_EXPORTER_OTLP_ENDPOINT`` *does* get
                ``/v1/traces`` appended (see ``README.md``, "Remote export over
                OTLP", for the resolution order).
            api_key: Key authenticating the export, sent as
                ``Authorization: Bearer <key>``. Required; falls back to the
                ``AEGIS_API_KEY`` env var (see ``README.md``, "Authentication").
            headers: Extra HTTP headers sent alongside the credential (e.g.
                routing or tenant hints). The only way to add headers: the
                transport's ``OTEL_EXPORTER_OTLP_*_HEADERS`` env vars never take
                effect, since Argus always passes headers to carry the credential.
            timeout: Per-export timeout in seconds; falls back to the
                transport's own default when omitted.

        Raises:
            ValueError: If ``endpoint`` is blank, or is omitted with neither
                endpoint env var set, or if the API key is missing or unusable
                (a blank value, or one containing whitespace).
            ImportError: If the optional ``otlp`` extra is not installed.
        """
        super().__init__()
        self._endpoint = _resolve_endpoint(endpoint)
        # The credential lives inside the transport's headers and nowhere on
        # this exporter, so it cannot surface through a warning or a repr.
        self._transport = _build_transport(
            self._endpoint, _resolve_auth_headers(api_key, headers), timeout
        )

    def _deliver(self, spans: list[ReadableSpan], *, failed: bool) -> Delivery:
        """POST every buffered span to the endpoint in a single request.

        Args:
            spans: Every span buffered so far, sent as one batch.
            failed: Accepted for parity with the buffered-exporter contract. The
                backend reads a run's outcome from each span's own status rather
                than from a run-level flag, so it is not encoded separately.

        Returns:
            :attr:`Delivery.CONSUMED` once the backend confirms the batch, so
            accepted spans are never POSTed twice. On a failure the batch is
            warned about (never raised) and then either
            :attr:`Delivery.DISCARDED`, when the status is a permanent client
            error a retry could only repeat (see :func:`_is_permanent_status`),
            or :attr:`Delivery.RETAINED`, when it is transient -- a ``5xx``, a
            connection error, an unattributed failure -- and a later emit might
            still deliver it. See ``docs/design-notes.md`` ("Delivery failures
            warn, never raise", "Permanent rejections are not retried").

        Either way the warning names the cause -- a 401, an unreachable host --
        via :func:`_describe_failure`, reading the status code or error the
        transport kept from its last attempt (see ``docs/design-notes.md``,
        "Delivery failures name their cause").
        """
        try:
            result = self._transport.export(spans)
        except Exception as exc:
            # The real transport catches connection errors and reports them by
            # return value, but a caller's own or a future one may still raise.
            # A connection-level failure is transient, so the spans are kept.
            self._warn_not_delivered(len(spans), status_code=None, error=exc)
            return Delivery.RETAINED
        if result == SpanExportResult.SUCCESS:
            return Delivery.CONSUMED
        status_code = getattr(self._transport, "last_status_code", None)
        error = getattr(self._transport, "last_error", None)
        self._warn_not_delivered(
            len(spans), status_code=status_code, error=error
        )
        if status_code is not None and _is_permanent_status(status_code):
            return Delivery.DISCARDED
        return Delivery.RETAINED

    def _warn_not_delivered(
        self,
        count: int,
        *,
        status_code: int | None,
        error: BaseException | None,
    ) -> None:
        """Warn that ``count`` spans were not delivered, naming the cause.

        The cause is resolved by :func:`_describe_failure` from whichever of the
        status code or error is known; the API key never appears in either, so
        it cannot leak through the warning.
        """
        warnings.warn(
            f"Argus: {count} span(s) were not delivered to {self._endpoint!r}: "
            f"{_describe_failure(status_code, error)}.",
            RuntimeWarning,
            stacklevel=3,
        )

    def shutdown(self) -> None:
        """Release the transport's resources (e.g. its HTTP session)."""
        self._transport.shutdown()
