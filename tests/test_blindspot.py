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
import threading

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

    def test_sharing_one_instance_across_threads_is_balanced(self):
        bs = argus.blindspot()
        nested = threading.Event()
        joined = threading.Event()
        released = threading.Event()
        outcomes = {}

        def holder():
            with bs:
                with bs:
                    nested.set()
                    assert joined.wait(timeout=5)
                # The other thread entered while this nesting was open, so an
                # exit reaching for a token attached elsewhere would lift the
                # suppression this outer scope still holds.
                held = suppressed()
            outcomes["holder"] = (held, suppressed())
            released.set()

        def joiner():
            assert nested.wait(timeout=5)
            with bs:
                inside = suppressed()
                joined.set()
                assert released.wait(timeout=5)
            outcomes["joiner"] = (inside, suppressed())

        threads = [
            threading.Thread(target=target) for target in (holder, joiner)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert outcomes == {"holder": (True, False), "joiner": (True, False)}


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


class TestGeneratorsAreRejected:
    """Decorating a generator function raises instead of failing silently.

    A generator borrows its consumer's context, which suppression cannot be
    scoped to, so the decorator refuses at decoration time with a message naming
    the pattern that does work. See ``docs/design-notes.md`` ("Why the decorator
    refuses generators").
    """

    def test_sync_generator_function_raises(self):
        with pytest.raises(TypeError, match="generator function"):

            @argus.blindspot()
            def streaming():
                yield 1

    def test_async_generator_function_raises(self):
        with pytest.raises(TypeError, match="async generator function"):

            @argus.blindspot()
            async def streaming():
                yield 1

    def test_error_names_the_function_and_the_working_pattern(self):
        def stream_answer():
            yield 1

        with pytest.raises(TypeError) as excinfo:
            argus.blindspot()(stream_answer)

        message = str(excinfo.value)
        assert "stream_answer" in message
        # The message has to carry the fix, since the whole point is that the
        # caller cannot see the failure any other way.
        assert "with argus.blindspot():" in message
        assert "for item in stream_answer(...)" in message

    def test_async_error_suggests_async_with_and_async_for(self):
        async def stream_answer():
            yield 1

        with pytest.raises(TypeError) as excinfo:
            argus.blindspot()(stream_answer)

        message = str(excinfo.value)
        assert "async with argus.blindspot():" in message
        assert "async for item in stream_answer(...)" in message

    def test_wrapping_the_consumption_site_is_the_documented_alternative(self):
        def stream_answer():
            yield suppressed()
            yield suppressed()

        with argus.blindspot():
            collected = list(stream_answer())

        # The generator body ran suppressed, and the scope closed cleanly.
        assert collected == [True, True]
        assert not suppressed()

    def test_ordinary_and_async_functions_are_still_accepted(self):
        @argus.blindspot()
        def sync_work():
            return suppressed()

        @argus.blindspot()
        async def async_work():
            return suppressed()

        assert sync_work() is True
        assert asyncio.run(async_work()) is True


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

    def test_sharing_one_instance_across_tasks_is_balanced(self):
        bs = argus.blindspot()

        async def scenario():
            nested = asyncio.Event()
            joined = asyncio.Event()
            released = asyncio.Event()

            async def holder():
                async with bs:
                    async with bs:
                        nested.set()
                        await joined.wait()
                    # The other task entered while this nesting was open, so an
                    # exit reaching for a token attached in that task's context
                    # would lift the suppression this outer scope still holds.
                    held = suppressed()
                released.set()
                return held, suppressed()

            async def joiner():
                await nested.wait()
                async with bs:
                    inside = suppressed()
                    joined.set()
                    await released.wait()
                return inside, suppressed()

            return await asyncio.gather(holder(), joiner())

        assert asyncio.run(scenario()) == [(True, False), (True, False)]

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
