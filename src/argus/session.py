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
doubles as a context manager -- and because a flush with nothing new to emit is
a no-op, using the context manager and the ``atexit`` hook together is
harmless. A flush is not a point of no return, though: spans produced after one
are emitted by the next, so code that keeps running past a ``with`` block still
gets its trace.

The session is a per-process singleton, which is what :func:`reset` is for: it
retires the active one so a notebook, a REPL, or a test can initialize again
with a different configuration instead of silently keeping the first.
"""

from __future__ import annotations

import atexit
import os
import sys
import warnings
from pathlib import Path
from typing import Optional, Sequence, Tuple, Union

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import (
    ReadableSpan,
    SpanLimits,
    SpanProcessor,
    TracerProvider,
)
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
# dropped with no error). The standard OpenTelemetry env vars remain the escape
# hatch for the rare caller who must tune it.
_DEFAULT_MAX_SPAN_ATTRIBUTES = 50_000
_SPAN_ATTRIBUTE_COUNT_ENV_VAR = "OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT"
# OpenTelemetry's generic attribute ceiling, which it uses as the *default* for
# the span-specific limit above. Since Argus always passes an explicit span
# attribute limit, that default never applies -- so this variable is read here
# too, or a ceiling an operator deliberately set would be silently shadowed.
_ATTRIBUTE_COUNT_ENV_VAR = "OTEL_ATTRIBUTE_COUNT_LIMIT"


def _attribute_cap_from_env(
    env_var: str, *, empty_means_unlimited: bool
) -> Tuple[bool, Optional[int]]:
    """Read an attribute-count ceiling from ``env_var``.

    Returns a ``(found, cap)`` pair: ``found`` says whether the variable
    supplied a usable value, and ``cap`` is the ceiling it asked for, where
    ``None`` means "no limit". An unset or malformed value is reported as not
    found, so the caller falls through to the next source.

    ``empty_means_unlimited`` mirrors OpenTelemetry's own split on a variable
    set to nothing: for the span-specific limit an empty value *is* how "no
    limit" is spelled, while an empty generic limit is indistinguishable from an
    unset one (OpenTelemetry reads both as "use the default").
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
    ``OTEL_ATTRIBUTE_COUNT_LIMIT``. Argus's raised default applies only when
    neither is usable. A malformed or negative span-specific value falls back
    rather than raising; a malformed generic one is OpenTelemetry's to reject,
    which :class:`SpanLimits` does when it reads that variable itself.

    Only the span attribute *count* is touched. Every other limit -- events,
    links, attribute lengths, and the generic ceiling's own effect on event and
    link attributes -- keeps whatever OpenTelemetry resolves for it.
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
        'argus.init(project, instrument=["openai_agents", "claude"]). To '
        "reconfigure instead -- re-running a notebook cell, say -- call "
        "argus.reset() first.",
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
    """Load the nearest ``.env`` at or above the working directory, if any.

    Run unconditionally by :func:`init`, which is what lets a key kept in
    ``.env`` satisfy ``AEGIS_API_KEY`` with no argument passed. There is no
    opt-out because there is nothing to opt out of: python-dotenv only ever
    *fills in* variables (it leaves anything already in the environment
    untouched, so a deployment's real configuration always wins), and a missing
    file is a no-op. Quietly does nothing when ``python-dotenv`` isn't
    installed, keeping the dependency genuinely optional.
    """
    try:
        from dotenv import find_dotenv, load_dotenv
    except ModuleNotFoundError:
        return
    # usecwd=True walks up from the working directory (where the script runs)
    # rather than from inside the installed argus package. The walk does mean a
    # script in a monorepo can reach a .env in a shared parent; harmless given
    # the no-override rule above, but it is why this is worth knowing about.
    load_dotenv(find_dotenv(usecwd=True))


class _SpanCounter(SpanProcessor):
    """Tallies ended spans so a :class:`Session` knows when it has new work.

    :meth:`Session.flush` must be safe to call repeatedly -- the context
    manager and the ``atexit`` hook routinely both fire -- but it must not
    treat "already flushed once" as "finished forever", or spans produced after
    an explicit flush would be buffered and never emitted. Comparing this tally
    against the one recorded at the last flush distinguishes the two cases: an
    unchanged count means there is genuinely nothing new to emit, while a higher
    one means more spans arrived and deserve a second emit.

    The sampling check mirrors :class:`SimpleSpanProcessor`, which drops
    unsampled spans before they ever reach an exporter; counting them here would
    otherwise signal new work that no exporter actually received.
    """

    def __init__(self) -> None:
        self.count = 0

    def on_end(self, span: ReadableSpan) -> None:
        if not (span.context and span.context.trace_flags.sampled):
            return
        self.count += 1


def _force_flush(exporter: SpanExporter) -> None:
    """Drain a stock OpenTelemetry exporter, one that has no ``emit`` hook.

    Argus's own exporters buffer everything and hand it over in a single
    ``emit(failed=...)`` call, but ``exporters=`` accepts any
    :class:`~opentelemetry.sdk.trace.export.SpanExporter`. Because Argus opts
    out of the provider's own ``atexit`` shutdown (see :func:`init`), nothing
    else in the pipeline would ever drain such an exporter, so one that batches
    internally would silently lose the run. Failures are swallowed: a sink that
    cannot flush must not take the host program down with it.
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
    """

    def __init__(
        self,
        provider: TracerProvider,
        exporters: Sequence[SpanExporter],
        instrumentors: Sequence[object],
        project: str,
        span_counter: Optional[_SpanCounter] = None,
    ) -> None:
        """Store the run's tracing state.

        Built by :func:`init`, not directly. Most arguments are what they say;
        the two worth noting are ``exporters`` (the sinks :meth:`flush` drives
        on exit) and ``instrumentors`` (the live instrumentor *instances*,
        retained so the session can be torn down via :func:`reset`).
        ``instruments`` exposes their class names for introspection.

        ``span_counter`` is the :class:`_SpanCounter` :func:`init` registered on
        the provider; together with ``_flushed_at_count`` it lets :meth:`flush`
        skip redundant work without going permanently inert (see
        :meth:`flush`). It defaults to an unused counter so a hand-built session
        still behaves, flushing on the first call and no-oping after.
        """
        self.provider = provider
        self.exporters = list(exporters)
        self.instrumentors = list(instrumentors)
        self.instruments = [type(i).__name__ for i in self.instrumentors]
        self.project = project
        self._span_counter = span_counter or _SpanCounter()
        self._flushed = False
        self._flushed_at_count = 0

    def flush(self, *, failed: Optional[bool] = None) -> None:
        """Emit every exporter's buffered traces.

        Argus's exporters buffer spans in memory and defer their real output
        (a JSON file, an OTLP POST) to an ``emit(failed=...)`` hook that this
        method drives. ``failed`` overrides the auto-detected outcome; when
        omitted we use the process-wide flag set by our excepthook. Any other
        :class:`~opentelemetry.sdk.trace.export.SpanExporter` passed via
        ``exporters=`` has no such hook -- it received its spans synchronously
        as they ended -- so it is drained with ``force_flush`` instead (see
        :func:`_force_flush`).

        Calling this repeatedly is safe *and* not permanently terminal. A
        repeat call with no new spans since the last one returns immediately,
        which is what makes the context manager and the ``atexit`` hook
        harmless together. But if spans were produced *after* a flush -- code
        that keeps running past a ``with argus.init(...)`` block -- the next
        flush emits again rather than discarding them.
        """
        if self._flushed and self._span_counter.count == self._flushed_at_count:
            return
        self._flushed = True
        self._flushed_at_count = self._span_counter.count
        is_failed = _run_failed if failed is None else failed
        for exporter in self.exporters:
            emit = getattr(exporter, "emit", None)
            if callable(emit):
                emit(failed=is_failed)
            else:
                _force_flush(exporter)

    def _shutdown_exporters(self) -> None:
        """Release every exporter's resources, once the run is truly over.

        Argus disables the provider's own ``atexit`` shutdown (see :func:`init`)
        because it would tear exporters down *before* the final flush. That
        leaves this: the last step of :func:`_flush_on_exit`, after everything
        has been emitted, so sockets and file handles are still closed. Each
        exporter is shut down independently and failures are swallowed, so one
        misbehaving sink neither skips the others nor crashes interpreter
        shutdown.
        """
        for exporter in self.exporters:
            try:
                exporter.shutdown()
            except Exception:
                pass

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
    api_key: Optional[str] = None,
) -> Session:
    """Configure tracing and turn on the right instrumentor(s).

    Args:
        project: Argus's logical run umbrella, stamped on every span as
            ``argus.project``. A project may span several services.
        service: Identity of the observed application, stamped as the
            OpenTelemetry ``service.name``. Defaults to the running script's
            name -- or ``"session"`` where there is no script file, as in a REPL
            or an embedded interpreter (see :func:`_detect_script_name`) -- so
            standard OTel backends group traces by the app that produced them
            rather than by Argus.
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
            ``force_flush`` on flush and ``shutdown`` at exit. An exporter that
            instead defines ``emit(failed=...)`` -- as both of Argus's own do --
            opts into the buffer-now/emit-once lifecycle and is handed the run's
            outcome (see :meth:`Session.flush`).
        otlp: Enable remote OTLP/HTTP export *alongside* the other exporters.
            The spans are buffered and POSTed once on exit (same lifecycle as
            the on-disk exporter), not streamed mid-run. A string sets the full
            endpoint URL explicitly; ``True`` reads it from the standard
            ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` or
            ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var, raising
            :class:`ValueError` when neither is set -- Argus ships no default
            endpoint (see :func:`~argus.exporters.otlp._resolve_endpoint`).
            ``None`` (the default) and ``False`` leave OTLP off; a blank string
            raises rather than joining them, since it reads as an endpoint that
            failed to resolve. For extra headers, a timeout, or full control,
            build the exporter with
            :func:`~argus.exporters.otlp.make_otlp_exporter` and pass it in
            ``exporters`` instead.
        api_key: Key authenticating the OTLP export, sent as
            ``Authorization: Bearer <key>``. Falls back to the ``AEGIS_API_KEY``
            env var (which is the recommended way to supply it -- an explicit
            argument invites committing a live secret), and is required whenever
            ``otlp`` is on. Applies only to the exporter ``otlp`` builds; a
            hand-built exporter takes its own ``api_key``.

    Returns:
        A :class:`Session`; traces flush automatically on process exit.

    Raises:
        ValueError: If ``otlp`` is a blank string, or is on with no endpoint or
            no API key resolvable.

    The nearest ``.env`` at or above the working directory is loaded before
    anything is resolved, so a key or endpoint kept there is picked up without
    being passed (see :func:`_load_dotenv`).

    Calling ``init`` more than once in a process is a no-op: it warns and
    returns the already-active :class:`Session` unchanged. Because
    instrumentors are global singletons a second provider could not reliably
    capture spans anyway, so to trace several frameworks pass them all in one
    call (e.g. ``instrument=["openai_agents", "claude"]``). To genuinely
    reconfigure -- re-running a notebook cell, say -- retire the first session
    with :func:`reset` and call ``init`` again.
    """
    global _session
    if _session is not None:
        _warn_reinit(_session, project)
        return _session

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
    # Registered before the exporters' processors so the session can tell
    # whether new spans have arrived since its last flush.
    span_counter = _SpanCounter()
    provider.add_span_processor(span_counter)

    if exporters is None:
        exporters = [
            FileSpanExporter(base_dir, script_name=_detect_script_name())
        ]
    else:
        exporters = list(exporters)
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
                "exporters=[FileSpanExporter(output_dir, script_name='run')].",
                RuntimeWarning,
                stacklevel=2,
            )
    # A string endpoint says "export remotely, here", so a blank one says
    # nothing at all -- and it is what os.getenv("ENDPOINT", "") yields when the
    # variable is missing. An empty string is falsy, so it would quietly turn
    # export off, leaving the api_key branch below to blame an endpoint the
    # caller believes they passed; a whitespace-only one is truthy and would
    # sail through to the transport as a nonsense URL. Name the real problem.
    if isinstance(otlp, str) and not otlp.strip():
        raise ValueError(
            "argus.init() was given an empty otlp endpoint. Pass otlp=None (or "
            "omit it) to leave remote export off, otlp=True to read the "
            "endpoint from the standard OTel environment variables, or a "
            "complete URL to set it here."
        )
    # ``otlp`` layers a remote sink on top of whatever the exporter list already
    # holds, so the on-disk JSON and the remote backend can run side by side. A
    # string is the endpoint; True defers to the OTEL_EXPORTER_OTLP_TRACES_
    # ENDPOINT env var (and errors if that is unset).
    if otlp:
        endpoint = otlp.strip() if isinstance(otlp, str) else None
        exporters.append(make_otlp_exporter(endpoint, api_key=api_key))
    elif api_key is not None:
        # A key with no remote sink to authenticate does nothing. Silence here
        # would read as "the key was accepted", so name the missing half.
        warnings.warn(
            "argus.init() was given an api_key but no otlp endpoint, so the "
            "key is unused and nothing is exported remotely. Pass otlp=True "
            "(or otlp='https://your-backend/...') to enable remote export.",
            RuntimeWarning,
            stacklevel=2,
        )
    # Every Argus exporter buffers spans and emits on exit, so SimpleSpanProcessor
    # (synchronous, no background queue that could drop under load) suits them
    # all; the actual send/write is deferred to each exporter's ``emit`` hook. A
    # caller's own exporter has no such hook, and this processor is why that
    # still works: it hands spans over as they end, so nothing is stranded in a
    # queue Argus doesn't own.
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
        span_counter=span_counter,
    )
    _install_excepthook()
    _session = session
    return session


def reset() -> None:
    """Retire the active session so the next :func:`init` takes effect.

    Argus is a per-process singleton, so a second :func:`init` warns and returns
    the first session unchanged. That is the right default for a script, but it
    also pins the first configuration in place for the life of the process --
    which is wrong in a notebook or REPL, where re-running the cell that calls
    ``init`` is the ordinary way to change a setting. Calling this first makes
    the next ``init`` a real initialization again::

        argus.reset()
        argus.init("my_project_name", otlp=True)

    Uninstruments the live instrumentors (so the next ``init`` can re-wire them
    to its own provider), drops the session, and clears the failure flag. The
    excepthook wrapper is left installed; it is idempotent and harmless to keep.

    It deliberately does *not* flush: retiring a session and emitting its traces
    are separate decisions. But the ``atexit`` hook only ever flushes the
    *active* session, so whatever the dropped one had buffered is gone. Flush
    first when you still want it::

        session.flush()
        argus.reset()
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
    """Flush the active session on process exit, then close its exporters.

    Registered with ``atexit`` so traces are persisted without the caller
    lifting a finger. Exceptions are swallowed: a failure to write the trace
    must never crash interpreter shutdown.

    The shutdown runs even if the flush failed -- resources are released either
    way -- and only here, never from :meth:`Session.flush`, because the run is
    genuinely over at this point. Flushing from a context manager mid-program
    must leave every exporter usable for the spans still to come.
    """
    if _session is None:
        return
    try:
        _session.flush()
    except Exception:
        # Never let trace flushing crash interpreter shutdown.
        pass
    _session._shutdown_exporters()
