"""Decide which OpenInference instrumentor(s) to turn on.

Two strategies are supported: a curated registry mapping each agent framework to
the instrumentor(s) it needs, detected by whether the framework is in use
(default), and entry-point discovery of every registered instrumentor (opt-in via
``instrument="all"``). See ``docs/design-notes.md`` ("Curated detection over
entry points").

:class:`Instrumentor` is the contract everything here resolves to, and the one
Argus drives: the extension point for a framework it doesn't know about yet.

A registry entry carries everything Argus knows about its framework, which
includes one thing detection itself never reads: the free functions the
framework's instrumentor patches by rebinding. :mod:`argus.bindings` watches
those, so that a name imported from the framework before ``init`` ran is
reported instead of quietly going untraced.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from importlib import import_module
from importlib.util import find_spec
from typing import Any, Literal, Protocol, runtime_checkable

from opentelemetry.trace import TracerProvider

# The public surface: the extension-point protocol and the key/strategy types
# ``instrument=`` accepts, all documented in the README and examples. Everything
# else in this module is Argus-internal detection machinery.
__all__ = [
    "InstrumentKey",
    "InstrumentSelection",
    "InstrumentStrategy",
    "Instrumentor",
]

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
InstrumentKey = Literal[
    "openai_agents",
    "claude",
    "agno",
    "openai",
    "anthropic",
    "langchain",
    "langgraph",
    "crewai",
]

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
    is in play; ``instrumentors`` are ``"module:ClassName"`` paths to apply;
    ``supersedes`` names the keys auto-detection drops when this framework is
    detected, which is what stops a narrower key from doubling up coverage this
    one already provides; ``companions`` names the provider keys auto-detection
    adds when this framework is detected, for a framework whose instrumentor
    records structure only and leaves the model calls to the providers'. A
    companion is found by importability rather than ``sys.modules``, because a
    framework may import its provider SDK only once an agent is built, after
    ``init`` has run; it is added only if its instrumentor is importable too,
    and it is still subject to supersession. Neither field ever changes an
    explicit list. And ``free_functions`` names the module-level
    functions of ``detector`` that its instrumentor patches by rebinding, which
    :mod:`argus.bindings` watches because a ``from``-import of one of those
    before ``init`` freezes the un-instrumented original. Everything about a
    framework is therefore one entry -- adding another needs no edit anywhere
    else in this module.
    """

    key: InstrumentKey
    detector: str
    instrumentors: tuple[str, ...]
    supersedes: tuple[InstrumentKey, ...] = ()
    companions: tuple[InstrumentKey, ...] = ()
    free_functions: tuple[str, ...] = ()


# Registry order is the order frameworks are detected, and so the order their
# instrumentors are applied. It is not what keeps OpenAI calls from being
# instrumented twice -- each entry's ``supersedes`` does that, and reordering
# these entries would not change it.
#
# No entry pairs an instrumentor it doesn't own. A framework whose instrumentor
# records the model calls itself ``supersedes`` the provider keys; one that
# records structure only names them as ``companions``; one with no Python
# provider SDK under it does neither. So provider spans are on unless a detected
# framework already records the model calls.
_FRAMEWORKS: tuple[_Framework, ...] = (
    _Framework(
        "openai_agents",
        "agents",
        (
            "openinference.instrumentation.openai_agents:OpenAIAgentsInstrumentor",
        ),
        # Load-bearing: this instrumentor covers OpenAI client calls itself, so
        # keeping the standalone key alongside it really would instrument them
        # twice. Not ``anthropic``: the SDK reaches Claude through LiteLLM, which
        # calls the Anthropic API over ``httpx`` rather than the ``anthropic``
        # package, so there is nothing for that key to double up.
        supersedes=("openai",),
    ),
    _Framework(
        "claude",
        "claude_agent_sdk",
        (
            "openinference.instrumentation.claude_agent_sdk:ClaudeAgentSDKInstrumentor",
        ),
        # The one framework here whose entry point is a free function rather than
        # a method: its instrumentor rebinds ``claude_agent_sdk.query``, so a
        # ``from claude_agent_sdk import query`` above the ``init`` line keeps
        # calling the original. See :mod:`argus.bindings`.
        free_functions=("query",),
    ),
    _Framework(
        "agno",
        "agno",
        ("openinference.instrumentation.agno:AgnoInstrumentor",),
        # Load-bearing, as for ``openai_agents``: Agno's instrumentor wraps
        # every model class under ``agno.models`` (``invoke``, ``ainvoke`` and
        # the streaming variants) and emits an LLM span with provider, model
        # and token counts whatever the provider. A provider instrumentor on as well
        # nests a second LLM span with the same tokens under each one, which a
        # backend summing usage counts twice -- for ``OpenAIChat`` and
        # ``Claude`` alike.
        supersedes=("openai", "anthropic"),
    ),
    # LangChain and LangGraph are two names for one instrumentor. OpenInference
    # ships no LangGraph instrumentor: ``LangChainInstrumentor`` hooks
    # ``langchain_core``'s callback manager, which LangGraph runs on too, so it
    # traces graph runs as well. The two entries exist so ``instrument=`` and
    # the extras can say what the caller is actually using, and a class is
    # instantiated once however many keys name it (see
    # ``resolve_instrumentors``).
    #
    # ``langchain`` is detected through ``langchain_core`` rather than the
    # ``langchain`` package: LangGraph (and ``langchain-openai`` and friends) can
    # be used without ``langchain`` itself, but never without ``langchain_core``,
    # which is also the module the instrumentor patches. ``langgraph``'s own
    # detector never changes what auto-detection turns on: anything that imports
    # it has already imported ``langchain_core``, so ``langchain`` is detected
    # too and both keys resolve to the one instrumentor. The entry earns its
    # place as a key -- an explicit ``instrument="langgraph"`` consults no
    # detector at all.
    #
    # Load-bearing, as for Agno: ``langchain-openai`` and
    # ``langchain-anthropic`` call the provider SDKs underneath, and the
    # standalone OpenAI or Anthropic instrumentor would report the same call a
    # second time -- same token counts, so a backend would count them twice.
    # LangChain's tracer never attaches its spans to the OpenTelemetry context,
    # so the second one does not nest under the first: it arrives as a trace of
    # its own. The cost of dropping them is that a node calling the raw
    # ``openai`` or ``anthropic`` client directly goes untraced;
    # ``instrument=["langchain", "openai"]`` opts back in, with that duplication
    # for every LangChain model call on that provider.
    _Framework(
        "langchain",
        "langchain_core",
        ("openinference.instrumentation.langchain:LangChainInstrumentor",),
        supersedes=("openai", "anthropic"),
    ),
    _Framework(
        "langgraph",
        "langgraph",
        ("openinference.instrumentation.langchain:LangChainInstrumentor",),
        supersedes=("openai", "anthropic"),
    ),
    # CrewAI's instrumentor wraps methods only -- ``Crew.kickoff``,
    # ``Task._execute_core``, ``Agent.kickoff``, ``Flow``'s entry points, the
    # tools' ``run`` -- and in its default wrapper mode emits crew, task, agent
    # and tool spans but no LLM spans. The instrumentor's other mode,
    # ``use_event_listener=True``, builds LLM spans from CrewAI's own events for
    # any provider, but its README keeps that for CrewAI AMP and recommends
    # wrapper mode for a Python app. ``init`` passes ``tracer_provider`` and
    # nothing else, so wrapper mode is what runs.
    #
    # The LLM spans come from the provider keys, turned on as companions. They
    # stay keys rather than being paired in as classes, so they remain subject
    # to supersession: a run that also uses LangChain drops them as
    # ``langchain`` requires, rather than having a pair smuggle a provider
    # instrumentor past that rule and report every ``ChatOpenAI`` call twice.
    #
    # Companions, rather than ordinary detection, because of when CrewAI loads
    # its providers. ``import crewai`` imports ``openai`` (a hard dependency),
    # so ``openai`` is detected anyway and listing it is redundant today -- but
    # it says what the entry means, and stops depending on CrewAI's import
    # graph. The native Anthropic provider (``anthropic/...`` and ``claude-...``
    # models) is imported lazily, in ``LLM._get_native_provider``, when an agent
    # on a Claude model is built. In the usual script order that is after
    # ``init``, so ``anthropic`` is not in ``sys.modules`` when detection runs,
    # and with ``crewai`` loaded the importability fallback never runs either.
    #
    # An explicit ``instrument="crewai"`` is not expanded, and so gets no LLM
    # spans -- ``["crewai", "openai"]`` or ``["crewai", "anthropic"]`` does. A
    # crew on Gemini, Bedrock or LiteLLM gets the structure spans and no LLM
    # spans, since Argus has no key for those providers.
    _Framework(
        "crewai",
        "crewai",
        ("openinference.instrumentation.crewai:CrewAIInstrumentor",),
        companions=("openai", "anthropic"),
    ),
    # The Anthropic API SDK, used directly -- not ``claude``, which is the Claude
    # Agent SDK. That one doesn't depend on ``anthropic``: it drives the Claude
    # Code CLI as a subprocess, so its model calls happen in another process and
    # the two keys never overlap. A script using both gets both.
    _Framework(
        "anthropic",
        "anthropic",
        ("openinference.instrumentation.anthropic:AnthropicInstrumentor",),
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

    Used as the fallback signal when nothing relevant is loaded yet, and for a
    detected framework's companions. A dotted name imports its parent packages
    to be looked up, but never the module itself. The broad ``except`` covers
    oddly-packaged modules whose spec lookup itself raises.
    """
    try:
        return find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _companion_available(framework: _Framework) -> bool:
    """Return whether a companion key can be turned on in this environment.

    Its provider SDK can be installed without its instrumentor (pulled in by
    something else), and ``_load`` would then raise out of ``init`` for a key
    nobody asked for. So both have to be importable; a companion that fails
    either check is skipped.
    """
    return _module_available(framework.detector) and all(
        _module_available(path.partition(":")[0])
        for path in framework.instrumentors
    )


def _auto_keys() -> list[InstrumentKey]:
    """Detect frameworks in use, preferring what's already imported.

    Falls back to importability when nothing relevant is loaded yet, then adds
    each detected framework's companions, then drops every key a detected
    framework supersedes (see :class:`_Framework`), so a framework's own
    coverage is never doubled up by a narrower key. Returned in registry order.
    See ``docs/design-notes.md`` ("Curated detection over entry points").
    """
    candidates = {fw.key for fw in _FRAMEWORKS if _module_loaded(fw.detector)}
    if not candidates:
        candidates = {
            fw.key for fw in _FRAMEWORKS if _module_available(fw.detector)
        }
    companions = {
        key
        for fw in _FRAMEWORKS
        if fw.key in candidates
        for key in fw.companions
        if key not in candidates and _companion_available(_BY_KEY[key])
    }
    detected = candidates | companions
    superseded = {
        key for fw in _FRAMEWORKS if fw.key in detected for key in fw.supersedes
    }
    return [
        fw.key
        for fw in _FRAMEWORKS
        if fw.key in detected and fw.key not in superseded
    ]


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
