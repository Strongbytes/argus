"""The Argus front door: :func:`init` and the :class:`Session` it returns."""

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
    main = sys.modules.get("__main__")
    path = getattr(main, "__file__", None)
    return Path(path).stem if path else "session"


def _load_dotenv() -> None:
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

        with argus.init("openai"):
            run_my_agent()
    """

    def __init__(
        self,
        provider: TracerProvider,
        exporters: Sequence[SpanExporter],
        instruments: Sequence[str],
        project: str,
    ) -> None:
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
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.flush(failed=exc_type is not None)
        return False


def init(
    project: str,
    *,
    instrument: Union[str, Sequence[str], None] = None,
    output_dir: Union[str, Path, None] = None,
    exporters: Optional[Sequence[SpanExporter]] = None,
    load_dotenv: bool = True,
) -> Session:
    """Configure tracing and turn on the right instrumentor(s).

    Args:
        project: Logical name for the run; used as the traces sub-directory and
            stamped onto every span as ``service.name``.
        instrument: ``None`` for curated auto-detection (default), ``"auto"``
            for entry-point discovery, or a key / list of keys
            (e.g. ``"openai_agents"``, ``["agno"]``).
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

    base_dir = Path(output_dir) if output_dir is not None else default_traces_dir()
    resource = Resource.create(
        {"service.name": project, "argus.project": project}
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
    for session in _active_sessions:
        try:
            session.flush()
        except Exception:
            # Never let trace flushing crash interpreter shutdown.
            pass
