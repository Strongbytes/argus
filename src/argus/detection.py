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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from importlib import import_module
from importlib.util import find_spec
from typing import Any, Literal, Protocol, runtime_checkable

from opentelemetry.trace import TracerProvider

# The entry-point group OpenInference instrumentors publish themselves under.
# It backs ``instrument="all"`` alone, which loads every instrumentor registered
# in the group without consulting a key; the curated registry below is a
# separate route that never reads it.
_ENTRY_POINT_GROUP = "openinference_instrumentor"


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
        tracer_provider: TracerProvider | None = None,
        **kwargs: Any,
    ) -> None:
        """Patch the target framework to emit spans into ``tracer_provider``."""
        ...

    def uninstrument(self, **kwargs: Any) -> None:
        """Undo :meth:`instrument`, leaving the framework unpatched."""
        ...


# ``instrument=``'s whole vocabulary, spelled as literals so an editor can
# complete it and a type checker can reject a typo at the call site instead of
# leaving it to the runtime ValueError in ``_classes_for_keys``. These are the
# source of truth for the names: ``_Framework.key`` below is annotated with
# ``InstrumentKey``, so a registry entry naming anything else is a type error,
# and the test suite pins the other direction -- every name here has an entry.

#: A curated registry key, naming one framework Argus knows how to instrument.
InstrumentKey = Literal["openai_agents", "claude", "agno", "openai"]

#: A strategy for choosing keys, rather than a key itself.
InstrumentStrategy = Literal["curated", "all"]

#: Everything :func:`argus.init`'s ``instrument=`` accepts.
InstrumentSelection = (
    InstrumentKey | InstrumentStrategy | Sequence[InstrumentKey] | None
)


@dataclass(frozen=True)
class _Framework:
    """A framework Argus knows how to instrument.

    ``detector`` is the importable module whose presence signals the framework
    is in play; ``instrumentors`` are ``"module:ClassName"`` paths to apply; and
    ``supersedes`` names the keys auto-detection drops when this framework is
    detected, which is what stops a narrower key from doubling up coverage this
    one already provides. Everything about a framework is therefore one entry --
    adding another needs no edit anywhere else in this module.
    """

    key: InstrumentKey
    detector: str
    instrumentors: tuple[str, ...]
    supersedes: tuple[InstrumentKey, ...] = ()


# Registry order is the order frameworks are detected, and so the order their
# instrumentors are applied. It is not what keeps OpenAI calls from being
# instrumented twice -- each entry's ``supersedes`` does that, and reordering
# these entries would not change it.
_FRAMEWORKS: tuple[_Framework, ...] = (
    _Framework(
        "openai_agents",
        "agents",
        (
            "openinference.instrumentation.openai_agents:OpenAIAgentsInstrumentor",
        ),
        # Load-bearing: this instrumentor covers OpenAI client calls itself, so
        # keeping the standalone key alongside it really would instrument them
        # twice.
        supersedes=("openai",),
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
        # Cosmetic, unlike the entry above: Agno's instrumentor does not cover
        # OpenAI calls, which is why the pair here includes OpenAIInstrumentor
        # already -- so dropping the standalone key changes the detected keys
        # and not the classes resolved from them. Kept so the selection does not
        # read as the double instrumentation that dedupe quietly prevents.
        supersedes=("openai",),
    ),
    _Framework(
        "openai",
        "openai",
        ("openinference.instrumentation.openai:OpenAIInstrumentor",),
    ),
)
# Keyed by plain ``str`` on purpose: lookups validate keys that arrive at
# runtime -- read from a config file, say -- which a ``dict[InstrumentKey, ...]``
# would reject at the type level while the check still had to happen anyway.
_BY_KEY: dict[str, _Framework] = {fw.key: fw for fw in _FRAMEWORKS}


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


def _auto_keys() -> list[InstrumentKey]:
    """Detect frameworks in use, preferring what's already imported.

    Falls back to importability when nothing relevant is loaded yet, then drops
    every key a detected framework supersedes (see :class:`_Framework`), so a
    framework's own coverage is never doubled up by a narrower key. See
    ``docs/design-notes.md`` ("Curated detection over entry points").
    """
    candidates = [fw.key for fw in _FRAMEWORKS if _module_loaded(fw.detector)]
    if not candidates:
        candidates = [
            fw.key for fw in _FRAMEWORKS if _module_available(fw.detector)
        ]
    detected = set(candidates)
    superseded = {
        key for fw in _FRAMEWORKS if fw.key in detected for key in fw.supersedes
    }
    return [key for key in candidates if key not in superseded]


def _entry_point_classes() -> list[type[Instrumentor]]:
    """Load every instrumentor advertised under the entry-point group.

    Backs ``instrument="all"``. A broken or incompatible instrumentor is skipped
    rather than allowed to abort the run.
    """
    from importlib.metadata import entry_points

    classes: list[type[Instrumentor]] = []
    for entry_point in entry_points(group=_ENTRY_POINT_GROUP):
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
    instrument: InstrumentSelection,
) -> list[Instrumentor]:
    """Return instantiated instrumentors for the requested selection.

    * ``None`` / ``"curated"``  -> curated auto-detection (default)
    * ``"all"``                 -> entry-point discovery
    * :data:`InstrumentKey`     -> a single registry key (e.g. ``"openai_agents"``)
    * a sequence of keys        -> exactly those, in the order given

    ``None`` is accepted as a synonym for ``"curated"`` so the bare
    ``init(project)`` does the auto-detection.

    Raises:
        ValueError: If an explicitly requested key is not in the curated
            registry. The message lists the known keys, since a typo is the
            likely cause -- one a type checker now catches first, but the check
            stays because a key can arrive at runtime from a config file or a
            command line. Auto-detection cannot hit this: it selects the keys
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
