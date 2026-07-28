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
import sys
import warnings
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Literal

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import (
    ReadableSpan,
    SpanProcessor,
    TracerProvider,
)
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter

from . import __version__ as argus_version
from .detection import (
    _BY_KEY,
    Instrumentor,
    InstrumentSelection,
    resolve_instrumentors,
)
from .exporters.base import BufferedSpanExporter
from .exporters.file import FileSpanExporter
from .exporters.otlp import BufferedOTLPExporter, OtlpConfig
from .limits import _resolve_span_limits
from .paths import default_traces_dir, detect_script_name

# The single session for this process; ``init`` enforces the singleton.
_session: Session | None = None
# Flipped by our excepthook when the run dies with an unhandled exception, so
# the atexit flush can tag the trace as a failure.
_run_failed = False
_excepthook_installed = False


def _warn_reinit(existing: Session, project: str) -> None:
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


def _warn_no_instrumentors() -> None:
    """Warn that detection found nothing to instrument.

    A bare ``argus.init(project)`` with no recognized framework otherwise
    installs no instrumentors, produces no spans, and writes no files -- the
    silent failure mode the rest of ``init`` refuses. Emitted as a
    :class:`RuntimeWarning` so ``python -W error`` can promote it; pass
    ``instrument=[]`` to opt out of instrumentation deliberately.
    """
    warnings.warn(
        "argus.init() detected no supported agent framework, so nothing was "
        "instrumented and this run will produce no spans. Known keys: "
        f"{sorted(_BY_KEY)}. Pass instrument= with one of those keys "
        "(or instrument=[] to silence this warning), or install a recognized "
        "framework.",
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
        exc_type: type[BaseException],
        exc: BaseException,
        tb: TracebackType | None,
    ) -> None:
        global _run_failed
        _run_failed = True
        previous(exc_type, exc, tb)

    sys.excepthook = hook
    _excepthook_installed = True


def _resolve_otlp_config(
    otlp: bool | OtlpConfig | None,
) -> OtlpConfig | None:
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


def _build_resource(project: str, service: str | None) -> Resource:
    """Build the attributes stamped on every span the run produces.

    ``service.name`` (OTel convention) identifies the observed app,
    ``argus.project`` is Argus's own grouping, and ``argus.version`` records the
    tool that produced the trace. Keeping them distinct lets standard backends
    group by app while Argus keys off a namespace nobody else touches.

    Args:
        project: Argus's logical run umbrella.
        service: Identity of the observed application; defaults to the running
            script's name (see :func:`~argus.paths.detect_script_name`).
    """
    return Resource.create(
        {
            "service.name": service or detect_script_name(),
            "argus.project": project,
            "argus.version": argus_version,
        }
    )


def _build_sinks(
    exporters: Sequence[SpanExporter] | None,
    output_dir: str | Path | None,
    otlp_config: OtlpConfig | None,
) -> list[SpanExporter]:
    """Decide which exporters the run writes through, in the order they see spans.

    Args:
        exporters: ``None`` for Argus's default file sink, or an explicit list
            replacing it. A given list is copied, so appending the remote sink
            below never mutates the caller's own.
        output_dir: Where the default file sink writes; ``None`` for
            ``<cwd>/traces``. It configures that sink alone, so passing it
            alongside ``exporters`` warns and is otherwise ignored.
        otlp_config: Remote export settings, or ``None`` to keep export local.

    Returns:
        The sinks to attach to the provider.
    """
    sinks: list[SpanExporter]
    if exporters is None:
        base_dir = (
            Path(output_dir) if output_dir is not None else default_traces_dir()
        )
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
                stacklevel=3,
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
    return sinks


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


class Session:
    """Handle for an initialized tracing session.

    Usually you don't touch this directly: ``argus.init(...)`` registers the
    session and an ``atexit`` hook flushes it on process exit. It also works as
    a context manager when you want deterministic, scoped flushing::

        with argus.init("my_project_name"):
            run_my_agent()

    Four read-only properties -- :attr:`provider`, :attr:`project`,
    :attr:`instruments` and :attr:`exporters` -- report what ``init``
    configured, for logging it or asserting on it. They are the whole supported
    surface alongside :meth:`flush` and the context manager; everything else,
    the constructor included, is Argus's own (see ``docs/design-notes.md``,
    "The session reports, it does not rewire").
    """

    def __init__(
        self,
        provider: TracerProvider,
        exporters: Sequence[SpanExporter],
        instrumentors: Sequence[Instrumentor],
        project: str,
        span_counter: _SpanCounter,
    ) -> None:
        """Store the run's tracing state.

        Built by :func:`init`, which is the only supported way to get a
        :class:`Session`: this signature takes Argus-internal state, so it is
        not part of the public API and may change. The class itself is exported
        so callers can annotate what ``init`` handed them.

        Args:
            provider: The configured :class:`TracerProvider` spans come from.
            exporters: The sinks :meth:`flush` drives.
            instrumentors: The live
                :class:`~argus.detection.Instrumentor` instances, retained so
                :func:`reset` can tear them down; :attr:`instruments` exposes
                their class names for introspection.
            project: Argus's logical run umbrella.
            span_counter: The :class:`_SpanCounter` :func:`init` registered on
                the provider, which lets :meth:`flush` tell "nothing new" from
                "finished".
        """
        self._provider = provider
        self._exporters: list[SpanExporter] = list(exporters)
        self._instrumentors: list[Instrumentor] = list(instrumentors)
        self._project = project
        self._span_counter = span_counter
        self._flushed = False
        self._flushed_at_count = 0
        self._reported_failures: set[tuple[int, str]] = set()

    @property
    def provider(self) -> TracerProvider:
        """The :class:`TracerProvider` spans come from.

        Handed out so callers can emit their own spans through
        ``provider.get_tracer(...)`` or point an instrumentor Argus does not
        know at it (see ``docs/examples.md``). Read-only because Argus's
        processors, this session's flush and the live instrumentors are all
        wired to *this* provider; a replacement would receive no spans.
        """
        return self._provider

    @property
    def project(self) -> str:
        """Argus's logical run umbrella, stamped on every span."""
        return self._project

    @property
    def exporters(self) -> tuple[SpanExporter, ...]:
        """The sinks :meth:`flush` drives, in the order they see spans.

        A tuple, and rebuilt on each access, so it answers "which sinks did
        ``init`` end up with?" without handing out the list Argus drives.
        """
        return tuple(self._exporters)

    @property
    def instruments(self) -> tuple[str, ...]:
        """Class names of the instrumentors ``init`` turned on.

        Derived on access rather than stored, so it cannot fall out of step
        with the instrumentors :func:`reset` tears down.
        """
        return tuple(type(i).__name__ for i in self._instrumentors)

    def flush(self, *, failed: bool | None = None) -> None:
        """Emit every exporter's buffered traces.

        An exporter implementing
        :class:`~argus.exporters.base.BufferedSpanExporter` is driven through
        ``emit(failed=...)``; any other is drained with ``force_flush``. Calling
        this repeatedly is safe and not terminal: a repeat call with no new spans
        returns immediately, while spans produced *after* a flush are emitted by
        the next one. See ``docs/design-notes.md`` ("A flush is not terminal",
        "Exporters Argus does not own").

        Args:
            failed: Overrides the auto-detected outcome; when omitted the
                process-wide flag set by our excepthook is used.
        """
        if self._flushed and self._span_counter.count == self._flushed_at_count:
            return
        self._flushed = True
        self._flushed_at_count = self._span_counter.count
        is_failed = _run_failed if failed is None else failed
        for exporter in self._exporters:
            if isinstance(exporter, BufferedSpanExporter):
                exporter.emit(failed=is_failed)
            else:
                # A stock exporter that cannot flush is reported and skipped: it
                # must neither take the host program down nor stop the sinks
                # after it from being drained.
                try:
                    exporter.force_flush()
                except Exception as exc:
                    self._report_swallowed("force_flush", exporter, exc)

    def _shutdown_exporters(self) -> None:
        """Release every exporter's resources, once the run is truly over.

        Driven only from :func:`_flush_on_exit`, after everything has been
        emitted, and never from :meth:`flush` -- which must leave every exporter
        usable for the spans still to come. Each exporter is shut down
        independently, and a failure is reported and swallowed, so one
        misbehaving sink neither skips the others nor crashes interpreter
        shutdown.
        """
        for exporter in self._exporters:
            try:
                exporter.shutdown()
            except Exception as exc:
                self._report_swallowed("shutdown", exporter, exc)

    def _report_swallowed(
        self, operation: str, target: object, exc: Exception
    ) -> None:
        """Warn about an error Argus deliberately ignored, once per target.

        A swallowed sink/instrumentor failure must not crash the host program,
        but staying silent would leave those extension points undebuggable, so it
        surfaces as a :class:`RuntimeWarning` (promotable with ``python -W
        error``). Repeats for the same target and operation are dropped -- the
        first report is the useful one. See ``docs/design-notes.md`` ("Swallowed
        errors are still audible").

        Args:
            operation: The method that raised, named as its author knows it.
            target: The exporter or instrumentor whose call failed.
            exc: What that call raised.
        """
        key = (id(target), operation)
        if key in self._reported_failures:
            return
        self._reported_failures.add(key)
        warnings.warn(
            f"Argus: {type(target).__name__}.{operation}() raised and was "
            "ignored, so it could not crash the run "
            f"({type(exc).__name__}: {exc}). Repeat failures from this object "
            "are not reported again.",
            RuntimeWarning,
            stacklevel=3,
        )

    def __enter__(self) -> Session:
        """Enter the context manager, returning the session itself."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
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
    service: str | None = None,
    instrument: InstrumentSelection = None,
    output_dir: str | Path | None = None,
    exporters: Sequence[SpanExporter] | None = None,
    otlp: bool | OtlpConfig | None = None,
) -> Session:
    """Configure tracing and turn on the right instrumentor(s).

    The nearest ``.env`` at or above the working directory is loaded before
    anything is resolved (see :func:`_load_dotenv`). Calling ``init`` more than
    once in a process warns and returns the already-active :class:`Session`
    unchanged; call :func:`reset` first to genuinely reconfigure. Auto-detection
    (or ``instrument="all"``) finding no instrumentors likewise warns rather
    than silently tracing nothing; pass ``instrument=[]`` to opt out
    deliberately. See ``docs/design-notes.md`` ("One session per process",
    "Curated detection over entry points").

    Args:
        project: Argus's logical run umbrella, stamped on every span as
            ``argus.project``. A project may span several services.
        service: Identity of the observed application, stamped as the
            OpenTelemetry ``service.name``. Defaults to the running script's
            name, falling back to ``"session"`` for a REPL, an embedded
            interpreter, or piped stdin (see
            :func:`~argus.paths.detect_script_name`).
        instrument: ``None``/``"curated"`` for curated auto-detection
            (default), ``"all"`` for entry-point discovery, or a key / list of
            keys (e.g. ``"openai_agents"``, ``["agno"]``). Typed as
            :data:`~argus.detection.InstrumentSelection`, so an editor completes
            the keys and a type checker rejects a misspelled one here rather
            than at run time.
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
    """
    global _session
    if _session is not None:
        _warn_reinit(_session, project)
        return _session

    # Checked before anything is created, so a bad argument costs no side effects.
    otlp_config = _resolve_otlp_config(otlp)

    _load_dotenv()

    # shutdown_on_exit=False is load-bearing: the provider's own atexit handler
    # would otherwise run before Argus's flush (atexit is LIFO) and tear the
    # exporters down before they emit. Argus drives the lifecycle itself and
    # closes them in _flush_on_exit. See ``docs/design-notes.md`` ("Opting out
    # of the provider's atexit shutdown").
    provider = TracerProvider(
        resource=_build_resource(project, service),
        span_limits=_resolve_span_limits(),
        shutdown_on_exit=False,
    )
    # Registered before the exporters' processors so the session can tell
    # whether new spans have arrived since its last flush.
    span_counter = _SpanCounter()
    provider.add_span_processor(span_counter)

    sinks = _build_sinks(exporters, output_dir, otlp_config)
    # SimpleSpanProcessor is synchronous, with no background queue that could
    # drop spans under load, and it hands spans to an exporter that has no
    # ``emit`` hook as they end, so nothing is stranded in a queue Argus doesn't
    # own.
    for exporter in sinks:
        provider.add_span_processor(SimpleSpanProcessor(exporter))

    instances = resolve_instrumentors(instrument)
    # Auto-detection (and entry-point discovery) finding nothing is the silent
    # failure mode a bare init otherwise falls into: no instrumentors, no spans,
    # no files. An explicit instrument=[] is the documented opt-out and stays
    # quiet. See ``docs/design-notes.md`` ("Curated detection over entry points").
    if not instances and (
        instrument is None or instrument in ("curated", "all")
    ):
        _warn_no_instrumentors()
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
        for instrumentor in _session._instrumentors:
            try:
                instrumentor.uninstrument()
            except Exception as exc:
                # A failed teardown -- or an object that turned out not to have
                # the method at all -- must not block the reset, but it is
                # named rather than hidden: the next init will patch a framework
                # this one never unpatched.
                _session._report_swallowed("uninstrument", instrumentor, exc)
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
        # The one place a swallowed error is *not* warned about (see
        # ``docs/design-notes.md``, "Swallowed errors are still audible"): a
        # warning from the atexit hook, mid interpreter shutdown, is not
        # something a caller can rely on seeing, and letting this escape would
        # crash shutdown. The exporters are still closed below regardless.
        pass
    _session._shutdown_exporters()
