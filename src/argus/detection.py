"""Decide which OpenInference instrumentor(s) to turn on.

Two strategies are supported:

* **Curated registry (default).** A small table maps an agent framework to the
  instrumentor(s) it needs, detected by whether the framework is actually in
  use. This is predictable and avoids double-instrumenting (e.g. it won't add
  the standalone OpenAI instrumentor on top of the OpenAI Agents one).
* **Entry-point discovery (opt-in via ``instrument="all"``).** Loads every
  instrumentor registered under the ``openinference_instrumentor`` entry-point
  group. New instrumentors light up with no code change, at the cost of
  possibly instrumenting more than intended in a multi-framework environment.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib import import_module
from importlib.util import find_spec
from typing import Iterable, Sequence, Union

# Keys for the bundled openinference instrumentation packages, matched against
# their published ``openinference_instrumentor`` entry-point names.
ENTRY_POINT_GROUP = "openinference_instrumentor"


@dataclass(frozen=True)
class _Framework:
    """A framework Argus knows how to instrument.

    ``detector`` is the importable module whose presence signals the framework
    is in play; ``instrumentors`` are ``"module:ClassName"`` paths to apply.
    """

    key: str
    detector: str
    instrumentors: tuple[str, ...]


# Order matters: higher-level agent frameworks take precedence over the bare
# OpenAI client so we don't instrument the same calls twice.
_FRAMEWORKS: tuple[_Framework, ...] = (
    _Framework(
        "openai_agents",
        "agents",
        (
            "openinference.instrumentation.openai_agents:OpenAIAgentsInstrumentor",
        ),
    ),
    _Framework(
        "claude",
        "claude_agent_sdk",
        (
            "openinference.instrumentation.claude_agent_sdk:ClaudeAgentSDKInstrumentor",
        ),
    ),
    _Framework(
        "agno",
        "agno",
        (
            "openinference.instrumentation.agno:AgnoInstrumentor",
            "openinference.instrumentation.openai:OpenAIInstrumentor",
        ),
    ),
    _Framework(
        "openai",
        "openai",
        ("openinference.instrumentation.openai:OpenAIInstrumentor",),
    ),
)
_BY_KEY = {fw.key: fw for fw in _FRAMEWORKS}

# Frameworks that already cover OpenAI calls, so the standalone OpenAI
# instrumentor should be dropped from auto-detection when one is present.
_OPENAI_SUPERSEDERS = {"openai_agents", "agno"}


def _load(path: str):
    """Import and return the attribute named by a ``"module:attr"`` path.

    This is the lazy-import seam: instrumentor classes are only imported when a
    framework is actually selected, so an unused optional dependency never has
    to be importable.
    """
    module_path, _, attr = path.partition(":")
    return getattr(import_module(module_path), attr)


def _module_loaded(name: str) -> bool:
    """Return whether ``name`` has already been imported in this process.

    A cheap ``sys.modules`` membership check -- it never triggers an import,
    so it reflects what the current script chose to bring in.
    """
    return name in sys.modules


def _module_available(name: str) -> bool:
    """Return whether ``name`` is importable, without importing it.

    Used as the fallback signal when nothing relevant is loaded yet. The broad
    ``except`` covers oddly-packaged modules whose spec lookup itself raises.
    """
    try:
        return find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _auto_keys() -> list[str]:
    """Detect frameworks in use, preferring what's already imported.

    Using ``sys.modules`` first means that in a shared environment with several
    SDKs installed we instrument only the framework the current script actually
    imported, rather than everything that happens to be installed.
    """
    candidates = [fw.key for fw in _FRAMEWORKS if _module_loaded(fw.detector)]
    if not candidates:
        candidates = [
            fw.key for fw in _FRAMEWORKS if _module_available(fw.detector)
        ]
    if _OPENAI_SUPERSEDERS & set(candidates):
        candidates = [k for k in candidates if k != "openai"]
    return candidates


def _entry_point_classes() -> list[type]:
    """Load every instrumentor advertised under the entry-point group.

    Backs ``instrument="all"``: any package that registers itself lights up
    with no code change here. A broken or incompatible instrumentor is skipped
    rather than allowed to abort the run.
    """
    from importlib.metadata import entry_points

    classes: list[type] = []
    for entry_point in entry_points(group=ENTRY_POINT_GROUP):
        try:
            classes.append(entry_point.load())
        except Exception:
            # A broken/incompatible instrumentor shouldn't abort the run.
            continue
    return classes


def _classes_for_keys(keys: Iterable[str]) -> list[type]:
    """Resolve curated registry keys to their instrumentor classes.

    Raises:
        ValueError: If a key isn't in the curated registry, with the set of
            known keys to guide the fix.
    """
    classes: list[type] = []
    for key in keys:
        framework = _BY_KEY.get(key)
        if framework is None:
            raise ValueError(
                f"Unknown instrument key: {key!r}. "
                f"Known keys: {sorted(_BY_KEY)}"
            )
        for path in framework.instrumentors:
            classes.append(_load(path))
    return classes


def resolve_instrumentors(
    instrument: Union[str, Sequence[str], None],
) -> list:
    """Return instantiated instrumentors for the requested selection.

    * ``None`` / ``"curated"``  -> curated auto-detection (default)
    * ``"all"``                 -> entry-point discovery
    * ``str``                   -> a single registry key (e.g. ``"openai_agents"``)
    * ``Sequence[str]``         -> explicit list of registry keys

    ``None`` is accepted as a synonym for ``"curated"`` so the bare
    ``init(project)`` does the auto-detection.
    """
    if instrument == "all":
        classes = _entry_point_classes()
    elif instrument is None or instrument == "curated":
        classes = _classes_for_keys(_auto_keys())
    elif isinstance(instrument, str):
        classes = _classes_for_keys([instrument])
    else:
        classes = _classes_for_keys(instrument)

    instances = []
    seen: set[type] = set()
    for cls in classes:
        if cls in seen:
            continue
        seen.add(cls)
        instances.append(cls())
    return instances
