"""Default filesystem locations for Argus.

A standalone SDK should not assume it lives inside any particular repository,
so the default output directory is anchored to the current working directory.
Pass ``output_dir`` to :func:`argus.init` to put traces somewhere else.
"""

from __future__ import annotations

from pathlib import Path


def default_traces_dir() -> Path:
    """Return the default base directory for traces (``<cwd>/traces``)."""
    return Path.cwd() / "traces"
