"""Argus: a thin wrapper over OpenInference + OpenTelemetry.

Argus is the all-seeing companion to Aegis: it watches what your agents do and
records it. A single :func:`init` call detects the agent framework in use,
turns on the matching OpenInference instrumentor(s), and persists each run's
spans to disk as readable JSON.

Typical usage::

    import argus
    argus.init("my_project_name")  # auto-detects the framework, flushes on exit
"""

from importlib.metadata import PackageNotFoundError, version

from .blindspot import blindspot
from .session import Session, init

try:
    __version__ = version("argus-trace")
except PackageNotFoundError:
    # Running from a source tree without installed metadata.
    __version__ = "0.0.0+unknown"

__all__ = ["init", "Session", "blindspot"]
