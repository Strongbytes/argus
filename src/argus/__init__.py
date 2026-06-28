"""Argus: a thin wrapper over OpenInference + OpenTelemetry.

Argus is the all-seeing companion to Aegis: it watches what your agents do and
records it. A single :func:`init` call detects the agent framework in use,
turns on the matching OpenInference instrumentor(s), and persists each run's
spans to disk as readable JSON.

Typical usage::

    import argus
    argus.init("openai")  # auto-detects the framework, flushes on exit
"""

from .session import Session, init

__all__ = ["init", "Session"]
__version__ = "0.1.0"
