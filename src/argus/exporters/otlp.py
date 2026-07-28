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
            standard OTel endpoint env vars (see :func:`_resolve_endpoint`).
        api_key: Key authenticating the export, sent as
            ``Authorization: Bearer <key>``. Omit it to read ``AEGIS_API_KEY``
            (see :func:`_resolve_auth_headers`).
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

    kwargs: dict = {"endpoint": endpoint, "headers": dict(headers)}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return OTLPSpanExporter(**kwargs)


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
            endpoint: Full URL to POST spans to, used verbatim -- no
                ``/v1/traces`` path is appended. When omitted, falls back to the
                standard OTel endpoint env vars (see :func:`_resolve_endpoint`).
            api_key: Key authenticating the export, sent as
                ``Authorization: Bearer <key>``. Required; falls back to the
                ``AEGIS_API_KEY`` env var (see :func:`_resolve_auth_headers`).
            headers: Extra HTTP headers sent alongside the credential (e.g.
                routing or tenant hints). The only way to add headers: the
                transport's ``OTEL_EXPORTER_OTLP_*_HEADERS`` env vars never take
                effect, since Argus always passes headers to carry the credential.
            timeout: Per-export timeout in seconds; falls back to the
                transport's own default when omitted.

        Raises:
            ValueError: If ``endpoint`` is blank, or is omitted with neither
                endpoint env var set (see :func:`_resolve_endpoint`), or if the
                API key is missing or unusable (see
                :func:`_resolve_auth_headers`).
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
            accepted spans are never POSTed twice; :attr:`Delivery.RETAINED`
            after a failure, which warns rather than raising and leaves the spans
            for a later emit to retry. See ``docs/design-notes.md`` ("Delivery
            failures warn, never raise").
        """
        try:
            result = self._transport.export(spans)
        except Exception as exc:
            warnings.warn(
                f"Argus: failed to export {len(spans)} span(s) to "
                f"{self._endpoint!r}; they were not delivered "
                f"({type(exc).__name__}: {exc}).",
                RuntimeWarning,
                stacklevel=2,
            )
            return Delivery.RETAINED
        if result == SpanExportResult.SUCCESS:
            return Delivery.CONSUMED
        warnings.warn(
            f"Argus: the backend rejected the export of {len(spans)} span(s) "
            f"to {self._endpoint!r}; they were not delivered.",
            RuntimeWarning,
            stacklevel=2,
        )
        return Delivery.RETAINED

    def shutdown(self) -> None:
        """Release the transport's resources (e.g. its HTTP session)."""
        self._transport.shutdown()
