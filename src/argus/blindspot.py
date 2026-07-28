"""The Argus escape hatch: :class:`blindspot`, a region the watcher ignores.

Argus records everything by default. When a workflow -- or a slice of one --
should stay off the record, :class:`blindspot` carves out a scope where no spans
are created at all, by attaching OpenTelemetry's own suppression flag to the
active context. The same object works three ways::

    with argus.blindspot():            # synchronous block
        run_sensitive_step()

    async with argus.blindspot():      # asynchronous block
        await run_sensitive_step()

    @argus.blindspot()                 # whole function, sync or async
    def internal_workflow(...):
        ...

The suppression follows ``await`` points and tasks spawned inside the block, but
not threads the caller starts. The decorator covers ordinary and ``async def``
functions and refuses generators with a :class:`TypeError` (see
:func:`_reject_generator`).

See ``docs/design-notes.md`` ("Suppression at the source", "Why the decorator
refuses generators").
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from contextvars import ContextVar
from types import TracebackType
from typing import Any, Literal, TypeVar

from opentelemetry.context import (
    _SUPPRESS_INSTRUMENTATION_KEY,
    Context,
    Token,
    attach,
    detach,
    set_value,
)

F = TypeVar("F", bound=Callable[..., Any])

# The tokens for the blindspot scopes currently open, innermost last. A
# ``ContextVar`` rather than instance state so the stack is per thread and per
# task, like the suppression it undoes: a token can only be detached from the
# context it was attached in, so an instance shared across threads or tasks must
# not let one of them pop another's token.
_open_tokens: ContextVar[tuple[Token[Context], ...]] = ContextVar(
    "argus_blindspot_tokens", default=()
)


def _reject_generator(func: Callable[..., Any]) -> None:
    """Refuse to decorate a generator function, explaining what to do instead.

    A generator runs in its consumer's context, so suppression can be neither
    confined to its body nor safely dropped -- both failures being silent, which
    a privacy primitive cannot ship. The error names the pattern that does work:
    wrap the *consumption* of the generator. See ``docs/design-notes.md`` ("Why
    the decorator refuses generators").

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

    A single instance can be entered repeatedly and nested safely -- including
    from several threads or tasks at once, since the tokens that undo the
    suppression live in :data:`_open_tokens` rather than on the instance -- and
    the decorator establishes suppression per call, so recursive or concurrent
    invocations never interfere.
    """

    def _enter(self) -> blindspot:
        """Attach the suppression flag and remember the token to undo it."""
        token = attach(set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
        _open_tokens.set(_open_tokens.get() + (token,))
        return self

    def _exit(self) -> None:
        """Detach the most recent suppression token, restoring the context."""
        open_tokens = _open_tokens.get()
        if open_tokens:
            _open_tokens.set(open_tokens[:-1])
            detach(open_tokens[-1])

    def __enter__(self) -> blindspot:
        """Begin a synchronous blindspot."""
        return self._enter()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        """End the blindspot, restoring tracing even if the block raised.

        Returns ``False`` -- typed as :data:`~typing.Literal` so a type checker
        knows it is *always* false -- so any exception from the block propagates
        normally rather than being swallowed.
        """
        self._exit()
        return False

    async def __aenter__(self) -> blindspot:
        """Begin an asynchronous blindspot (``async with``)."""
        return self._enter()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        """End an asynchronous blindspot, restoring tracing on exit."""
        self._exit()
        return False

    def __call__(self, func: F) -> F:
        """Wrap ``func`` so each call runs inside its own blindspot.

        Coroutine functions are wrapped so the suppression spans the awaited
        body; plain functions get a synchronous wrapper.

        Raises:
            TypeError: If ``func`` is a generator or async-generator function,
                whose body suppression cannot be scoped to (see
                :func:`_reject_generator`).
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
