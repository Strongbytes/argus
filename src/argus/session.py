"""The Argus front door: :func:`init` and the :class:`Session` it returns.

Everything a user does with Argus flows through here. :func:`init` is the one
call they make: it wires up an OpenTelemetry :class:`TracerProvider`, attaches
the exporter(s), turns on the auto-detected instrumentor(s), and hands back a
:class:`Session`. The :class:`Session` owns the run's tracing state and knows
how to flush it.

The central design goal is **zero-ceremony capture**: a user should be able to
add a single ``argus.init(...)`` line and get a complete, correctly-labelled
trace on disk -- even if they never call anything else and even if their script
crashes. Two module-level mechanisms make that possible:

* An ``atexit`` hook (:func:`_flush_on_exit`) flushes the active session on
  process exit, so the common case needs no context manager and no explicit
  flush call.
* An ``excepthook`` wrapper (:func:`_install_excepthook`) records whether the
  run died from an unhandled exception. That flag is what lets the on-exit
  flush tag a crashed run as failed without the user opting in.

For callers who want deterministic, scoped flushing instead, :class:`Session`
doubles as a context manager -- and because flushing is guarded to run exactly
once, using the context manager and the ``atexit`` hook together is harmless.
"""

from __future__ import annotations

import atexit
import os
import sys
import warnings
from pathlib import Path
from typing import Optional, Sequence, Union

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter

from .detection import resolve_instrumentors
from .exporters.file import FileSpanExporter
from .exporters.otlp import make_otlp_exporter
from .paths import default_traces_dir

# OpenTelemetry caps span attributes at 128 by default. OpenInference flattens
# every chat message into several attributes (role, content, each tool call's
# id/name/arguments, ...), so a long agent conversation blows past 128 and the
# SDK silently evicts the oldest attributes -- which, given the order
# OpenInference writes them, includes the model's final output message. We raise
# the ceiling far past any realistic run while keeping a rail against a
# pathological one. At roughly three attributes per chat message this holds on
# the order of ten-thousand messages in a single span.
#
# This is deliberately not exposed as an ``init`` argument: choosing it well
# requires knowing OpenInference's per-message flattening, and a too-low value
# fails silently (the oldest attributes, including the model's output, are
# dropped with no error). The standard OpenTelemetry env var remains the escape
# hatch for the rare caller who must tune it.
_DEFAULT_MAX_SPAN_ATTRIBUTES = 50_000
_SPAN_ATTRIBUTE_COUNT_ENV_VAR = "OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT"


def _resolve_span_limits() -> SpanLimits:
    """Build span limits that raise the attribute ceiling past OTel's default.

    Honors the standard ``OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT`` when set (an empty
    value means "no limit", matching OpenTelemetry), and otherwise applies
    Argus's raised default. Only the span attribute *count* is touched; every
    other limit keeps its OpenTelemetry default.
    """
    raw = os.environ.get(_SPAN_ATTRIBUTE_COUNT_ENV_VAR)
    if raw is None:
        cap: Optional[int] = _DEFAULT_MAX_SPAN_ATTRIBUTES
    else:
        raw = raw.strip()
        try:
            # Empty string is OTel's spelling of "unlimited"; a non-negative
            # int is an explicit cap. Anything else falls back to our default.
            cap = None if raw == "" else int(raw)
        except ValueError:
            cap = _DEFAULT_MAX_SPAN_ATTRIBUTES
        if cap is not None and cap < 0:
            cap = _DEFAULT_MAX_SPAN_ATTRIBUTES
    return SpanLimits(
        max_span_attributes=SpanLimits.UNSET if cap is None else cap
    )


# The single session for this process. Argus is intentionally a per-process
# singleton: instrumentors are global, so a second provider can never reliably
# receive spans for an already-instrumented framework. ``init`` enforces this.
_session: "Optional[Session]" = None
# Flipped by our excepthook when the run dies with an unhandled exception, so
# the atexit flush can tag the trace as a failure.
_run_failed = False
_excepthook_installed = False


def _warn_reinit(existing: "Session", project: str) -> None:
    """Warn that Argus is already initialized and this call is being ignored.

    Emitted as a :class:`RuntimeWarning` rather than raised so a stray second
    ``init`` never crashes the host program -- the same forgiving stance
    OpenTelemetry's own ``set_tracer_provider`` takes. Callers who *want* the
    strict, fail-fast behavior can promote it with ``python -W error``.
    """
    if project != existing.project:
        detail = (
            f" (already initialized for project {existing.project!r}; "
            f"ignoring new project {project!r})"
        )
    else:
        detail = f" (already initialized for project {existing.project!r})"
    warnings.warn(
        "argus.init() has already been called"
        + detail
        + "; returning the existing session and ignoring this call. To trace "
        "multiple frameworks, list them in a single init, e.g. "
        'argus.init(project, instrument=["openai_agents", "claude"]).',
        RuntimeWarning,
        stacklevel=3,
    )


def _install_excepthook() -> None:
    """Wrap ``sys.excepthook`` so we learn whether the run failed.

    This is what lets the default ``atexit`` flush still distinguish success
    from failure without forcing callers to use a context manager.
    """
    global _excepthook_installed
    if _excepthook_installed:
        return
    previous = sys.excepthook

    def hook(exc_type, exc, tb):
        global _run_failed
        _run_failed = True
        previous(exc_type, exc, tb)

    sys.excepthook = hook
    _excepthook_installed = True


def _detect_script_name() -> str:
    """Best-effort name for the running script, used to label trace files.

    Derived from the ``__main__`` module's filename (e.g. ``my_agent.py`` ->
    ``my_agent``). Falls back to ``"session"`` when there is no file to read,
    such as an interactive REPL or an embedded interpreter.
    """
    main = sys.modules.get("__main__")
    path = getattr(main, "__file__", None)
    return Path(path).stem if path else "session"


def _load_dotenv() -> None:
    """Load a ``.env`` from the working directory, if python-dotenv is present.

    Quietly does nothing when ``python-dotenv`` isn't installed, keeping the
    dependency genuinely optional.
    """
    try:
        from dotenv import find_dotenv, load_dotenv
    except ModuleNotFoundError:
        return
    # usecwd=True walks up from the working directory (where the script runs)
    # rather than from inside the installed argus package.
    load_dotenv(find_dotenv(usecwd=True))


class Session:
    """Handle for an initialized tracing session.

    Usually you don't touch this directly: ``argus.init(...)`` registers the
    session and an ``atexit`` hook flushes it on process exit. It also works as
    a context manager when you want deterministic, scoped flushing::

        with argus.init("my_project_name"):
            run_my_agent()
    """

    def __init__(
        self,
        provider: TracerProvider,
        exporters: Sequence[SpanExporter],
        instrumentors: Sequence[object],
        project: str,
    ) -> None:
        """Store the run's tracing state.

        Built by :func:`init`, not directly. Most arguments are what they say;
        the two worth noting are ``exporters`` (the sinks :meth:`flush` drives
        on exit) and ``instrumentors`` (the live instrumentor *instances*,
        retained so the session can be torn down via :func:`_reset`).
        ``instruments`` exposes their class names for introspection, and
        ``_flushed`` is the guard that makes :meth:`flush` idempotent.
        """
        self.provider = provider
        self.exporters = list(exporters)
        self.instrumentors = list(instrumentors)
        self.instruments = [type(i).__name__ for i in self.instrumentors]
        self.project = project
        self._flushed = False

    def flush(self, *, failed: Optional[bool] = None) -> None:
        """Emit every buffered exporter's traces exactly once.

        Argus's exporters buffer spans in memory and defer their real output
        (a JSON file, an OTLP POST) to an ``emit(failed=...)`` hook that this
        method drives on exit. ``failed`` overrides the auto-detected outcome;
        when omitted we use the process-wide flag set by our excepthook.
        """
        if self._flushed:
            return
        self._flushed = True
        is_failed = _run_failed if failed is None else failed
        for exporter in self.exporters:
            emit = getattr(exporter, "emit", None)
            if callable(emit):
                emit(failed=is_failed)

    def __enter__(self) -> "Session":
        """Enter the context manager, returning the session itself."""
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        """Flush on scope exit, tagging failure if the block raised.

        Returns ``False`` so any exception from the ``with`` block propagates
        normally rather than being swallowed.
        """
        self.flush(failed=exc_type is not None)
        return False


def init(
    project: str,
    *,
    service: Optional[str] = None,
    instrument: Union[str, Sequence[str], None] = None,
    output_dir: Union[str, Path, None] = None,
    exporters: Optional[Sequence[SpanExporter]] = None,
    otlp: Union[bool, str, None] = None,
    load_dotenv: bool = True,
) -> Session:
    """Configure tracing and turn on the right instrumentor(s).

    Args:
        project: Argus's logical run umbrella, stamped on every span as
            ``argus.project``. A project may span several services.
        service: Identity of the observed application, stamped as the
            OpenTelemetry ``service.name``. Defaults to the running script's
            name, so standard OTel backends group traces by the app that
            produced them rather than by Argus.
        instrument: ``None``/``"curated"`` for curated auto-detection
            (default), ``"all"`` for entry-point discovery, or a key / list of
            keys (e.g. ``"openai_agents"``, ``["agno"]``).
        output_dir: Directory traces are written to. Defaults to
            ``<cwd>/traces``.
        exporters: Custom span exporters. Defaults to a single
            :class:`FileSpanExporter` writing readable JSON.
        otlp: Enable remote OTLP/HTTP export *alongside* the other exporters.
            The spans are buffered and POSTed once on exit (same lifecycle as
            the on-disk exporter), not streamed mid-run. ``True`` reads the
            endpoint from the ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` env var,
            raising :class:`ValueError` if it is unset -- Argus ships no default
            endpoint; a string sets the full endpoint URL explicitly. Falsy
            values (the default) leave OTLP off. For headers/timeout or full
            control, build the exporter with
            :func:`~argus.exporters.otlp.make_otlp_exporter` and pass it in
            ``exporters`` instead.
        load_dotenv: Load environment variables from a ``.env`` file found from
            the working directory.

    Returns:
        A :class:`Session`; traces flush automatically on process exit.

    Calling ``init`` more than once in a process is a no-op: it warns and
    returns the already-active :class:`Session` unchanged. Because
    instrumentors are global singletons a second provider could not reliably
    capture spans anyway, so to trace several frameworks pass them all in one
    call (e.g. ``instrument=["openai_agents", "claude"]``).
    """
    global _session
    if _session is not None:
        _warn_reinit(_session, project)
        return _session

    if load_dotenv:
        _load_dotenv()

    base_dir = (
        Path(output_dir) if output_dir is not None else default_traces_dir()
    )
    # service.name (OTel convention) identifies the observed app; argus.project
    # is Argus's own grouping; argus.version records the tool that produced the
    # trace. Keeping them distinct lets standard backends group by app while
    # Argus keys off a namespace nobody else touches.
    from . import __version__ as argus_version

    resource = Resource.create(
        {
            "service.name": service or _detect_script_name(),
            "argus.project": project,
            "argus.version": argus_version,
        }
    )
    # shutdown_on_exit=False is load-bearing, not a tidiness choice. A
    # TracerProvider otherwise registers its own atexit handler in its
    # constructor, and atexit runs handlers LIFO. Argus's _flush_on_exit is
    # registered at *import* time -- before any provider exists -- so the
    # provider's shutdown, registered here at init time, would run *first* on
    # exit. That shutdown cascades into each exporter's shutdown(), tearing down
    # the OTLP transport's HTTP session, so Argus's later flush would then call
    # emit() on an already-dead transport (OTel logs "Exporter already shutdown,
    # ignoring batch" and returns FAILURE -- the backend is never even
    # contacted). Argus drives the whole buffer-now/emit-once lifecycle itself
    # via _flush_on_exit, so we opt out of the provider's competing handler.
    provider = TracerProvider(
        resource=resource,
        span_limits=_resolve_span_limits(),
        shutdown_on_exit=False,
    )

    if exporters is None:
        exporters = [
            FileSpanExporter(base_dir, script_name=_detect_script_name())
        ]
    else:
        exporters = list(exporters)
    # ``otlp`` layers a remote sink on top of whatever the exporter list already
    # holds, so the on-disk JSON and the remote backend can run side by side. A
    # string is the endpoint; True defers to the OTEL_EXPORTER_OTLP_TRACES_
    # ENDPOINT env var (and errors if that is unset).
    if otlp:
        endpoint = otlp if isinstance(otlp, str) else None
        exporters.append(make_otlp_exporter(endpoint))
    # Every Argus exporter buffers spans and emits on exit, so SimpleSpanProcessor
    # (synchronous, no background queue that could drop under load) suits them
    # all; the actual send/write is deferred to each exporter's ``emit`` hook.
    for exporter in exporters:
        provider.add_span_processor(SimpleSpanProcessor(exporter))

    instances = resolve_instrumentors(instrument)
    for instrumentor in instances:
        instrumentor.instrument(tracer_provider=provider)

    session = Session(
        provider=provider,
        exporters=exporters,
        instrumentors=instances,
        project=project,
    )
    _install_excepthook()
    _session = session
    return session


def _reset() -> None:
    """Tear down the active session so a fresh :func:`init` can run.

    Intended for tests and interactive (REPL/notebook) re-runs, where the
    per-process singleton would otherwise pin the first configuration in place.
    Uninstruments the live instrumentors (so the next ``init`` can re-wire them
    to a new provider), drops the session, and clears the failure flag.

    It deliberately does *not* flush -- call :meth:`Session.flush` first if you
    still want the buffered traces. The excepthook wrapper is left installed; it
    is idempotent and harmless to keep.
    """
    global _session, _run_failed
    if _session is not None:
        for instrumentor in _session.instrumentors:
            uninstrument = getattr(instrumentor, "uninstrument", None)
            if callable(uninstrument):
                try:
                    uninstrument()
                except Exception:
                    # A failed teardown must not block the reset.
                    pass
    _session = None
    _run_failed = False


@atexit.register
def _flush_on_exit() -> None:
    """Flush the active session on process exit.

    Registered with ``atexit`` so traces are persisted without the caller
    lifting a finger. Exceptions are swallowed: a failure to write the trace
    must never crash interpreter shutdown.
    """
    if _session is None:
        return
    try:
        _session.flush()
    except Exception:
        # Never let trace flushing crash interpreter shutdown.
        pass
