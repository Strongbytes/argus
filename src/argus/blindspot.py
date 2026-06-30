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
        """
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
