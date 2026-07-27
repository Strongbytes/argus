"""Argus: a thin wrapper over OpenInference + OpenTelemetry.

Argus is the all-seeing companion to Aegis: it watches what your agents do and
records it. A single :func:`init` call detects the agent framework in use,
turns on the matching OpenInference instrumentor(s), and persists each run's
spans to disk as both canonical OTLP/JSON and a human-readable rendering.

Typical usage::

    import argus
    argus.init("my_project_name")  # auto-detects the framework, flushes on exit

The same call can ship those spans to a remote backend over OTLP/HTTP, alongside
the files, once the ``argus-trace[otlp]`` extra is installed::

    argus.init("my_project_name", otlp=True)  # endpoint + key from the env

The public surface is small: :func:`init` and the :class:`Session` it returns,
:class:`blindspot` to keep a scope off the record, and :func:`reset` to retire
the session so a notebook or REPL can initialize again.
"""

from importlib.metadata import PackageNotFoundError, version

from .blindspot import blindspot
from .session import Session, init, reset

try:
    __version__ = version("argus-trace")
except PackageNotFoundError:
    # Running from a source tree without installed metadata.
    __version__ = "0.0.0+unknown"

__all__ = ["init", "Session", "blindspot", "reset"]
