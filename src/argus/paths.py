"""Where traces go by default, and what they are named after.

A standalone SDK should not assume it lives inside any particular repository,
so the default output directory is anchored to the current working directory.
Pass ``output_dir`` to :func:`argus.init` to put traces somewhere else.

Both defaults live here rather than in :mod:`argus.session` so that
:class:`~argus.exporters.file.FileSpanExporter` can apply them itself when it is
constructed directly, exactly as ``init`` would.
"""

from __future__ import annotations

import sys
from pathlib import Path


def default_traces_dir() -> Path:
    """Return the default base directory for traces (``<cwd>/traces``)."""
    return Path.cwd() / "traces"


def detect_script_name() -> str:
    """Best-effort name for the running script, used to label trace files.

    Derived from the ``__main__`` module's filename (e.g. ``my_agent.py`` ->
    ``my_agent``). Falls back to ``"session"`` when there is no file to read,
    such as an interactive REPL or an embedded interpreter.
    """
    main = sys.modules.get("__main__")
    path = getattr(main, "__file__", None)
    return Path(path).stem if path else "session"
