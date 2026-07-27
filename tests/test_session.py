"""Tests for :mod:`argus.session` -- init, the re-init guard, reset, flush."""

from __future__ import annotations

import sys
import warnings

import pytest

import argus
from argus import Session
from argus import session as session_module

from tests.factories import (
    PlainSpanExporter,
    RaisingUninstrumentor,
    make_instrumentor,
)


class _BufferedExporter(PlainSpanExporter):
    """A plain exporter that also opts into Argus's ``emit`` hook."""

    def __init__(self) -> None:
        super().__init__()
        self.emit_calls: list[bool] = []

    def emit(self, failed: bool = False) -> None:
        self.emit_calls.append(failed)


class _HostileExporter(PlainSpanExporter):
    """A plain exporter whose lifecycle calls both raise."""

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        super().force_flush(timeout_millis)
        raise RuntimeError("force_flush boom")

    def shutdown(self) -> None:
        super().shutdown()
        raise RuntimeError("shutdown boom")


class TestInit:
    def test_returns_session_and_registers_singleton(
        self, use_instrumentors, recording_exporter
    ):
        inst = make_instrumentor()
        received = use_instrumentors(inst)

        session = argus.init("proj", exporters=[recording_exporter])

        assert isinstance(session, Session)
        assert session_module._session is session
        # Detection ran once with the default (curated) selection.
        assert received == [None]
        # The framework was instrumented exactly once, against this provider.
        assert inst.instrument_calls == [session.provider]
        assert session.instrumentors == [inst]
        assert session.instruments == ["FakeInstrumentor"]

    def test_provider_does_not_register_its_own_atexit_shutdown(
        self, use_instrumentors, recording_exporter
    ):
        # Regression guard for an atexit ordering collision. A TracerProvider
        # registers its own atexit shutdown by default, and atexit runs LIFO;
        # since Argus's _flush_on_exit is registered at import (before any
        # provider), that shutdown would run first on exit and tear down the
        # OTLP transport before Argus's flush emits -- so emit() would hit an
        # already-dead transport and the backend would never be contacted.
        # Argus owns the lifecycle, so the provider must register no handler.
        # There is no public way to ask, so this reads a private OTel attribute
        # deliberately: if an SDK upgrade renames it, this test failing is the
        # cheapest possible warning that the wiring needs rechecking.
        use_instrumentors()
        session = argus.init("proj", exporters=[recording_exporter])

        assert session.provider._atexit_handler is None

    def test_stamps_resource_attributes(
        self, use_instrumentors, recording_exporter
    ):
        use_instrumentors()
        session = argus.init(
            "my-project",
            service="my-service",
            exporters=[recording_exporter],
        )

        attributes = session.provider.resource.attributes
        assert attributes["service.name"] == "my-service"
        assert attributes["argus.project"] == "my-project"
        assert attributes["argus.version"] == argus.__version__


class TestSpanLimits:
    """The raised span attribute ceiling and its env-var escape hatch.

    OpenTelemetry drops a span's oldest attributes once it exceeds 128, which
    silently loses the model's output on long conversations (OpenInference
    flattens each message into several attributes). Argus raises that ceiling.
    """

    ENV = session_module._SPAN_ATTRIBUTE_COUNT_ENV_VAR
    GENERIC_ENV = session_module._ATTRIBUTE_COUNT_ENV_VAR
    DEFAULT = session_module._DEFAULT_MAX_SPAN_ATTRIBUTES

    @pytest.fixture(autouse=True)
    def clean_limit_env(self, monkeypatch):
        """Ignore the developer's own OTel limit vars; tests set what they need.

        Either variable can decide the ceiling, so a machine that happens to
        export one would satisfy the tests asserting the default applies.
        """
        monkeypatch.delenv(self.ENV, raising=False)
        monkeypatch.delenv(self.GENERIC_ENV, raising=False)

    def test_default_raises_ceiling_when_env_absent(self):
        limits = session_module._resolve_span_limits()

        assert limits.max_span_attributes == self.DEFAULT

    def test_env_var_overrides_default(self, monkeypatch):
        monkeypatch.setenv(self.ENV, "8000")

        limits = session_module._resolve_span_limits()

        assert limits.max_span_attributes == 8000

    def test_empty_env_var_means_unlimited(self, monkeypatch):
        monkeypatch.setenv(self.ENV, "")

        limits = session_module._resolve_span_limits()

        assert limits.max_span_attributes is None

    @pytest.mark.parametrize("value", ["garbage", "-5"])
    def test_invalid_env_var_falls_back_to_default(self, monkeypatch, value):
        monkeypatch.setenv(self.ENV, value)

        limits = session_module._resolve_span_limits()

        assert limits.max_span_attributes == self.DEFAULT

    def test_generic_attribute_env_var_is_honored(self, monkeypatch):
        monkeypatch.setenv(self.GENERIC_ENV, "256")

        limits = session_module._resolve_span_limits()

        # OpenTelemetry applies the generic ceiling to span attributes only as
        # the default for the span-specific limit -- which the explicit value
        # Argus passes would shadow, silently beating a cap an operator set.
        assert limits.max_span_attributes == 256

    def test_span_specific_env_var_wins_over_generic(self, monkeypatch):
        monkeypatch.setenv(self.ENV, "8000")
        monkeypatch.setenv(self.GENERIC_ENV, "256")

        limits = session_module._resolve_span_limits()

        assert limits.max_span_attributes == 8000

    def test_empty_generic_env_var_leaves_the_default_in_place(
        self, monkeypatch
    ):
        monkeypatch.setenv(self.GENERIC_ENV, "")

        limits = session_module._resolve_span_limits()

        # Unlike the span-specific variable, an empty generic one is not "no
        # limit": OpenTelemetry cannot tell it from an unset one, so neither do
        # we, and our raised default stands.
        assert limits.max_span_attributes == self.DEFAULT

    def test_generic_env_var_still_governs_other_attribute_limits(
        self, monkeypatch
    ):
        monkeypatch.setenv(self.ENV, "8000")
        monkeypatch.setenv(self.GENERIC_ENV, "256")

        limits = session_module._resolve_span_limits()

        # Only the span attribute count is ours to raise; event and link
        # attributes keep whatever OpenTelemetry resolves for them.
        assert limits.max_attributes == 256
        assert limits.max_event_attributes == 256
        assert limits.max_link_attributes == 256

    def test_init_provider_carries_raised_limit(
        self, use_instrumentors, recording_exporter
    ):
        use_instrumentors()

        session = argus.init("proj", exporters=[recording_exporter])

        # A private OTel attribute on purpose: it pins the exact ceiling the
        # provider was built with, which the behavioral test below cannot (that
        # one only proves the ceiling is somewhere above 200). Should an SDK
        # upgrade rename it, this failing is the warning to recheck the wiring.
        assert session.provider._span_limits.max_span_attributes == self.DEFAULT

    def test_provider_retains_attributes_past_otel_default(
        self, use_instrumentors, recording_exporter
    ):
        # The regression guard: a span with far more than OTel's default of
        # 128 attributes must keep every one, so a long agent conversation
        # never loses its final output message to silent truncation.
        use_instrumentors()
        session = argus.init("proj", exporters=[recording_exporter])

        span = session.provider.get_tracer("test").start_span("response")
        for i in range(200):
            span.set_attribute(f"llm.input_messages.{i}.message.role", "tool")
        span.end()

        assert len(span.attributes) == 200
        assert span.dropped_attributes == 0


class TestPlainExporterLifecycle:
    """``exporters=`` accepts any OpenTelemetry exporter, not just Argus's own.

    One without an ``emit`` hook is driven the ordinary way: spans reach it as
    they end, ``force_flush`` on every flush, ``shutdown`` once at exit. See
    ``docs/design-notes.md`` ("Exporters Argus does not own").
    """

    def test_spans_reach_it_as_they_end(self, use_instrumentors):
        use_instrumentors()
        plain = PlainSpanExporter()
        session = argus.init("proj", exporters=[plain])

        session.provider.get_tracer("test").start_span("work").end()

        assert [span.name for span in plain.exported_spans] == ["work"]

    def test_flush_force_flushes_it(self, use_instrumentors):
        use_instrumentors()
        plain = PlainSpanExporter()
        session = argus.init("proj", exporters=[plain])
        session.provider.get_tracer("test").start_span("work").end()

        session.flush()

        # Without this an exporter that batches internally loses the run.
        assert plain.force_flush_count == 1

    def test_exit_shuts_it_down(self, use_instrumentors):
        use_instrumentors()
        plain = PlainSpanExporter()
        argus.init("proj", exporters=[plain])

        session_module._flush_on_exit()

        assert plain.shutdown_count == 1

    def test_flush_alone_does_not_shut_it_down(self, use_instrumentors):
        use_instrumentors()
        plain = PlainSpanExporter()
        session = argus.init("proj", exporters=[plain])

        session.flush()

        # A scoped flush must leave the exporter usable: the program may keep
        # running and producing spans after it.
        assert plain.shutdown_count == 0

    def test_emit_hook_takes_precedence_over_force_flush(
        self, use_instrumentors
    ):
        use_instrumentors()
        buffered = _BufferedExporter()
        session = argus.init("proj", exporters=[buffered])
        session.provider.get_tracer("test").start_span("work").end()

        session.flush(failed=True)

        # emit carries the run's outcome; force_flush cannot, so an exporter
        # offering both is driven only through emit.
        assert buffered.emit_calls == [True]
        assert buffered.force_flush_count == 0

    def test_a_failing_exporter_does_not_break_the_run(self, use_instrumentors):
        use_instrumentors()
        hostile = _HostileExporter()
        healthy = PlainSpanExporter()
        session = argus.init("proj", exporters=[hostile, healthy])
        session.provider.get_tracer("test").start_span("work").end()

        session.flush()  # must not raise
        session_module._flush_on_exit()

        # A broken sink is isolated: the one after it is still driven.
        assert hostile.force_flush_count == 1
        assert healthy.force_flush_count == 1
        assert healthy.shutdown_count == 1


class TestOutputDirWithCustomExporters:
    """``output_dir`` only configures the default file exporter."""

    def test_warns_when_given_alongside_exporters(
        self, use_instrumentors, recording_exporter, traces_dir
    ):
        use_instrumentors()

        # It cannot be applied -- the given exporters are already built -- so
        # silence would leave the caller believing traces go somewhere they
        # don't.
        with pytest.warns(RuntimeWarning, match="output_dir has no effect"):
            argus.init(
                "proj",
                exporters=[recording_exporter],
                output_dir=traces_dir,
            )

        assert not traces_dir.exists()

    def test_no_warning_when_it_configures_the_default_exporter(
        self, use_instrumentors, traces_dir, recwarn
    ):
        use_instrumentors()

        argus.init("proj", output_dir=traces_dir)

        assert [w for w in recwarn if "output_dir" in str(w.message)] == []
        assert traces_dir.is_dir()


class TestFlushIsNotTerminal:
    """A flush emits what has accumulated; it does not retire the session.

    The context manager is documented for scoped flushing, so a program can
    reasonably keep working (and keep producing spans) after one. Those spans
    must still be emitted rather than buffered into the void.
    """

    def test_spans_produced_after_a_flush_are_emitted_by_the_next(
        self, use_instrumentors, recording_exporter
    ):
        use_instrumentors()
        session = argus.init("proj", exporters=[recording_exporter])
        tracer = session.provider.get_tracer("test")
        tracer.start_span("before").end()
        session.flush()

        tracer.start_span("after").end()
        session.flush()

        assert recording_exporter.emit_calls == [False, False]

    def test_repeat_flush_with_nothing_new_stays_a_noop(
        self, use_instrumentors, recording_exporter
    ):
        use_instrumentors()
        session = argus.init("proj", exporters=[recording_exporter])
        session.provider.get_tracer("test").start_span("work").end()

        session.flush()
        session.flush()
        session.flush()

        # This is what keeps the context manager and the atexit hook from
        # duplicating each other's work.
        assert recording_exporter.emit_calls == [False]

    def test_work_after_a_context_manager_still_reaches_the_exit_flush(
        self, use_instrumentors, recording_exporter
    ):
        use_instrumentors()
        with argus.init("proj", exporters=[recording_exporter]) as session:
            session.provider.get_tracer("test").start_span("inside").end()

        session.provider.get_tracer("test").start_span("outside").end()
        session_module._flush_on_exit()

        # The trailing span triggered a second emit instead of being dropped.
        assert recording_exporter.emit_calls == [False, False]
        assert [s.name for s in recording_exporter.exported_spans] == [
            "inside",
            "outside",
        ]


class TestReinitGuard:
    def test_second_init_warns_and_returns_existing(
        self, use_instrumentors, recording_exporter
    ):
        inst = make_instrumentor()
        received = use_instrumentors(inst)
        first = argus.init("proj", exporters=[recording_exporter])

        with pytest.warns(RuntimeWarning, match="already been called"):
            second = argus.init("proj")

        assert second is first
        # The second call did no work: no re-detection, no re-instrumentation.
        assert received == [None]
        assert inst.instrument_calls == [first.provider]

    def test_warning_names_both_projects_on_mismatch(self, use_instrumentors):
        use_instrumentors()
        argus.init("alpha")

        with pytest.warns(RuntimeWarning) as record:
            argus.init("beta")

        message = str(record[0].message)
        assert "alpha" in message
        assert "beta" in message

    def test_warning_names_the_reset_escape_hatch(self, use_instrumentors):
        use_instrumentors()
        argus.init("proj")

        # The warning explains how to trace several frameworks at once; it must
        # also name the way to genuinely reconfigure, or a notebook user is
        # told only what they cannot do.
        with pytest.warns(RuntimeWarning, match=r"argus\.reset\(\)"):
            argus.init("proj")

    def test_reinit_can_be_promoted_to_error(self, use_instrumentors):
        use_instrumentors()
        argus.init("proj")

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with pytest.raises(RuntimeWarning):
                argus.init("proj")

        # Even when it raises, the original session is left intact.
        assert session_module._session is not None


class TestFlush:
    def test_propagates_process_failure_flag(
        self, use_instrumentors, recording_exporter, monkeypatch
    ):
        use_instrumentors()
        session = argus.init("proj", exporters=[recording_exporter])
        monkeypatch.setattr(session_module, "_run_failed", True)

        session.flush()

        assert recording_exporter.emit_calls == [True]

    def test_explicit_failed_overrides_flag(
        self, use_instrumentors, recording_exporter
    ):
        use_instrumentors()
        session = argus.init("proj", exporters=[recording_exporter])

        session.flush(failed=True)

        assert recording_exporter.emit_calls == [True]

    def test_is_idempotent(self, use_instrumentors, recording_exporter):
        use_instrumentors()
        session = argus.init("proj", exporters=[recording_exporter])

        session.flush()
        session.flush()

        assert recording_exporter.emit_calls == [False]


class TestContextManager:
    def test_flushes_success_on_clean_exit(
        self, use_instrumentors, recording_exporter
    ):
        use_instrumentors()
        with argus.init("proj", exporters=[recording_exporter]):
            pass

        assert recording_exporter.emit_calls == [False]

    def test_flags_failure_and_propagates_exception(
        self, use_instrumentors, recording_exporter
    ):
        use_instrumentors()
        with pytest.raises(ValueError, match="boom"):
            with argus.init("proj", exporters=[recording_exporter]):
                raise ValueError("boom")

        assert recording_exporter.emit_calls == [True]


class TestReset:
    """``argus.reset()`` retires the singleton so the next init is a real one.

    It is public because the per-process singleton, right as it is for a script,
    would otherwise pin the first configuration for the life of a notebook or
    REPL session -- where re-running the ``init`` cell is the normal way to
    change something.
    """

    def test_exposed_on_the_package(self):
        assert argus.reset is session_module.reset
        assert "reset" in argus.__all__

    def test_uninstruments_and_clears_singleton(self, use_instrumentors):
        inst = make_instrumentor()
        use_instrumentors(inst)
        argus.init("proj")

        argus.reset()

        assert inst.uninstrument_count == 1
        assert session_module._session is None

    def test_allows_a_fresh_init_afterwards(self, use_instrumentors):
        first_inst = make_instrumentor()
        use_instrumentors(first_inst)
        first = argus.init("proj")

        argus.reset()

        second_inst = make_instrumentor()
        use_instrumentors(second_inst)
        second = argus.init("proj")

        assert second is not first
        assert second_inst.instrument_calls == [second.provider]

    def test_reinit_after_reset_does_not_warn(self, use_instrumentors, recwarn):
        use_instrumentors()
        argus.init("alpha")

        argus.reset()
        argus.init("beta")

        # The documented escape hatch has to be silent, or following it would
        # still look like the mistake it replaces.
        assert [
            w for w in recwarn if "already been called" in str(w.message)
        ] == []

    def test_does_not_flush_the_session_it_retires(
        self, use_instrumentors, recording_exporter
    ):
        use_instrumentors()
        session = argus.init("proj", exporters=[recording_exporter])
        session.provider.get_tracer("test").start_span("work").end()

        argus.reset()

        # Teardown and emitting are separate decisions; callers who want the
        # buffered spans flush first.
        assert recording_exporter.emit_calls == []

    def test_clears_failure_flag(self, monkeypatch):
        monkeypatch.setattr(session_module, "_run_failed", True)

        argus.reset()

        assert session_module._run_failed is False

    def test_survives_uninstrument_error(self, use_instrumentors):
        inst = RaisingUninstrumentor()
        use_instrumentors(inst)
        argus.init("proj")

        argus.reset()  # must not raise

        assert inst.uninstrument_count == 1
        assert session_module._session is None


class TestFlushOnExit:
    def test_flushes_active_session(
        self, use_instrumentors, recording_exporter
    ):
        use_instrumentors()
        argus.init("proj", exporters=[recording_exporter])

        session_module._flush_on_exit()

        assert recording_exporter.emit_calls == [False]

    def test_noop_without_active_session(self):
        assert session_module._session is None
        session_module._flush_on_exit()  # must not raise

    def test_swallows_exporter_errors(self, use_instrumentors, monkeypatch):
        use_instrumentors()
        session = argus.init("proj")

        def boom(*_, **__):
            raise RuntimeError("flush boom")

        monkeypatch.setattr(session, "flush", boom)

        session_module._flush_on_exit()  # must not raise


class TestExcepthook:
    def test_marks_run_failed_and_delegates(self, monkeypatch):
        delegated = []
        monkeypatch.setattr(session_module, "_excepthook_installed", False)
        monkeypatch.setattr(
            sys, "excepthook", lambda *args: delegated.append(args)
        )

        session_module._install_excepthook()
        try:
            assert session_module._run_failed is False
            sys.excepthook(ValueError, ValueError("x"), None)
            assert session_module._run_failed is True
            assert len(delegated) == 1
        finally:
            session_module._run_failed = False

    def test_install_is_idempotent(self, monkeypatch):
        monkeypatch.setattr(session_module, "_excepthook_installed", False)
        original = sys.excepthook

        session_module._install_excepthook()
        after_first = sys.excepthook
        session_module._install_excepthook()
        after_second = sys.excepthook

        assert after_first is not original
        assert after_second is after_first
