"""Tests for :mod:`argus.session` -- init, the re-init guard, reset, flush."""

from __future__ import annotations

import sys
import warnings

import pytest

import argus
from argus import Session
from argus import session as session_module

from tests.factories import RaisingUninstrumentor, make_instrumentor


class TestInit:
    def test_returns_session_and_registers_singleton(
        self, use_instrumentors, recording_exporter, traces_dir
    ):
        inst = make_instrumentor()
        received = use_instrumentors(inst)

        session = argus.init(
            "proj",
            exporters=[recording_exporter],
            output_dir=traces_dir,
            load_dotenv=False,
        )

        assert isinstance(session, Session)
        assert session_module._session is session
        # Detection ran once with the default (curated) selection.
        assert received == [None]
        # The framework was instrumented exactly once, against this provider.
        assert inst.instrument_calls == [session.provider]
        assert session.instrumentors == [inst]
        assert session.instruments == ["FakeInstrumentor"]

    def test_provider_does_not_register_its_own_atexit_shutdown(
        self, use_instrumentors, recording_exporter, traces_dir
    ):
        # Regression guard for an atexit ordering collision. A TracerProvider
        # registers its own atexit shutdown by default, and atexit runs LIFO;
        # since Argus's _flush_on_exit is registered at import (before any
        # provider), that shutdown would run first on exit and tear down the
        # OTLP transport before Argus's flush emits -- so emit() would hit an
        # already-dead transport and the backend would never be contacted.
        # Argus owns the lifecycle, so the provider must register no handler.
        use_instrumentors()
        session = argus.init(
            "proj",
            exporters=[recording_exporter],
            output_dir=traces_dir,
            load_dotenv=False,
        )

        assert session.provider._atexit_handler is None

    def test_stamps_resource_attributes(
        self, use_instrumentors, recording_exporter
    ):
        use_instrumentors()
        session = argus.init(
            "my-project",
            service="my-service",
            exporters=[recording_exporter],
            load_dotenv=False,
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
    DEFAULT = session_module._DEFAULT_MAX_SPAN_ATTRIBUTES

    def test_default_raises_ceiling_when_env_absent(self, monkeypatch):
        monkeypatch.delenv(self.ENV, raising=False)

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

    def test_init_provider_carries_raised_limit(
        self, monkeypatch, use_instrumentors, recording_exporter
    ):
        monkeypatch.delenv(self.ENV, raising=False)
        use_instrumentors()

        session = argus.init(
            "proj", exporters=[recording_exporter], load_dotenv=False
        )

        assert session.provider._span_limits.max_span_attributes == self.DEFAULT

    def test_provider_retains_attributes_past_otel_default(
        self, monkeypatch, use_instrumentors, recording_exporter
    ):
        # The regression guard: a span with far more than OTel's default of
        # 128 attributes must keep every one, so a long agent conversation
        # never loses its final output message to silent truncation.
        monkeypatch.delenv(self.ENV, raising=False)
        use_instrumentors()
        session = argus.init(
            "proj", exporters=[recording_exporter], load_dotenv=False
        )

        span = session.provider.get_tracer("test").start_span("response")
        for i in range(200):
            span.set_attribute(f"llm.input_messages.{i}.message.role", "tool")
        span.end()

        assert len(span.attributes) == 200
        assert span.dropped_attributes == 0


class TestReinitGuard:
    def test_second_init_warns_and_returns_existing(
        self, use_instrumentors, recording_exporter
    ):
        inst = make_instrumentor()
        received = use_instrumentors(inst)
        first = argus.init(
            "proj", exporters=[recording_exporter], load_dotenv=False
        )

        with pytest.warns(RuntimeWarning, match="already been called"):
            second = argus.init("proj", load_dotenv=False)

        assert second is first
        # The second call did no work: no re-detection, no re-instrumentation.
        assert received == [None]
        assert inst.instrument_calls == [first.provider]

    def test_warning_names_both_projects_on_mismatch(self, use_instrumentors):
        use_instrumentors()
        argus.init("alpha", load_dotenv=False)

        with pytest.warns(RuntimeWarning) as record:
            argus.init("beta", load_dotenv=False)

        message = str(record[0].message)
        assert "alpha" in message
        assert "beta" in message

    def test_reinit_can_be_promoted_to_error(self, use_instrumentors):
        use_instrumentors()
        argus.init("proj", load_dotenv=False)

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with pytest.raises(RuntimeWarning):
                argus.init("proj", load_dotenv=False)

        # Even when it raises, the original session is left intact.
        assert session_module._session is not None


class TestFlush:
    def test_propagates_process_failure_flag(
        self, use_instrumentors, recording_exporter, monkeypatch
    ):
        use_instrumentors()
        session = argus.init(
            "proj", exporters=[recording_exporter], load_dotenv=False
        )
        monkeypatch.setattr(session_module, "_run_failed", True)

        session.flush()

        assert recording_exporter.emit_calls == [True]

    def test_explicit_failed_overrides_flag(
        self, use_instrumentors, recording_exporter
    ):
        use_instrumentors()
        session = argus.init(
            "proj", exporters=[recording_exporter], load_dotenv=False
        )

        session.flush(failed=True)

        assert recording_exporter.emit_calls == [True]

    def test_is_idempotent(self, use_instrumentors, recording_exporter):
        use_instrumentors()
        session = argus.init(
            "proj", exporters=[recording_exporter], load_dotenv=False
        )

        session.flush()
        session.flush()

        assert recording_exporter.emit_calls == [False]


class TestContextManager:
    def test_flushes_success_on_clean_exit(
        self, use_instrumentors, recording_exporter
    ):
        use_instrumentors()
        with argus.init(
            "proj", exporters=[recording_exporter], load_dotenv=False
        ):
            pass

        assert recording_exporter.emit_calls == [False]

    def test_flags_failure_and_propagates_exception(
        self, use_instrumentors, recording_exporter
    ):
        use_instrumentors()
        with pytest.raises(ValueError, match="boom"):
            with argus.init(
                "proj", exporters=[recording_exporter], load_dotenv=False
            ):
                raise ValueError("boom")

        assert recording_exporter.emit_calls == [True]


class TestReset:
    def test_uninstruments_and_clears_singleton(self, use_instrumentors):
        inst = make_instrumentor()
        use_instrumentors(inst)
        argus.init("proj", load_dotenv=False)

        session_module._reset()

        assert inst.uninstrument_count == 1
        assert session_module._session is None

    def test_allows_a_fresh_init_afterwards(self, use_instrumentors):
        first_inst = make_instrumentor()
        use_instrumentors(first_inst)
        first = argus.init("proj", load_dotenv=False)

        session_module._reset()

        second_inst = make_instrumentor()
        use_instrumentors(second_inst)
        second = argus.init("proj", load_dotenv=False)

        assert second is not first
        assert second_inst.instrument_calls == [second.provider]

    def test_clears_failure_flag(self, monkeypatch):
        monkeypatch.setattr(session_module, "_run_failed", True)

        session_module._reset()

        assert session_module._run_failed is False

    def test_survives_uninstrument_error(self, use_instrumentors):
        inst = RaisingUninstrumentor()
        use_instrumentors(inst)
        argus.init("proj", load_dotenv=False)

        session_module._reset()  # must not raise

        assert inst.uninstrument_count == 1
        assert session_module._session is None


class TestFlushOnExit:
    def test_flushes_active_session(
        self, use_instrumentors, recording_exporter
    ):
        use_instrumentors()
        argus.init("proj", exporters=[recording_exporter], load_dotenv=False)

        session_module._flush_on_exit()

        assert recording_exporter.emit_calls == [False]

    def test_noop_without_active_session(self):
        assert session_module._session is None
        session_module._flush_on_exit()  # must not raise

    def test_swallows_exporter_errors(self, use_instrumentors, monkeypatch):
        use_instrumentors()
        session = argus.init("proj", load_dotenv=False)

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
