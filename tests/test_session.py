"""Tests for :mod:`argus.session` -- init, the re-init guard, reset, flush."""

from __future__ import annotations

import json
import os
import sys
import warnings

import pytest

import argus
from argus import Session
from argus import session as session_module

# Bound at import, so the autouse ``no_dotenv`` fixture -- which replaces the
# attribute on the session module -- cannot reach it. ``TestLoadDotenv`` is the
# one place that wants the real function rather than the stub.
from argus.session import _load_dotenv as _real_load_dotenv

from tests.factories import (
    PlainSpanExporter,
    RaisingUninstrumentor,
    make_instrumentor,
    patch_resolve_instrumentors,
)


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
        assert session.instruments == ("FakeInstrumentor",)

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


class TestTheDocumentedOneLiner:
    """The README's headline flow, end to end, through the real default sink.

    Every other test here hands ``init`` an exporter of its own, so this is the
    only one that runs what the README's first code block promises: init, produce
    a span, exit -- and the run is on disk as the documented pair of files. What
    it covers that the unit tests cannot is the seam between them, where init's
    provider, the default :class:`~argus.exporters.file.FileSpanExporter` and the
    ``atexit`` flush all have to agree.
    """

    @pytest.fixture
    def written(self, use_instrumentors, traces_dir):
        """Run the documented flow, returning the files it left behind."""
        use_instrumentors()
        session = argus.init("proj", output_dir=traces_dir)

        session.provider.get_tracer("test").start_span("work").end()
        session_module._flush_on_exit()

        return sorted(traces_dir.iterdir())

    def test_exit_writes_the_documented_pair_of_files(self, written):
        # The stem is read back rather than pinned -- under pytest the script
        # name is the runner's, not an agent script's -- because what the README
        # promises is the pair: one base name, two format markers.
        # ``tests/test_file_exporter.py`` pins the scheme itself.
        otlp, readable = written
        assert otlp.name.endswith(".otlp.json")
        assert readable.name.endswith(".readable.json")
        assert otlp.name.removesuffix(
            ".otlp.json"
        ) == readable.name.removesuffix(".readable.json")

    def test_the_span_reaches_both_of_them(self, written):
        otlp, readable = written

        # Not merely that the files exist: a span really travelled provider ->
        # processor -> exporter -> disk, through the OTLP encoder on one side and
        # the readable rendering on the other.
        request = json.loads(otlp.read_text())
        (resource_spans,) = request["resourceSpans"]
        (scope_spans,) = resource_spans["scopeSpans"]
        assert [span["name"] for span in scope_spans["spans"]] == ["work"]
        assert [span["name"] for span in json.loads(readable.read_text())] == [
            "work"
        ]

    def test_the_project_is_stamped_on_what_lands_on_disk(self, written):
        _, readable = written

        # The resource init built survives the whole trip, which is what makes a
        # trace file attributable to the run that produced it.
        (span,) = json.loads(readable.read_text())
        assert span["resource"]["attributes"]["argus.project"] == "proj"


class TestSessionReportsButDoesNotRewire:
    """The session hands its state out to read, never to swap.

    ``tests/test_public_api.py`` pins *which* members are public; these pin what
    reading them gets you. See ``docs/design-notes.md`` ("The session reports,
    it does not rewire").
    """

    @pytest.fixture
    def session(self, use_instrumentors, recording_exporter):
        use_instrumentors(make_instrumentor())
        return argus.init("proj", exporters=[recording_exporter])

    @pytest.mark.parametrize(
        "name", ["provider", "project", "instruments", "exporters"]
    )
    def test_no_member_can_be_replaced(self, session, name):
        # Replacing the provider would leave the session flushing sinks that no
        # longer see the spans; replacing the sinks would drive exporters the
        # provider never feeds. Both fail loudly instead.
        with pytest.raises(AttributeError):
            setattr(session, name, object())

    def test_the_sinks_are_a_tuple_not_the_list_argus_drives(
        self, session, recording_exporter
    ):
        # A list would invite append(), which yields a sink that gets driven on
        # flush but never receives a span -- an empty trace file, silently.
        assert session.exporters == (recording_exporter,)
        with pytest.raises(AttributeError):
            session.exporters.append(PlainSpanExporter())

    def test_the_instrument_names_are_derived_on_access(self, session):
        # Stored at construction, this could disagree with the instrumentors
        # reset() tears down. Reaching past the property is the only way to
        # observe that it doesn't.
        assert session.instruments == ("FakeInstrumentor",)

        session._instrumentors.clear()

        assert session.instruments == ()


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
        self, use_instrumentors, recording_exporter
    ):
        use_instrumentors()
        session = argus.init("proj", exporters=[recording_exporter])
        session.provider.get_tracer("test").start_span("work").end()

        session.flush(failed=True)

        # emit carries the run's outcome; force_flush cannot, so an exporter
        # offering both is driven only through emit.
        assert recording_exporter.emit_calls == [True]
        assert recording_exporter.force_flush_count == 0

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


class TestSwallowedFailuresAreAudible:
    """An error Argus refuses to raise is still named, once per sink.

    Never crashing the host program is what makes ``exporters=`` safe to extend;
    saying nothing is what would make it undebuggable, since a sink that raises
    on every call otherwise produces no trace and no explanation. See
    ``docs/design-notes.md`` ("Swallowed errors are still audible").
    """

    def test_a_failed_force_flush_warns(self, use_instrumentors):
        use_instrumentors()
        session = argus.init("proj", exporters=[_HostileExporter()])

        with pytest.warns(RuntimeWarning, match=r"force_flush\(\) raised"):
            session.flush()

    def test_the_warning_names_the_sink_and_the_error(self, use_instrumentors):
        use_instrumentors()
        session = argus.init("proj", exporters=[_HostileExporter()])

        with pytest.warns(RuntimeWarning) as record:
            session.flush()

        # Which sink, which call, and what it raised: everything needed to find
        # the fault, since the traceback itself is gone.
        message = str(record[0].message)
        assert "_HostileExporter" in message
        assert "force_flush" in message
        assert "RuntimeError: force_flush boom" in message

    def test_the_warning_points_at_the_caller(self, use_instrumentors):
        use_instrumentors()
        session = argus.init("proj", exporters=[_HostileExporter()])

        with pytest.warns(RuntimeWarning) as record:
            session.flush()

        # Attributed to the flush() call in the caller's own code, not to a line
        # inside Argus -- which is what makes -W error tracebacks and per-module
        # warning filters useful.
        assert record[0].filename == __file__

    def test_repeat_failures_are_reported_once(self, use_instrumentors):
        use_instrumentors()
        hostile = _HostileExporter()
        session = argus.init("proj", exporters=[hostile])
        tracer = session.provider.get_tracer("test")

        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            tracer.start_span("one").end()
            session.flush()
            tracer.start_span("two").end()
            session.flush()

        reported = [w for w in recorded if "force_flush" in str(w.message)]

        # Both flushes drove the sink, but a sink that fails every time must not
        # drown the run it was meant to record.
        assert hostile.force_flush_count == 2
        assert len(reported) == 1

    def test_each_failing_sink_is_reported_separately(self, use_instrumentors):
        use_instrumentors()
        session = argus.init(
            "proj", exporters=[_HostileExporter(), _HostileExporter()]
        )

        with pytest.warns(RuntimeWarning) as record:
            session.flush()

        reported = [w for w in record if "force_flush" in str(w.message)]

        # Deduping is per sink, not per message: two broken sinks are two
        # problems to fix.
        assert len(reported) == 2

    def test_a_failed_shutdown_warns(self, use_instrumentors):
        use_instrumentors()
        argus.init("proj", exporters=[_HostileExporter()])

        with pytest.warns(RuntimeWarning, match=r"shutdown\(\) raised"):
            session_module._flush_on_exit()

    def test_a_failed_uninstrument_warns(
        self, use_instrumentors, recording_exporter
    ):
        inst = RaisingUninstrumentor()
        use_instrumentors(inst)
        argus.init("proj", exporters=[recording_exporter])

        # Worth hearing about: the next init patches a framework this reset
        # never unpatched.
        with pytest.warns(RuntimeWarning, match=r"uninstrument\(\) raised"):
            argus.reset()

        assert session_module._session is None

    def test_a_healthy_run_stays_quiet(self, use_instrumentors):
        use_instrumentors()
        session = argus.init("proj", exporters=[PlainSpanExporter()])
        session.provider.get_tracer("test").start_span("work").end()

        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            session.flush()
            session_module._flush_on_exit()
            argus.reset()

        assert [w for w in recorded if "was ignored" in str(w.message)] == []


class TestLoadDotenv:
    """The ``.env`` search ``init`` runs before it resolves anything.

    The autouse ``no_dotenv`` fixture stubs this out for the rest of the suite --
    the developer's own ``.env`` would otherwise leak into it -- so here the real
    function is called directly, through the module-level alias the stub cannot
    reach. See ``docs/design-notes.md`` ("Loading .env unconditionally").
    """

    KEY = "AEGIS_API_KEY"

    @pytest.fixture(autouse=True)
    def disposable_environ(self, monkeypatch):
        """Give each test an ``os.environ`` of its own to be written into.

        ``load_dotenv`` really does set variables, and ``monkeypatch.delenv``
        records nothing to undo for a variable that was never there, so a value
        loaded from a fixture ``.env`` would otherwise outlive the test.
        """
        monkeypatch.setattr(os, "environ", dict(os.environ))
        monkeypatch.delitem(os.environ, self.KEY, raising=False)

    def test_finds_a_dotenv_above_the_working_directory(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / ".env").write_text(f"{self.KEY}=from-parent\n")
        nested = tmp_path / "pkg" / "deep"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)

        _real_load_dotenv()

        # The search walks up from the working directory, not from wherever argus
        # is installed, which is what lets a key at the project root serve a
        # script run from a subdirectory of it.
        assert os.environ[self.KEY] == "from-parent"

    def test_never_overrides_a_variable_already_set(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / ".env").write_text(f"{self.KEY}=from-the-file\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(self.KEY, "from-the-environment")

        _real_load_dotenv()

        # An explicit export -- or a key a CI runner injected -- outranks the
        # file, so a checked-out .env can never quietly replace the credential
        # the run was launched with.
        assert os.environ[self.KEY] == "from-the-environment"


class TestOutputDirWithCustomExporters:
    """``output_dir`` only configures the default file exporter."""

    def test_warns_when_given_alongside_exporters(
        self, use_instrumentors, recording_exporter, traces_dir
    ):
        use_instrumentors()

        # It cannot be applied -- the given exporters are already built -- so
        # silence would leave the caller believing traces go somewhere they
        # don't.
        with pytest.warns(
            RuntimeWarning, match="output_dir has no effect"
        ) as record:
            argus.init(
                "proj",
                exporters=[recording_exporter],
                output_dir=traces_dir,
            )

        # Raised from sink assembly, but attributed to the caller's init() line:
        # the argument named in the message is one they passed.
        assert record[0].filename == __file__
        assert not traces_dir.exists()

    def test_no_warning_when_it_configures_the_default_exporter(
        self, use_instrumentors, traces_dir, recwarn
    ):
        use_instrumentors()

        argus.init("proj", output_dir=traces_dir)

        assert [w for w in recwarn if "output_dir" in str(w.message)] == []
        assert traces_dir.is_dir()


class TestNoInstrumentorsWarning:
    """Detection finding nothing must not stay silent."""

    def test_warns_when_auto_detection_finds_nothing(
        self, monkeypatch, recording_exporter
    ):
        patch_resolve_instrumentors(monkeypatch, [])

        # A bare init with no recognized framework otherwise installs nothing,
        # produces no spans, and writes no files -- silence would leave the
        # caller believing the one-liner worked.
        with pytest.warns(RuntimeWarning, match="no supported agent framework"):
            session = argus.init("proj", exporters=[recording_exporter])

        assert session.instruments == ()

    def test_warning_names_known_keys_and_instrument_argument(
        self, monkeypatch, recording_exporter
    ):
        patch_resolve_instrumentors(monkeypatch, [])

        with pytest.warns(RuntimeWarning) as record:
            argus.init("proj", exporters=[recording_exporter])

        message = str(record[0].message)
        assert "instrument=" in message
        assert "openai_agents" in message
        assert "claude" in message
        assert "agno" in message
        assert "openai" in message

    def test_no_warning_when_instrument_is_explicitly_empty(
        self, monkeypatch, recording_exporter, recwarn
    ):
        patch_resolve_instrumentors(monkeypatch, [])

        # instrument=[] is the documented opt-out; warning on it would punish
        # the deliberate choice the message itself recommends.
        argus.init("proj", instrument=[], exporters=[recording_exporter])

        assert [
            w
            for w in recwarn
            if "no supported agent framework" in str(w.message)
        ] == []

    def test_warning_can_be_promoted_to_error(
        self, monkeypatch, recording_exporter
    ):
        patch_resolve_instrumentors(monkeypatch, [])

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with pytest.raises(
                RuntimeWarning, match="no supported agent framework"
            ):
                argus.init("proj", exporters=[recording_exporter])

    def test_the_warning_points_at_the_caller(
        self, monkeypatch, recording_exporter
    ):
        patch_resolve_instrumentors(monkeypatch, [])

        with pytest.warns(RuntimeWarning) as record:
            argus.init("proj", exporters=[recording_exporter])

        # The init() line is where the fix goes, so that is what the warning has
        # to name. Nothing else fails when a refactor puts another frame between
        # the two, which is why the stacklevel is asserted rather than commented.
        assert record[0].filename == __file__


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

    def test_the_warning_points_at_the_second_init(
        self, use_instrumentors, recording_exporter
    ):
        use_instrumentors()
        argus.init("proj", exporters=[recording_exporter])

        with pytest.warns(RuntimeWarning) as record:
            argus.init("proj")

        # The stray second call is the one worth finding, and it is the caller's
        # line rather than anything inside Argus.
        assert record[0].filename == __file__

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
