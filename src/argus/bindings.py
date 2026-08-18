"""Catch the import order that silently traces nothing.

An OpenInference instrumentor patches by *rebinding a name*. Once
``ClaudeAgentSDKInstrumentor`` has run, ``claude_agent_sdk.query`` names a
wrapper that opens a span and delegates to the function it replaced. That works
because the patched name is resolved at call time: ``claude_agent_sdk.query(...)``
reads whatever the module attribute holds *now*, and a method call
(``Runner.run(...)``, ``agent.arun(...)``) resolves through its class the same
way -- which is why importing a class before ``init`` is harmless.

A ``from`` import resolves nothing later. It copies the object::

    from claude_agent_sdk import query   # binds the function object itself
    argus.init("my_project")             # rebinds claude_agent_sdk.query
    query(prompt=...)                    # still the original -- no span

The local name holds the function that was there at import time, and rebinding
the module attribute afterwards cannot reach it. Nothing raises: no span is
started, so the buffered exporters have nothing to emit, no trace file is
written, and the run looks exactly like one where Argus was never installed.
Silence of that kind is what the rest of ``init`` refuses (see
``docs/design-notes.md``, "Stale bindings from an import before init"), so this
module turns it into a warning.

It finds the case rather than guessing at it. Each registry entry names the free
functions its instrumentor rebinds (:class:`~argus.detection._Framework`);
:func:`init` snapshots those attributes with :func:`snapshot_free_functions`
*before* instrumenting, and :func:`warn_stale_bindings` compares afterwards. An
attribute whose object changed identity was really patched, so anything else in
``sys.modules`` still bound to the *old* object is a stale binding, reported by
module and attribute name in one :class:`RuntimeWarning` per framework.

Using the patch itself as the signal is what keeps the check quiet when nothing
is wrong: a framework imported before ``init`` but called through its module, a
framework nobody instrumented (``instrument=[]``), an import that happens after
``init``, or an upstream release that no longer patches the name -- none of them
leave a holder of the original, so none of them warn. Two limits follow from
scanning module namespaces: a stale reference held anywhere else (a default
argument, a class attribute, a closure) is invisible, and a holder is named
where it is *bound*, which is where the fix goes, not where it is called.
"""

from __future__ import annotations

import sys
import warnings
from collections.abc import Sequence
from dataclasses import dataclass

from .detection import _FRAMEWORKS, InstrumentKey

# Packages whose reference to a pre-patch function is not a caller's to fix, and
# not a blind spot either: the instrumentation machinery keeps the original
# precisely so ``uninstrument`` can put it back, and Argus's own snapshot below
# holds one too. The framework's own package is skipped as well (see
# :func:`_skipped_packages`), for the same reason: that is where the function is
# defined.
_INTERNAL_PACKAGES = ("argus", "openinference", "wrapt")


@dataclass(frozen=True)
class _FreeFunction:
    """A module-level function an instrumentor is expected to rebind.

    ``original`` is the object the framework's attribute held before
    instrumentation ran. It is what a stale binding still points at, and holding
    it here is also what keeps its ``id`` meaningful for the scan in
    :func:`_holders` -- an id is only unique among live objects.
    """

    key: InstrumentKey
    module: str
    attribute: str
    original: object


@dataclass(frozen=True)
class _StaleBinding:
    """A patched function, and the names still bound to the original.

    ``holders`` are ``"module.attribute"`` strings: the bindings a call would go
    through, which is where the caller has to make the fix.
    """

    key: InstrumentKey
    module: str
    attribute: str
    holders: tuple[str, ...]


def snapshot_free_functions() -> tuple[_FreeFunction, ...]:
    """Record the free functions instrumentation is about to rebind.

    Called by :func:`argus.init` before any instrumentor runs, since a stale
    binding can only be recognized by comparing what a patch replaced against
    what is still bound elsewhere.

    Only frameworks already in ``sys.modules`` are snapshotted, and only the
    attributes their registry entry declares. A framework that is not imported
    yet needs no check at all: a ``from``-import binding cannot exist before the
    import that would create it, and the instrumentor's own import comes later.
    A declared name the module does not have (an upstream rename) or one holding
    something uncallable (a submodule shadowing the re-exported function) is
    skipped rather than watched, so neither can be mistaken for a patch.
    """
    watched: list[_FreeFunction] = []
    for framework in _FRAMEWORKS:
        module = sys.modules.get(framework.detector)
        if module is None:
            continue
        for attribute in framework.free_functions:
            original = getattr(module, attribute, None)
            if not callable(original):
                continue
            watched.append(
                _FreeFunction(
                    key=framework.key,
                    module=framework.detector,
                    attribute=attribute,
                    original=original,
                )
            )
    return tuple(watched)


def warn_stale_bindings(watched: Sequence[_FreeFunction]) -> None:
    """Warn about each framework whose patched functions are bound stale.

    One :class:`RuntimeWarning` per framework, listing every stale name it left
    behind, so a caller fixes one import rather than reading the same advice
    twice. Emitted as a warning rather than raised -- like every other thing
    ``init`` complains about -- and promotable with ``python -W error``.

    Attributed with ``stacklevel=3``: this frame, :func:`argus.init`'s, then the
    caller's own ``init`` line, which is where the import order can be changed.
    """
    stale = find_stale_bindings(watched)
    for key in dict.fromkeys(binding.key for binding in stale):
        warnings.warn(
            _describe([binding for binding in stale if binding.key == key]),
            RuntimeWarning,
            stacklevel=3,
        )


def find_stale_bindings(
    watched: Sequence[_FreeFunction],
) -> list[_StaleBinding]:
    """Return the patched functions still reachable under a pre-patch name.

    A watched attribute that still holds the object :func:`snapshot_free_functions`
    recorded was never patched, so it is dropped before anything is scanned:
    whatever is bound to it elsewhere is the live function, not a stale copy.
    What remains was genuinely rebound, and every other module-level name bound
    to the object it replaced is a call site instrumentation cannot reach.
    """
    patched = [
        function
        for function in watched
        if _current(function) is not function.original
    ]
    if not patched:
        return []
    holders = _holders(patched)
    return [
        _StaleBinding(
            key=function.key,
            module=function.module,
            attribute=function.attribute,
            holders=tuple(sorted(holders[id(function.original)])),
        )
        for function in patched
        if holders[id(function.original)]
    ]


def _current(function: _FreeFunction) -> object:
    """Return what the watched attribute holds now, or ``None`` if it is gone.

    An attribute that produces a fresh object on every read would compare as
    patched when it was not; the scan then finds no holder of the object first
    read, and the check degrades to saying nothing rather than to a false alarm.
    """
    module = sys.modules.get(function.module)
    return getattr(module, function.attribute, None)


def _holders(patched: Sequence[_FreeFunction]) -> dict[int, list[str]]:
    """Map each pre-patch function, by ``id``, to the names still bound to it.

    One pass over the loaded modules, matching on identity rather than on the
    attribute name, so an aliased import (``from x import query as q``) is found
    too. ``sys.modules`` is copied first because an import on another thread
    would otherwise resize it mid-iteration.
    """
    by_id = {id(function.original): function for function in patched}
    found: dict[int, list[str]] = {identity: [] for identity in by_id}
    skipped = _skipped_packages(patched)
    for module_name, module in list(sys.modules.items()):
        if module is None or any(
            _is_within(module_name, package) for package in skipped
        ):
            continue
        try:
            namespace = list(vars(module).items())
        except Exception:
            # A lazy-import shim or other module-like object standing in for a
            # module need not have a readable namespace. It cannot be the
            # caller's stale binding either, so skipping it costs nothing.
            continue
        for attribute, value in namespace:
            if id(value) in found:
                found[id(value)].append(f"{module_name}.{attribute}")
    return found


def _skipped_packages(patched: Sequence[_FreeFunction]) -> tuple[str, ...]:
    """Return the packages whose bindings are not worth reporting."""
    return _INTERNAL_PACKAGES + tuple({function.module for function in patched})


def _is_within(module_name: str, package: str) -> bool:
    """Return whether ``module_name`` is ``package`` or lives inside it."""
    return module_name == package or module_name.startswith(package + ".")


def _describe(stale: Sequence[_StaleBinding]) -> str:
    """Compose one framework's warning: what is stale, why, and the two fixes.

    Both fixes are named because either is enough and they suit different code:
    moving ``init`` above the import keeps the ``from`` spelling, while calling
    through the module keeps the import where it is.
    """
    module = stale[0].module
    functions = ", ".join(f"{module}.{b.attribute}" for b in stale)
    example = f"{module}.{stale[0].attribute}"
    names = [holder for binding in stale for holder in binding.holders]
    subject = ", ".join(names)
    verb, pronoun = ("points", "it") if len(names) == 1 else ("point", "them")
    return (
        f"Argus: {module} was imported before argus.init() ran, so {subject} "
        f"still {verb} at the original, un-instrumented {functions}. Calls "
        f"through {pronoun} produce no spans, and a run whose traced work all "
        f"goes through {pronoun} writes no trace file at all. A `from {module} "
        "import ...` binds the function object itself, so the instrumentor "
        f"rebinding {functions} afterwards cannot reach that name. Fix it by "
        f"calling argus.init() before importing {module}, or by importing the "
        f"module and calling {example}(...), where the name is looked up at "
        "call time."
    )
