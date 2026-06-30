"""Tests for :class:`argus.blindspot` -- the trace suppression escape hatch.

The contract a blindspot must uphold is narrow and precise: while the scope is
active OpenTelemetry's :data:`_SUPPRESS_INSTRUMENTATION_KEY` is set in the
ambient context (that is the flag every OpenInference instrumentor checks before
creating a span), and the moment the scope exits the context is restored to
exactly what it was -- even when the body raises. These tests assert that flag's
state directly, plus a small fake "instrumentor" that mirrors how a real one
reads the flag, so the suppression is exercised the way production code sees it.
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY, get_value

import argus
from argus.blindspot import blindspot


def suppressed() -> bool:
    """Whether tracing is currently suppressed in the active context."""
    return get_value(_SUPPRESS_INSTRUMENTATION_KEY) is True


def fake_instrument() -> bool:
    """Mimic an instrumentor: report whether it *would* record a span now.

    Real OpenInference instrumentors short-circuit when the suppression flag is
    set, so "would record" is simply the negation of :func:`suppressed`.
    """
    return not suppressed()


class TestContextManager:
    def test_suppresses_within_block_and_restores_after(self):
        assert not suppressed()
        with argus.blindspot():
            assert suppressed()
        assert not suppressed()

    def test_an_instrumentor_skips_inside_and_records_outside(self):
        assert fake_instrument() is True
        with argus.blindspot():
            assert fake_instrument() is False
        assert fake_instrument() is True

    def test_restores_context_even_when_block_raises(self):
        with pytest.raises(ValueError, match="boom"):
            with argus.blindspot():
                assert suppressed()
                raise ValueError("boom")
        assert not suppressed()

    def test_returns_the_blindspot_as_with_target(self):
        with argus.blindspot() as bs:
            assert isinstance(bs, blindspot)

    def test_nesting_the_same_instance_is_balanced(self):
        bs = argus.blindspot()
        with bs:
            assert suppressed()
            with bs:
                assert suppressed()
            # Inner exit must not lift suppression while the outer scope holds.
            assert suppressed()
        assert not suppressed()


class TestDecorator:
    def test_suppresses_for_the_duration_of_a_sync_call(self):
        @argus.blindspot()
        def work():
            return suppressed()

        assert not suppressed()
        assert work() is True
        assert not suppressed()

    def test_preserves_wrapped_function_metadata(self):
        @argus.blindspot()
        def documented(a, b):
            """A docstring to preserve."""
            return a + b

        assert documented.__name__ == "documented"
        assert documented.__doc__ == "A docstring to preserve."
        assert documented(2, 3) == 5

    def test_restores_context_when_decorated_function_raises(self):
        @argus.blindspot()
        def boom():
            raise ValueError("nope")

        with pytest.raises(ValueError, match="nope"):
            boom()
        assert not suppressed()

    def test_recursion_stays_suppressed_until_the_outermost_return(self):
        seen = []

        @argus.blindspot()
        def countdown(n):
            seen.append(suppressed())
            if n:
                countdown(n - 1)
            return suppressed()

        assert countdown(3) is True
        assert seen == [True, True, True, True]
        assert not suppressed()


class TestAsync:
    def test_async_context_manager_suppresses_across_await(self):
        async def scenario():
            assert not suppressed()
            async with argus.blindspot():
                assert suppressed()
                await asyncio.sleep(0)
                assert suppressed()
            assert not suppressed()

        asyncio.run(scenario())

    def test_async_context_manager_restores_on_exception(self):
        async def scenario():
            with pytest.raises(ValueError, match="boom"):
                async with argus.blindspot():
                    raise ValueError("boom")
            assert not suppressed()

        asyncio.run(scenario())

    def test_decorating_a_coroutine_returns_a_coroutine_function(self):
        @argus.blindspot()
        async def work():
            await asyncio.sleep(0)
            return suppressed()

        assert asyncio.iscoroutinefunction(work)

        async def scenario():
            assert await work() is True
            assert not suppressed()

        asyncio.run(scenario())

    def test_suppression_propagates_into_tasks_spawned_inside(self):
        async def scenario():
            async with argus.blindspot():
                # A task copies the current context at creation, so it inherits
                # the suppression flag.
                assert await asyncio.create_task(_report()) is True
            assert await asyncio.create_task(_report()) is False

        async def _report():
            return suppressed()

        asyncio.run(scenario())
