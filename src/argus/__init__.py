"""Argus: a thin wrapper over OpenInference + OpenTelemetry.

Argus is the all-seeing companion to Aegis: it watches what your agents do and
records it. A single :func:`init` call detects the agent framework in use, turns
on the matching OpenInference instrumentor(s), and persists each run's spans to
disk as both canonical OTLP/JSON and a human-readable rendering -- or ships them
to a remote backend over OTLP/HTTP with ``otlp=True``::

    import argus
    argus.init("my_project_name")  # auto-detects the framework, flushes on exit

The public surface is small: :func:`init` and the :class:`Session` it returns,
:class:`blindspot` to keep a scope off the record, and :func:`reset` to retire
the session so a notebook or REPL can initialize again. The two things you hand
to ``init`` are here too: :class:`OtlpConfig` for remote export and
:class:`FileSpanExporter` for naming the default sink explicitly. See
``README.md`` for the guide, ``docs/examples.md`` for worked examples of each use
case, and ``docs/design-notes.md`` for the reasoning behind the design.
"""

from importlib.metadata import PackageNotFoundError, version

from .blindspot import blindspot
from .exporters.file import FileSpanExporter
from .exporters.otlp import OtlpConfig
from .session import Session, init, reset

try:
    __version__ = version("argus-trace")
except PackageNotFoundError:
    # Running from a source tree without installed metadata.
    __version__ = "0.0.0+unknown"

__all__ = [
    "FileSpanExporter",
    "OtlpConfig",
    "Session",
    "blindspot",
    "init",
    "reset",
]
