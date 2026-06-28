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

* An ``atexit`` hook (:func:`_flush_active_sessions`) flushes every session on
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
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Union

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter

from .detection import resolve_instrumentors
from .exporters.file import FileSpanExporter
from .paths import default_traces_dir

# Sessions opened in this process, flushed once on exit.
_active_sessions: "List[Session]" = []
# Flipped by our excepthook when the run dies with an unhandled exception, so
# the atexit flush can tag the trace as a failure.
_run_failed = False
_excepthook_installed = False


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
        instruments: Sequence[str],
        project: str,
    ) -> None:
        """Store the run's tracing state.

        Built by :func:`init`, not directly. Most arguments are what they say;
        the two worth noting are ``exporters`` (the sinks :meth:`flush` drives
        on exit) and ``instruments`` (instrumentor *names*, kept only for
        introspection). ``_flushed`` is the guard that makes :meth:`flush`
        idempotent.
        """
        self.provider = provider
        self.exporters = list(exporters)
        self.instruments = list(instruments)
        self.project = project
        self._flushed = False

    def flush(self, *, failed: Optional[bool] = None) -> None:
        """Write buffered traces to their exporters exactly once.

        ``failed`` overrides the auto-detected outcome; when omitted we use the
        process-wide flag set by our excepthook.
        """
        if self._flushed:
            return
        self._flushed = True
        is_failed = _run_failed if failed is None else failed
        for exporter in self.exporters:
            write_to_disk = getattr(exporter, "write_to_disk", None)
            if callable(write_to_disk):
                write_to_disk(failed=is_failed)

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
        load_dotenv: Load environment variables from a ``.env`` file found from
            the working directory.

    Returns:
        A :class:`Session`; traces flush automatically on process exit.
    """
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
    provider = TracerProvider(resource=resource)

    if exporters is None:
        exporters = [
            FileSpanExporter(base_dir, script_name=_detect_script_name())
        ]
    for exporter in exporters:
        provider.add_span_processor(SimpleSpanProcessor(exporter))

    instances = resolve_instrumentors(instrument)
    for instrumentor in instances:
        instrumentor.instrument(tracer_provider=provider)

    session = Session(
        provider=provider,
        exporters=exporters,
        instruments=[type(i).__name__ for i in instances],
        project=project,
    )
    _install_excepthook()
    _active_sessions.append(session)
    return session


@atexit.register
def _flush_active_sessions() -> None:
    """Flush every registered session on process exit.

    Registered with ``atexit`` so traces are persisted without the caller
    lifting a finger. Exceptions are swallowed per-session: a failure to write
    one trace must never crash interpreter shutdown or block the others.
    """
    for session in _active_sessions:
        try:
            session.flush()
        except Exception:
            # Never let trace flushing crash interpreter shutdown.
            pass
