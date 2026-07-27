"""Decide which OpenInference instrumentor(s) to turn on.

Two strategies are supported: a curated registry mapping each agent framework to
the instrumentor(s) it needs, detected by whether the framework is in use
(default), and entry-point discovery of every registered instrumentor (opt-in via
``instrument="all"``). See ``docs/design-notes.md`` ("Curated detection over
entry points").

:class:`Instrumentor` is the contract everything here resolves to, and the one
Argus drives: the extension point for a framework it doesn't know about yet.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib import import_module
from importlib.util import find_spec
from typing import (
    Any,
    Iterable,
    Optional,
    Protocol,
    Sequence,
    Union,
    runtime_checkable,
)

from opentelemetry.trace import TracerProvider

# The entry-point group OpenInference instrumentors publish themselves under.
# It backs ``instrument="all"`` alone, which loads every instrumentor registered
# in the group without consulting a key; the curated registry below is a
# separate route that never reads it.
ENTRY_POINT_GROUP = "openinference_instrumentor"


@runtime_checkable
class Instrumentor(Protocol):
    """Something that can patch a framework to emit spans, and unpatch it again.

    OpenInference's instrumentors all derive from OpenTelemetry's
    ``BaseInstrumentor``, whose surface is far wider than this. These two methods
    are the whole of what Argus calls, and therefore the whole of what a
    substitute has to provide -- a fake in the test suite, or an instrumentor for
    a framework the curated registry doesn't cover yet. The protocol is
    structural: nothing has to inherit from it, or even know it exists. See
    ``docs/design-notes.md`` ("Extension points are protocols").
    """

    def instrument(
        self,
        *,
        tracer_provider: Optional[TracerProvider] = None,
        **kwargs: Any,
    ) -> None:
        """Patch the target framework to emit spans into ``tracer_provider``."""
        ...

    def uninstrument(self, **kwargs: Any) -> None:
        """Undo :meth:`instrument`, leaving the framework unpatched."""
        ...


@dataclass(frozen=True)
class _Framework:
    """A framework Argus knows how to instrument.

    ``detector`` is the importable module whose presence signals the framework
    is in play; ``instrumentors`` are ``"module:ClassName"`` paths to apply.
    """

    key: str
    detector: str
    instrumentors: tuple[str, ...]


# Registry order is the order frameworks are detected, and so the order their
# instrumentors are applied. It is not what keeps OpenAI calls from being
# instrumented twice -- _OPENAI_SUPERSEDERS below does that, and reordering
# these entries would not change it.
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

# Frameworks that make the standalone ``openai`` key redundant, so
# auto-detection drops it when one of them is present -- for two different
# reasons. The OpenAI Agents instrumentor covers those calls itself, so adding
# the standalone one on top really would instrument them twice. Agno's does not
# cover them, which is why its entry above already pairs AgnoInstrumentor with
# OpenAIInstrumentor: there the dropped key resolves to a class the selection
# holds either way, and ``resolve_instrumentors`` dedupes by class, so its
# membership here shapes the detected keys rather than the outcome.
_OPENAI_SUPERSEDERS = {"openai_agents", "agno"}


def _load(path: str) -> Any:
    """Import and return the attribute named by a ``"module:attr"`` path.

    The lazy-import seam: an instrumentor class is imported only once its
    framework is selected, so an unused optional dependency never has to be
    importable.
    """
    module_path, _, attr = path.partition(":")
    return getattr(import_module(module_path), attr)


def _module_loaded(name: str) -> bool:
    """Return whether ``name`` has already been imported in this process.

    A ``sys.modules`` membership check, so it never triggers an import.
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

    Falls back to importability when nothing relevant is loaded yet, and drops
    the standalone OpenAI key when a framework that supersedes it is present.
    See ``docs/design-notes.md`` ("Curated detection over entry points").
    """
    candidates = [fw.key for fw in _FRAMEWORKS if _module_loaded(fw.detector)]
    if not candidates:
        candidates = [
            fw.key for fw in _FRAMEWORKS if _module_available(fw.detector)
        ]
    if _OPENAI_SUPERSEDERS & set(candidates):
        candidates = [k for k in candidates if k != "openai"]
    return candidates


def _entry_point_classes() -> list[type[Instrumentor]]:
    """Load every instrumentor advertised under the entry-point group.

    Backs ``instrument="all"``. A broken or incompatible instrumentor is skipped
    rather than allowed to abort the run.
    """
    from importlib.metadata import entry_points

    classes: list[type[Instrumentor]] = []
    for entry_point in entry_points(group=ENTRY_POINT_GROUP):
        try:
            classes.append(entry_point.load())
        except Exception:
            # A broken/incompatible instrumentor shouldn't abort the run.
            continue
    return classes


def _classes_for_keys(keys: Iterable[str]) -> list[type[Instrumentor]]:
    """Resolve curated registry keys to their instrumentor classes.

    Raises:
        ValueError: If a key isn't in the curated registry, with the set of
            known keys to guide the fix.
    """
    classes: list[type[Instrumentor]] = []
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
) -> list[Instrumentor]:
    """Return instantiated instrumentors for the requested selection.

    * ``None`` / ``"curated"``  -> curated auto-detection (default)
    * ``"all"``                 -> entry-point discovery
    * ``str``                   -> a single registry key (e.g. ``"openai_agents"``)
    * ``Sequence[str]``         -> explicit list of registry keys

    ``None`` is accepted as a synonym for ``"curated"`` so the bare
    ``init(project)`` does the auto-detection.

    Raises:
        ValueError: If an explicitly requested key is not in the curated
            registry. The message lists the known keys, since a typo is the
            likely cause. Auto-detection cannot hit this: it selects the keys
            itself.
    """
    if instrument == "all":
        classes = _entry_point_classes()
    elif instrument is None or instrument == "curated":
        classes = _classes_for_keys(_auto_keys())
    elif isinstance(instrument, str):
        classes = _classes_for_keys([instrument])
    else:
        classes = _classes_for_keys(instrument)

    instances: list[Instrumentor] = []
    seen: set[type[Instrumentor]] = set()
    for cls in classes:
        if cls in seen:
            continue
        seen.add(cls)
        instances.append(cls())
    return instances
