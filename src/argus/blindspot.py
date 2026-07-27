"""The Argus escape hatch: :class:`blindspot`, a region the watcher ignores.

Argus's default stance is to record everything an instrumented run does. That
is the right default, but not every step deserves a trace -- a workflow may
touch secrets, PII, or simply noise you never want on disk. :class:`blindspot`
carves out a scope where Argus looks away.

The mechanism is OpenTelemetry's own suppression flag. Entering a blindspot
attaches :data:`~opentelemetry.context._SUPPRESS_INSTRUMENTATION_KEY` to the
active context; OpenInference's instrumentors (and OTel-aware libraries in
general) check that flag and skip span creation entirely while it is set.
Nothing is created, buffered, or written -- this suppresses at the source
rather than dropping spans after the fact, so sensitive payloads never enter
the pipeline at all.

Because the flag rides on a :mod:`contextvars`-backed context, the suppression
follows ``await`` points and copies into tasks spawned inside the block. It does
*not* reach threads you start yourself (a raw :class:`threading.Thread` or a
:class:`~concurrent.futures.ThreadPoolExecutor`), since those begin from a fresh
context unless you explicitly copy it.

The same object works three ways::

    with argus.blindspot():            # synchronous block
        run_sensitive_step()

    async with argus.blindspot():      # asynchronous block
        await run_sensitive_step()

    @argus.blindspot()                 # whole function, sync or async
    def internal_workflow(...):
        ...

The decorator covers ordinary and ``async def`` functions. It refuses generator
and async-generator functions with a :class:`TypeError`, because a generator
borrows its consumer's context and suppression therefore cannot be confined to
its body -- wrap the loop that consumes it instead (see
:func:`_reject_generator`).
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable, List, Optional, TypeVar

from opentelemetry.context import (
    _SUPPRESS_INSTRUMENTATION_KEY,
    attach,
    detach,
    set_value,
)

F = TypeVar("F", bound=Callable[..., Any])


def _reject_generator(func: Callable[..., Any]) -> None:
    """Refuse to decorate a generator function, explaining what to do instead.

    A generator does not get a context of its own the way a coroutine does: it
    runs in whatever context its consumer is in. So a wrapper holding the
    suppression across a ``yield`` cannot confine it to the generator's body --
    the flag stays attached while the consumer runs too, quietly suppressing
    code the caller never meant to hide. Nor can the wrapper simply not hold it,
    because the body would then run unsuppressed, which is worse: the decorator
    would look like it was protecting a sensitive stream while doing nothing.

    Both failure modes are silent, and for a feature whose whole job is keeping
    payloads off the record, silence is the one thing we cannot ship. So we
    refuse at decoration time -- when the mistake is cheap to fix -- and name the
    pattern that does work: wrap the *consumption* of the generator, which
    covers the body and the consumer alike, deliberately.

    Raises:
        TypeError: If ``func`` is a generator or async-generator function.
    """
    if inspect.isasyncgenfunction(func):
        kind, opener, loop = (
            "an async generator function",
            "async with",
            "async for",
        )
    elif inspect.isgeneratorfunction(func):
        kind, opener, loop = "a generator function", "with", "for"
    else:
        return
    name = getattr(func, "__name__", "<anonymous>")
    raise TypeError(
        f"argus.blindspot() cannot decorate {name!r} because it is {kind}. A "
        "generator runs inside its consumer's context, so the suppression "
        "would leak into the consumer between yields instead of covering only "
        "the generator's body. Wrap the point of consumption instead:\n\n"
        f"    {opener} argus.blindspot():\n"
        f"        {loop} item in {name}(...):\n"
        "            ...\n"
    )


class blindspot:
    """A scope Argus does not trace -- usable as context manager or decorator.

    Suppression is keyed to the context active when the scope is entered, so a
    single instance can be entered more than once (and nested) safely: each
    entry stacks its own token and exit pops it in last-in/first-out order.
    Used as a decorator the suppression is established per call, so concurrent
    or recursive invocations never interfere.
    """

    def __init__(self) -> None:
        """Create an (initially inactive) blindspot.

        The token stack is what makes re-entry and nesting safe; it holds one
        OpenTelemetry context token per active ``with``/``async with`` entry.
        """
        self._tokens: List[object] = []

    def _enter(self) -> "blindspot":
        """Attach the suppression flag and remember the token to undo it."""
        self._tokens.append(
            attach(set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
        )
        return self

    def _exit(self) -> None:
        """Detach the most recent suppression token, restoring the context."""
        if self._tokens:
            detach(self._tokens.pop())

    def __enter__(self) -> "blindspot":
        """Begin a synchronous blindspot."""
        return self._enter()

    def __exit__(self, exc_type, exc, tb) -> bool:
        """End the blindspot, restoring tracing even if the block raised.

        Returns ``False`` so any exception from the block propagates normally
        rather than being swallowed.
        """
        self._exit()
        return False

    async def __aenter__(self) -> "blindspot":
        """Begin an asynchronous blindspot (``async with``)."""
        return self._enter()

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        """End an asynchronous blindspot, restoring tracing on exit."""
        self._exit()
        return False

    def __call__(self, func: F) -> F:
        """Wrap ``func`` so each call runs inside its own blindspot.

        Coroutine functions are wrapped so the suppression spans the awaited
        body; plain functions get a synchronous wrapper. A fresh
        :class:`blindspot` is used per invocation so the decorator is safe under
        recursion and concurrency.

        Raises:
            TypeError: If ``func`` is a generator or async-generator function.
                Suppression cannot be scoped to a generator's body (see
                :func:`_reject_generator`), so rather than decorate one
                misleadingly we refuse and point at the pattern that works.
        """
        _reject_generator(func)
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                async with blindspot():
                    return await func(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with blindspot():
                return func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]
