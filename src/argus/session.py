"""The Argus front door: :func:`init` and the :class:`Session` it returns.

:func:`init` wires up an OpenTelemetry :class:`TracerProvider`, attaches the
exporter(s), turns on the auto-detected instrumentor(s), and hands back a
:class:`Session` that owns the run's tracing state and knows how to flush it.
An ``atexit`` hook flushes that session on process exit and an ``excepthook``
wrapper records whether the run crashed, so a single ``init`` line is enough to
get a complete, correctly-labelled trace. :class:`Session` also doubles as a
context manager for scoped flushing, and :func:`reset` retires the per-process
singleton so a notebook or REPL can initialize again.

See ``docs/design-notes.md`` ("Zero-ceremony capture", "One session per
process", "A flush is not terminal", "Exporters Argus does not own", "Opting out
of the provider's atexit shutdown") for why it is built this way.
"""

from __future__ import annotations

import atexit
import os
import sys
import warnings
from pathlib import Path
from types import TracebackType
from typing import List, Literal, Optional, Sequence, Tuple, Type, Union

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import (
    ReadableSpan,
    SpanLimits,
    SpanProcessor,
    TracerProvider,
)
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter

from .detection import Instrumentor, resolve_instrumentors
from .exporters.base import BufferedSpanExporter
from .exporters.file import FileSpanExporter
from .exporters.otlp import BufferedOTLPExporter, OtlpConfig
from .paths import default_traces_dir, detect_script_name

# OpenTelemetry caps span attributes at 128, low enough that a long agent
# conversation silently loses the model's final output. Argus raises the ceiling
# far past any realistic run, deliberately without an ``init`` argument, leaving
# the standard OTel env vars as the escape hatch. See ``docs/design-notes.md``
# ("The raised span-attribute ceiling").
_DEFAULT_MAX_SPAN_ATTRIBUTES = 50_000
_SPAN_ATTRIBUTE_COUNT_ENV_VAR = "OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT"
_ATTRIBUTE_COUNT_ENV_VAR = "OTEL_ATTRIBUTE_COUNT_LIMIT"


def _attribute_cap_from_env(
    env_var: str, *, empty_means_unlimited: bool
) -> Tuple[bool, Optional[int]]:
    """Read an attribute-count ceiling from ``env_var``.

    Args:
        env_var: Name of the environment variable to read.
        empty_means_unlimited: How to read a variable set to nothing, mirroring
            OpenTelemetry's own split between the two variables.

    Returns:
        A ``(found, cap)`` pair: ``found`` says whether the variable supplied a
        usable value, and ``cap`` is the ceiling it asked for, where ``None``
        means "no limit". An unset or malformed value is reported as not found,
        so the caller falls through to the next source.
    """
    raw = os.environ.get(env_var)
    if raw is None:
        return False, None
    raw = raw.strip()
    if raw == "":
        return empty_means_unlimited, None
    try:
        cap = int(raw)
    except ValueError:
        return False, None
    if cap < 0:
        return False, None
    return True, cap


def _resolve_span_limits() -> SpanLimits:
    """Build span limits that raise the attribute ceiling past OTel's default.

    The standard environment variables win, in OpenTelemetry's own precedence:
    ``OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT`` first, then the generic
    ``OTEL_ATTRIBUTE_COUNT_LIMIT``; Argus's raised default applies only when
    neither is usable. Only the span attribute *count* is touched -- every other
    limit keeps whatever OpenTelemetry resolves for it. See
    ``docs/design-notes.md`` ("The raised span-attribute ceiling").
    """
    found, cap = _attribute_cap_from_env(
        _SPAN_ATTRIBUTE_COUNT_ENV_VAR, empty_means_unlimited=True
    )
    if not found:
        found, cap = _attribute_cap_from_env(
            _ATTRIBUTE_COUNT_ENV_VAR, empty_means_unlimited=False
        )
    if not found:
        cap = _DEFAULT_MAX_SPAN_ATTRIBUTES
    return SpanLimits(
        max_span_attributes=SpanLimits.UNSET if cap is None else cap
    )


# The single session for this process; ``init`` enforces the singleton.
_session: "Optional[Session]" = None
# Flipped by our excepthook when the run dies with an unhandled exception, so
# the atexit flush can tag the trace as a failure.
_run_failed = False
_excepthook_installed = False


def _warn_reinit(existing: "Session", project: str) -> None:
    """Warn that Argus is already initialized and this call is being ignored.

    Emitted as a :class:`RuntimeWarning` rather than raised, so a stray second
    ``init`` never crashes the host program; ``python -W error`` promotes it for
    callers who want it strict.
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
        'argus.init(project, instrument=["openai_agents", "claude"]). To '
        "reconfigure instead -- re-running a notebook cell, say -- call "
        "argus.reset() first.",
        RuntimeWarning,
        stacklevel=3,
    )


def _install_excepthook() -> None:
    """Wrap ``sys.excepthook`` so we learn whether the run failed.

    Idempotent, and what lets the default ``atexit`` flush distinguish success
    from failure without forcing callers to use a context manager.
    """
    global _excepthook_installed
    if _excepthook_installed:
        return
    previous = sys.excepthook

    def hook(
        exc_type: Type[BaseException],
        exc: BaseException,
        tb: Optional[TracebackType],
    ) -> None:
        global _run_failed
        _run_failed = True
        previous(exc_type, exc, tb)

    sys.excepthook = hook
    _excepthook_installed = True


def _resolve_otlp_config(
    otlp: Union[bool, OtlpConfig, None],
) -> Optional[OtlpConfig]:
    """Normalize :func:`init`'s ``otlp`` argument to a config, or ``None`` for off.

    Args:
        otlp: ``None``/``False`` to leave remote export off, ``True`` to enable it
            with everything read from the environment, or an explicit
            :class:`~argus.exporters.otlp.OtlpConfig`.

    Raises:
        TypeError: If ``otlp`` is anything else. A string -- the endpoint form
            Argus took before the settings moved into ``OtlpConfig`` -- gets a
            message naming its replacement.
    """
    if otlp is None or otlp is False:
        return None
    if otlp is True:
        return OtlpConfig()
    if isinstance(otlp, OtlpConfig):
        return otlp
    if isinstance(otlp, str):
        raise TypeError(
            "argus.init() no longer takes an otlp endpoint string. Pass "
            f"otlp=OtlpConfig({otlp!r}) instead -- it carries the endpoint, "
            "api_key, headers and timeout together -- or otlp=True to read the "
            "endpoint from the standard OTel environment variables."
        )
    raise TypeError(
        "argus.init() takes otlp=True, otlp=OtlpConfig(...) or otlp=None; got "
        f"{type(otlp).__name__}."
    )


def _load_dotenv() -> None:
    """Load the nearest ``.env`` at or above the working directory, if any.

    Run unconditionally by :func:`init`, which is what lets a key kept in
    ``.env`` satisfy ``AEGIS_API_KEY`` with no argument passed. Values already in
    the environment are never overridden, and a missing file -- or a missing
    ``python-dotenv`` -- is a no-op. See ``docs/design-notes.md`` ("Loading .env
    unconditionally").
    """
    try:
        from dotenv import find_dotenv, load_dotenv
    except ModuleNotFoundError:
        return
    # usecwd=True walks up from the working directory (where the script runs)
    # rather than from inside the installed argus package.
    load_dotenv(find_dotenv(usecwd=True))


class _SpanCounter(SpanProcessor):
    """Tallies ended spans so a :class:`Session` knows when it has new work.

    Comparing this tally against the one recorded at the last flush is what lets
    :meth:`Session.flush` skip redundant work without going permanently inert.
    The sampling check mirrors :class:`SimpleSpanProcessor`, which drops
    unsampled spans before they reach an exporter. See ``docs/design-notes.md``
    ("A flush is not terminal").
    """

    def __init__(self) -> None:
        self.count = 0

    def on_end(self, span: ReadableSpan) -> None:
        if not (span.context and span.context.trace_flags.sampled):
            return
        self.count += 1


def _force_flush(exporter: SpanExporter) -> None:
    """Drain a stock OpenTelemetry exporter, one that has no ``emit`` hook.

    Failures are swallowed: a sink that cannot flush must not take the host
    program down with it. See ``docs/design-notes.md`` ("Exporters Argus does not
    own").
    """
    try:
        exporter.force_flush()
    except Exception:
        pass


class Session:
    """Handle for an initialized tracing session.

    Usually you don't touch this directly: ``argus.init(...)`` registers the
    session and an ``atexit`` hook flushes it on process exit. It also works as
    a context manager when you want deterministic, scoped flushing::

        with argus.init("my_project_name"):
            run_my_agent()

    Attributes:
        provider: The :class:`TracerProvider` spans come from.
        exporters: The sinks :meth:`flush` drives.
        instrumentors: The live :class:`~argus.detection.Instrumentor` instances.
        instruments: Their class names -- what ``init`` ended up patching.
        project: Argus's logical run umbrella.

    Those five are readable, for asserting or logging what ``init`` configured.
    Argus drives them on flush, exit and :func:`reset`, so replacing or mutating
    one is not supported.
    """

    def __init__(
        self,
        provider: TracerProvider,
        exporters: Sequence[SpanExporter],
        instrumentors: Sequence[Instrumentor],
        project: str,
        span_counter: Optional[_SpanCounter] = None,
    ) -> None:
        """Store the run's tracing state.

        Built by :func:`init`, not directly.

        Args:
            provider: The configured :class:`TracerProvider` spans come from.
            exporters: The sinks :meth:`flush` drives.
            instrumentors: The live
                :class:`~argus.detection.Instrumentor` instances, retained so
                :func:`reset` can tear them down; ``instruments`` exposes their
                class names for introspection.
            project: Argus's logical run umbrella.
            span_counter: The :class:`_SpanCounter` :func:`init` registered on
                the provider, which lets :meth:`flush` tell "nothing new" from
                "finished". Defaults to an unused counter, so a hand-built
                session still flushes on the first call and no-ops after.
        """
        self.provider = provider
        self.exporters: List[SpanExporter] = list(exporters)
        self.instrumentors: List[Instrumentor] = list(instrumentors)
        self.instruments = [type(i).__name__ for i in self.instrumentors]
        self.project = project
        self._span_counter = span_counter or _SpanCounter()
        self._flushed = False
        self._flushed_at_count = 0

    def flush(self, *, failed: Optional[bool] = None) -> None:
        """Emit every exporter's buffered traces.

        Args:
            failed: Overrides the auto-detected outcome; when omitted the
                process-wide flag set by our excepthook is used.

        An exporter implementing
        :class:`~argus.exporters.base.BufferedSpanExporter` is driven through
        ``emit(failed=...)``; any other is drained with ``force_flush``. Calling
        this repeatedly is safe and not terminal: a repeat call with no new spans
        returns immediately, while spans produced *after* a flush are emitted by
        the next one. See ``docs/design-notes.md`` ("A flush is not terminal",
        "Exporters Argus does not own").
        """
        if self._flushed and self._span_counter.count == self._flushed_at_count:
            return
        self._flushed = True
        self._flushed_at_count = self._span_counter.count
        is_failed = _run_failed if failed is None else failed
        for exporter in self.exporters:
            if isinstance(exporter, BufferedSpanExporter):
                exporter.emit(failed=is_failed)
            else:
                _force_flush(exporter)

    def _shutdown_exporters(self) -> None:
        """Release every exporter's resources, once the run is truly over.

        Driven only from :func:`_flush_on_exit`, after everything has been
        emitted, and never from :meth:`flush` -- which must leave every exporter
        usable for the spans still to come. Each exporter is shut down
        independently and failures are swallowed, so one misbehaving sink neither
        skips the others nor crashes interpreter shutdown.
        """
        for exporter in self.exporters:
            try:
                exporter.shutdown()
            except Exception:
                pass

    def __enter__(self) -> "Session":
        """Enter the context manager, returning the session itself."""
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> Literal[False]:
        """Flush on scope exit, tagging failure if the block raised.

        Returns ``False`` -- typed as :data:`~typing.Literal` so a type checker
        knows it is *always* false -- so any exception from the ``with`` block
        propagates normally rather than being swallowed.
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
    otlp: Union[bool, OtlpConfig, None] = None,
) -> Session:
    """Configure tracing and turn on the right instrumentor(s).

    Args:
        project: Argus's logical run umbrella, stamped on every span as
            ``argus.project``. A project may span several services.
        service: Identity of the observed application, stamped as the
            OpenTelemetry ``service.name``. Defaults to the running script's
            name, or ``"session"`` where there is no script file (see
            :func:`~argus.paths.detect_script_name`).
        instrument: ``None``/``"curated"`` for curated auto-detection
            (default), ``"all"`` for entry-point discovery, or a key / list of
            keys (e.g. ``"openai_agents"``, ``["agno"]``).
        output_dir: Directory the default file exporter writes traces to.
            Defaults to ``<cwd>/traces``. It configures *that* exporter only,
            so passing it together with ``exporters`` (which replaces the
            default) has no effect and warns.
        exporters: Custom span exporters, replacing the default. Defaults to a
            single :class:`FileSpanExporter` writing each trace to disk as both
            canonical OTLP/JSON and a human-readable rendering. Any
            :class:`~opentelemetry.sdk.trace.export.SpanExporter` is accepted:
            spans reach it synchronously as they end, and Argus drains it with
            ``force_flush`` on flush and ``shutdown`` at exit. One that
            implements :class:`~argus.exporters.base.BufferedSpanExporter` --
            as both of Argus's own do -- opts into the buffer-now/emit-once
            lifecycle and is handed the run's outcome (see
            :meth:`Session.flush`).
        otlp: Enable remote OTLP/HTTP export *alongside* the other exporters,
            buffered and POSTed once on exit. ``True`` takes the endpoint and API
            key from the environment; an
            :class:`~argus.exporters.otlp.OtlpConfig` sets any of the endpoint,
            key, extra headers and timeout explicitly. ``None`` (the default) and
            ``False`` leave remote export off. Every remote setting lives in that
            config, so nothing here can be passed without an endpoint to use it
            (see ``docs/design-notes.md``, "Remote export is one argument").

    Returns:
        A :class:`Session`; traces flush automatically on process exit.

    Raises:
        TypeError: If ``otlp`` is neither a bool, an
            :class:`~argus.exporters.otlp.OtlpConfig`, nor ``None``.
        ValueError: If ``instrument`` names a key the curated registry does not
            know (see :func:`~argus.detection.resolve_instrumentors`), or if
            remote export is on with a blank or unresolvable endpoint, or with
            no API key resolvable.
        ImportError: If remote export is on but the optional ``otlp`` extra is
            not installed. Like the endpoint and key, this surfaces here rather
            than at exit with the run's whole trace already buffered (see
            ``docs/design-notes.md``, "No default OTLP endpoint").

    The nearest ``.env`` at or above the working directory is loaded before
    anything is resolved (see :func:`_load_dotenv`). Calling ``init`` more than
    once in a process warns and returns the already-active :class:`Session`
    unchanged; call :func:`reset` first to genuinely reconfigure. See
    ``docs/design-notes.md`` ("One session per process").
    """
    global _session
    if _session is not None:
        _warn_reinit(_session, project)
        return _session

    # Checked before anything is created, so a bad argument costs no side effects.
    otlp_config = _resolve_otlp_config(otlp)

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
            "service.name": service or detect_script_name(),
            "argus.project": project,
            "argus.version": argus_version,
        }
    )
    # shutdown_on_exit=False is load-bearing: the provider's own atexit handler
    # would otherwise run before Argus's flush (atexit is LIFO) and tear the
    # exporters down before they emit. Argus drives the lifecycle itself and
    # closes them in _flush_on_exit. See ``docs/design-notes.md`` ("Opting out
    # of the provider's atexit shutdown").
    provider = TracerProvider(
        resource=resource,
        span_limits=_resolve_span_limits(),
        shutdown_on_exit=False,
    )
    # Registered before the exporters' processors so the session can tell
    # whether new spans have arrived since its last flush.
    span_counter = _SpanCounter()
    provider.add_span_processor(span_counter)

    sinks: List[SpanExporter]
    if exporters is None:
        sinks = [FileSpanExporter(base_dir)]
    else:
        sinks = list(exporters)
        if output_dir is not None:
            # output_dir only ever configured the default file exporter, which
            # an explicit list replaces. Applying it is impossible (the given
            # exporters are already constructed), so name the no-op rather than
            # let a caller believe their traces are going somewhere they aren't.
            warnings.warn(
                "argus.init() was given both output_dir and an explicit "
                "exporters list, so output_dir has no effect: it only sets "
                "where the default file exporter writes, and exporters "
                "replaces that exporter. Pass the directory to the exporter "
                "itself instead, e.g. "
                "exporters=[FileSpanExporter(output_dir)].",
                RuntimeWarning,
                stacklevel=2,
            )
    # The remote sink layers on top of whatever the exporter list already holds,
    # so the on-disk JSON and the remote backend run side by side.
    if otlp_config is not None:
        sinks.append(
            BufferedOTLPExporter(
                otlp_config.endpoint,
                api_key=otlp_config.api_key,
                headers=otlp_config.headers,
                timeout=otlp_config.timeout,
            )
        )
    # SimpleSpanProcessor is synchronous, with no background queue that could
    # drop spans under load, and it hands spans to an exporter that has no
    # ``emit`` hook as they end, so nothing is stranded in a queue Argus doesn't
    # own.
    for exporter in sinks:
        provider.add_span_processor(SimpleSpanProcessor(exporter))

    instances = resolve_instrumentors(instrument)
    for instrumentor in instances:
        instrumentor.instrument(tracer_provider=provider)

    session = Session(
        provider=provider,
        exporters=sinks,
        instrumentors=instances,
        project=project,
        span_counter=span_counter,
    )
    _install_excepthook()
    _session = session
    return session


def reset() -> None:
    """Retire the active session so the next :func:`init` takes effect.

    Argus is a per-process singleton, so a second :func:`init` warns and returns
    the first session unchanged. Calling this first makes the next ``init`` a
    real initialization again, which is what a notebook or REPL needs::

        argus.reset()
        argus.init("my_project_name", otlp=True)

    Uninstruments the live instrumentors, drops the session, and clears the
    failure flag; the idempotent excepthook wrapper is left installed. It
    deliberately does *not* flush, and the ``atexit`` hook only flushes the
    *active* session, so flush first when you still want what the retired one
    buffered::

        session.flush()
        argus.reset()

    See ``docs/design-notes.md`` ("One session per process").
    """
    global _session, _run_failed
    if _session is not None:
        for instrumentor in _session.instrumentors:
            try:
                instrumentor.uninstrument()
            except Exception:
                # A failed teardown -- or an object that turned out not to have
                # the method at all -- must not block the reset.
                pass
    _session = None
    _run_failed = False


@atexit.register
def _flush_on_exit() -> None:
    """Flush the active session on process exit, then close its exporters.

    Exceptions are swallowed: a failure to write the trace must never crash
    interpreter shutdown. The shutdown runs even if the flush failed --
    resources are released either way -- and only here, because the run is
    genuinely over at this point.
    """
    if _session is None:
        return
    try:
        _session.flush()
    except Exception:
        # Never let trace flushing crash interpreter shutdown.
        pass
    _session._shutdown_exporters()
