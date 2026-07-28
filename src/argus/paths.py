"""Where traces go by default, and what they are named after.

A standalone SDK should not assume it lives inside any particular repository,
so the default output directory is anchored to the current working directory.
Pass ``output_dir`` to :func:`argus.init` to put traces somewhere else.

Both defaults live here rather than in :mod:`argus.session` so that
:class:`~argus.exporters.file.FileSpanExporter` can apply them itself when it is
constructed directly, exactly as ``init`` would.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# The name is embedded verbatim into every trace filename, so it has to be a
# portable filename component: word characters, dot, and hyphen only. The name
# that fails this in practice is ``<stdin>`` -- what ``__main__.__file__`` holds
# under piped input (``python - < script.py`` or a heredoc, both common in CI)
# -- whose angle brackets are invalid on Windows and awkward in any shell.
_SAFE_SCRIPT_NAME = re.compile(r"\A[\w.-]+\Z")

# Used when there is no script file to read, or its name is not usable as one.
_FALLBACK_SCRIPT_NAME = "session"


def default_traces_dir() -> Path:
    """Return the default base directory for traces (``<cwd>/traces``)."""
    return Path.cwd() / "traces"


def detect_script_name() -> str:
    """Best-effort name for the running script, used to label trace files.

    Derived from the ``__main__`` module's filename (e.g. ``my_agent.py`` ->
    ``my_agent``). Falls back to ``"session"`` when there is no file to read (an
    interactive REPL or an embedded interpreter) or when the stem is not a
    portable filename component -- most notably ``<stdin>`` from piped input,
    whose angle brackets are invalid on Windows. A usable stem is taken as-is.
    """
    main = sys.modules.get("__main__")
    path = getattr(main, "__file__", None)
    if not path:
        return _FALLBACK_SCRIPT_NAME
    stem = Path(path).stem
    if not _SAFE_SCRIPT_NAME.match(stem):
        return _FALLBACK_SCRIPT_NAME
    return stem
