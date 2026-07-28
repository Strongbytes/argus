"""Tests for the OTLP exporter and its wiring into :func:`argus.init`.

Mirroring the rest of the suite, these never touch the network or require the
optional ``argus-trace[otlp]`` extra: the transport (the real OpenTelemetry
OTLP exporter) is swapped for a recording fake, and only the endpoint-resolution
and missing-dependency seams exercise the real import path.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

import argus
from argus import OtlpConfig
from argus import session as session_module
from argus.exporters import BufferedOTLPExporter
from argus.exporters.otlp import _resolve_auth_headers, _resolve_endpoint

# The module whose absence means the ``otlp`` extra isn't installed.
_OTLP_MODULE = "opentelemetry.exporter.otlp.proto.http.trace_exporter"
_API_KEY_ENV = "AEGIS_API_KEY"
_AUTH_HEADER = "Authorization"
# Both standard OTel endpoint vars: the traces-specific one is a complete URL,
# the generic one a per-signal base that ``v1/traces`` is appended to.
_TRACES_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
# Shaped like a real Aegis key (``sk_`` + 32 chars) without being one; Argus
# deliberately does not validate that shape, so nothing depends on it.
_KEY = "sk_" + "a" * 32


class _RecordingTransport(SpanExporter):
    """Stand-in for the real OTLP transport: records what it's asked to send.

    Delivery behavior is configurable so failure paths can be exercised without
    a network: ``result`` is the :class:`SpanExportResult` each ``export``
    returns, and setting ``raises`` makes ``export`` raise that exception
    instead (standing in for a connection error the real transport might let
    escape rather than reporting via return value).

    ``captured`` holds the endpoint, headers and timeout the transport was built
    with, filled in by whoever installs it.
    """

    def __init__(
        self,
        result: SpanExportResult = SpanExportResult.SUCCESS,
        raises: Exception | None = None,
    ) -> None:
        self.exported: list[Any] = []
        self.captured: dict[str, Any] = {}
        self.shutdown_count = 0
        self.result = result
        self.raises = raises

    def export(self, spans) -> SpanExportResult:
        self.exported.append(list(spans))
        if self.raises is not None:
            raise self.raises
        return self.result

    def shutdown(self) -> None:
        self.shutdown_count += 1


@pytest.fixture(autouse=True)
def api_key_env(monkeypatch):
    """Give every test in this module a resolvable API key by default.

    Authentication is a construction-time requirement, but most tests here are
    about buffering and delivery rather than credentials. Supplying the key via
    the environment keeps those tests focused instead of threading a credential
    through every call site; the tests that care about resolution set or clear
    it themselves.
    """
    monkeypatch.setenv(_API_KEY_ENV, _KEY)


@pytest.fixture(autouse=True)
def clean_endpoint_env(monkeypatch):
    """Clear both OTLP endpoint vars so the suite ignores the real environment.

    Endpoint resolution reads two variables, and a developer running the suite
    on a machine configured to talk to a collector would otherwise satisfy the
    tests asserting that a missing endpoint raises. Tests that want one set it
    themselves.
    """
    monkeypatch.delenv(_TRACES_ENDPOINT_ENV, raising=False)
    monkeypatch.delenv(_ENDPOINT_ENV, raising=False)


@pytest.fixture
def install_transport(monkeypatch):
    """Return a helper that patches transport construction with a fake.

    Transport construction is the module's single seam -- patching it is what
    keeps the suite off the network and free of the ``otlp`` extra -- so every
    test goes through this one helper rather than repeating the ``setattr``.
    Delivery behavior is passed straight to :class:`_RecordingTransport`::

        transport = install_transport(result=SpanExportResult.FAILURE)
        transport = install_transport(raises=ConnectionError("refused"))

    The returned transport is the single one every ``BufferedOTLPExporter``
    built during the test will send through, and it captures the
    endpoint/headers/timeout it was asked to build with.
    """

    def _install(**behavior: Any) -> _RecordingTransport:
        transport = _RecordingTransport(**behavior)

        def fake_build(endpoint, headers, timeout):
            transport.captured.update(
                endpoint=endpoint, headers=headers, timeout=timeout
            )
            return transport

        monkeypatch.setattr("argus.exporters.otlp._build_transport", fake_build)
        return transport

    return _install


@pytest.fixture
def fake_transport(install_transport):
    """A successfully delivering transport, for the tests that assume one."""
    return install_transport()


class TestResolveEndpoint:
    ENV = _TRACES_ENDPOINT_ENV

    def test_explicit_argument_wins(self, monkeypatch):
        monkeypatch.setenv(self.ENV, "http://env:9000/v1/traces")

        assert (
            _resolve_endpoint("http://arg:9000/ingest")
            == "http://arg:9000/ingest"
        )

    def test_env_var_used_when_no_argument(self, monkeypatch):
        monkeypatch.setenv(self.ENV, "http://env:9000/v1/traces")

        assert _resolve_endpoint(None) == "http://env:9000/v1/traces"

    def test_raises_when_no_endpoint_configured(self):
        # No explicit endpoint and neither env var: Argus has no default, so
        # this is a misconfiguration we surface loudly rather than guess a
        # target. Both variables are named so the fix is obvious.
        with pytest.raises(ValueError, match=self.ENV) as excinfo:
            _resolve_endpoint(None)

        assert _ENDPOINT_ENV in str(excinfo.value)

    def test_surrounding_whitespace_is_stripped(self):
        # An endpoint read out of a file or a shell often arrives with a
        # trailing newline, which would make every POST fail.
        assert (
            _resolve_endpoint("  http://arg:9000/ingest\n")
            == "http://arg:9000/ingest"
        )

    @pytest.mark.parametrize("value", ["", "   ", "\n"])
    def test_a_blank_endpoint_raises_rather_than_falling_back(
        self, value, monkeypatch
    ):
        monkeypatch.setenv(self.ENV, "http://env:9000/v1/traces")

        # A blank string is what os.getenv("ENDPOINT", "") yields when the
        # variable is missing. Silently using the environment instead would hide
        # the slip behind an endpoint the caller never chose.
        with pytest.raises(ValueError, match="empty OTLP endpoint"):
            _resolve_endpoint(value)


class TestResolveGenericEndpoint:
    """The generic, all-signals ``OTEL_EXPORTER_OTLP_ENDPOINT`` fallback.

    It is the variable a collector deployment normally sets, and OpenTelemetry's
    own exporter honors it by appending the traces path. Argus reads the endpoint
    itself (to fail early when nothing is configured), so it has to reproduce
    that rule or it would reject a perfectly standard setup.
    """

    def test_generic_var_gets_the_traces_path_appended(self, monkeypatch):
        monkeypatch.setenv(_ENDPOINT_ENV, "http://collector:4318")

        assert _resolve_endpoint(None) == "http://collector:4318/v1/traces"

    def test_trailing_slash_does_not_double_up(self, monkeypatch):
        monkeypatch.setenv(_ENDPOINT_ENV, "http://collector:4318/")

        assert _resolve_endpoint(None) == "http://collector:4318/v1/traces"

    def test_path_is_preserved_under_a_base_route(self, monkeypatch):
        # A collector behind a path prefix: the traces route hangs off it.
        monkeypatch.setenv(_ENDPOINT_ENV, "http://gateway/otlp")

        assert _resolve_endpoint(None) == "http://gateway/otlp/v1/traces"

    def test_traces_specific_var_wins_and_is_used_verbatim(self, monkeypatch):
        monkeypatch.setenv(_ENDPOINT_ENV, "http://collector:4318")
        monkeypatch.setenv(_TRACES_ENDPOINT_ENV, "http://direct:9000/ingest")

        # The signal-specific variable is already a complete route, so nothing
        # is appended to it.
        assert _resolve_endpoint(None) == "http://direct:9000/ingest"

    def test_explicit_argument_still_wins(self, monkeypatch):
        monkeypatch.setenv(_ENDPOINT_ENV, "http://collector:4318")

        assert _resolve_endpoint("http://arg/ingest") == "http://arg/ingest"

    def test_init_accepts_a_collector_configured_the_standard_way(
        self, use_instrumentors, fake_transport, monkeypatch
    ):
        use_instrumentors()
        monkeypatch.setenv(_ENDPOINT_ENV, "http://collector:4318")

        argus.init("proj", otlp=True)

        assert (
            fake_transport.captured["endpoint"]
            == "http://collector:4318/v1/traces"
        )


class TestResolveAuthHeaders:
    """The API key -> ``Authorization: Bearer`` mapping and its guard rails."""

    def test_explicit_key_becomes_bearer_header(self, monkeypatch):
        monkeypatch.delenv(_API_KEY_ENV, raising=False)

        assert _resolve_auth_headers(_KEY, None) == {
            "Authorization": f"Bearer {_KEY}"
        }

    def test_env_var_used_when_no_argument(self):
        # The autouse fixture put the key in the environment.
        assert _resolve_auth_headers(None, None) == {
            "Authorization": f"Bearer {_KEY}"
        }

    def test_explicit_argument_wins_over_env_var(self, monkeypatch):
        monkeypatch.setenv(_API_KEY_ENV, "sk_from_env")

        headers = _resolve_auth_headers("sk_from_arg", None)

        assert headers == {"Authorization": "Bearer sk_from_arg"}

    def test_surrounding_whitespace_is_stripped(self, monkeypatch):
        # A key pasted out of a shell or .env often carries a trailing newline.
        headers = _resolve_auth_headers(f"  {_KEY}\n", None)

        assert headers == {"Authorization": f"Bearer {_KEY}"}

    def test_other_headers_are_preserved(self, monkeypatch):
        headers = _resolve_auth_headers(_KEY, {"x-tenant": "acme"})

        assert headers == {
            "x-tenant": "acme",
            "Authorization": f"Bearer {_KEY}",
        }

    def test_raises_when_no_key_anywhere(self, monkeypatch):
        monkeypatch.delenv(_API_KEY_ENV, raising=False)

        # Ingest is authenticated, so a missing key is a misconfiguration we
        # surface now rather than as a 401 at interpreter shutdown, by which
        # point the whole run's trace is unrecoverable.
        with pytest.raises(ValueError, match=_API_KEY_ENV):
            _resolve_auth_headers(None, None)

    def test_blank_key_is_treated_as_missing(self, monkeypatch):
        monkeypatch.delenv(_API_KEY_ENV, raising=False)

        with pytest.raises(ValueError, match=_API_KEY_ENV):
            _resolve_auth_headers("   ", None)

    def test_key_containing_whitespace_raises(self, monkeypatch):
        # Aegis splits the header on whitespace, so an interior space could only
        # ever come back as an opaque 401. Name the real problem instead.
        with pytest.raises(ValueError, match="whitespace"):
            _resolve_auth_headers("sk_ab cd", None)

    def test_headers_do_not_opt_out_of_the_key_requirement(self, monkeypatch):
        monkeypatch.delenv(_API_KEY_ENV, raising=False)

        # Remote export exists to reach Aegis, whose ingest is authenticated, so
        # there is no header set that makes a key optional.
        with pytest.raises(ValueError, match=_API_KEY_ENV):
            _resolve_auth_headers(None, {"x-api-key": "other"})

    def test_key_overwriting_a_given_authorization_header_raises(self):
        # The key is going to win, so the header the caller wrote would vanish
        # silently. Say so instead.
        with pytest.raises(ValueError, match=_AUTH_HEADER):
            _resolve_auth_headers(_KEY, {"Authorization": "Bearer other"})

    def test_conflict_detection_ignores_header_name_case(self):
        # HTTP header names are case-insensitive, so the clash is real however
        # the caller spelled it.
        with pytest.raises(ValueError, match=_AUTH_HEADER):
            _resolve_auth_headers(_KEY, {"authorization": "Bearer other"})


class TestMissingDependency:
    def test_construction_raises_actionable_error(self, monkeypatch):
        # Simulate the extra not being installed: a None entry in sys.modules
        # makes the deferred import fail, just like a genuinely absent package.
        monkeypatch.setitem(sys.modules, _OTLP_MODULE, None)

        with pytest.raises(ImportError, match=r"argus-trace\[otlp\]"):
            BufferedOTLPExporter("http://localhost:9000/v1/traces")


class TestBufferAndEmit:
    def test_export_buffers_without_sending(self, fake_transport):
        exporter = BufferedOTLPExporter("http://localhost:9000/ingest")

        exporter.export(["span-a", "span-b"])
        exporter.export(["span-c"])

        # Nothing sent yet -- spans are only buffered until emit.
        assert fake_transport.exported == []

    def test_emit_posts_all_buffered_spans_in_one_request(self, fake_transport):
        exporter = BufferedOTLPExporter("http://localhost:9000/ingest")
        exporter.export(["span-a", "span-b"])
        exporter.export(["span-c"])

        exporter.emit(failed=False)

        # A single request carrying every buffered span across the run.
        assert fake_transport.exported == [["span-a", "span-b", "span-c"]]

    def test_emit_with_empty_buffer_sends_nothing(self, fake_transport):
        exporter = BufferedOTLPExporter("http://localhost:9000/ingest")

        exporter.emit()

        assert fake_transport.exported == []

    def test_emit_is_a_noop_the_second_time(self, fake_transport):
        exporter = BufferedOTLPExporter("http://localhost:9000/ingest")
        exporter.export(["span-a"])

        exporter.emit()
        exporter.emit()

        # A confirmed send clears the buffer, so the second emit has nothing
        # left to post -- a re-POST would duplicate the run's spans.
        assert fake_transport.exported == [["span-a"]]

    def test_endpoint_is_resolved_and_forwarded_to_transport(
        self, fake_transport
    ):
        BufferedOTLPExporter("http://localhost:9000/api/v1/trace/ingest")

        assert (
            fake_transport.captured["endpoint"]
            == "http://localhost:9000/api/v1/trace/ingest"
        )

    def test_shutdown_releases_transport(self, fake_transport):
        exporter = BufferedOTLPExporter("http://localhost:9000/ingest")

        exporter.shutdown()

        assert fake_transport.shutdown_count == 1


class TestAuthWiring:
    """The resolved credential reaches the transport -- and nowhere else."""

    def test_key_is_forwarded_to_the_transport(self, fake_transport):
        BufferedOTLPExporter("http://localhost:9000/ingest", api_key=_KEY)

        assert fake_transport.captured["headers"] == {
            "Authorization": f"Bearer {_KEY}"
        }

    def test_headers_are_always_passed_to_the_transport(self, fake_transport):
        # No headers argument, no api_key argument -- and the transport is still
        # handed an explicit mapping. That is what keeps the transport's own
        # OTEL_EXPORTER_OTLP_*_HEADERS variables permanently inert: it reads
        # them only when it is given no headers at all.
        BufferedOTLPExporter("http://localhost:9000/ingest")

        assert fake_transport.captured["headers"] == {
            _AUTH_HEADER: f"Bearer {_KEY}"
        }

    def test_extra_headers_ride_alongside_the_credential(self, fake_transport):
        # The headers argument is the documented way to add headers, precisely
        # because the environment variables cannot be.
        BufferedOTLPExporter(
            "http://localhost:9000/ingest", headers={"x-tenant": "acme"}
        )

        assert fake_transport.captured["headers"] == {
            "x-tenant": "acme",
            _AUTH_HEADER: f"Bearer {_KEY}",
        }

    def test_construction_raises_when_no_key_is_available(self, monkeypatch):
        monkeypatch.delenv(_API_KEY_ENV, raising=False)

        # No transport fixture: the headers are resolved on the way into
        # ``_build_transport``, so this fails before there is a transport to
        # fake.
        with pytest.raises(ValueError, match=_API_KEY_ENV):
            BufferedOTLPExporter("http://localhost:9000/ingest")

    def test_key_is_not_kept_on_the_exporter(self, fake_transport):
        exporter = BufferedOTLPExporter(
            "http://localhost:9000/ingest", api_key=_KEY
        )

        # The credential lives in the transport's headers only, so it cannot
        # surface through an attribute dump or a repr.
        assert _KEY not in repr(vars(exporter))

    def test_delivery_failure_warning_does_not_leak_the_key(
        self, install_transport
    ):
        install_transport(raises=ConnectionError("refused"))
        exporter = BufferedOTLPExporter(
            "http://localhost:9000/ingest", api_key=_KEY
        )
        exporter.export(["span-a"])

        with pytest.warns(RuntimeWarning) as recorded:
            exporter.emit()

        assert _KEY not in str(recorded[0].message)


class TestDeliveryFailureIsReportedNotFatal:
    """A backend failure must warn (never crash) and keep the spans buffered.

    Argus is a side-channel, so a down/rejecting backend can't be allowed to
    crash the run -- but a silent drop is just as bad with an emit-once model,
    so the failure surfaces as a ``RuntimeWarning`` and the buffer is emptied
    only on a confirmed success.
    """

    def test_rejected_batch_warns_and_keeps_buffer(self, install_transport):
        transport = install_transport(result=SpanExportResult.FAILURE)
        exporter = BufferedOTLPExporter("http://localhost:9000/ingest")
        exporter.export(["span-a"])

        with pytest.warns(RuntimeWarning, match="rejected"):
            exporter.emit()

        # Failure attempted the send but left the spans intact: a later
        # successful emit can still deliver them.
        assert transport.exported == [["span-a"]]
        transport.result = SpanExportResult.SUCCESS
        exporter.emit()
        assert transport.exported == [["span-a"], ["span-a"]]

    def test_transport_raise_is_caught_warns_and_keeps_buffer(
        self, install_transport
    ):
        transport = install_transport(raises=ConnectionError("refused"))
        exporter = BufferedOTLPExporter("http://localhost:9000/ingest")
        exporter.export(["span-a"])

        # The raise must not propagate (it would crash atexit or, via the
        # context manager, mask the user's own exception).
        with pytest.warns(RuntimeWarning, match="ConnectionError"):
            exporter.emit()

        transport.raises = None
        transport.result = SpanExportResult.SUCCESS
        exporter.emit()
        # The spans survived the failed attempt and were delivered on retry.
        assert transport.exported[-1] == ["span-a"]


class TestInitOtlpIntegration:
    """Remote export as ``init`` assembles it, alongside the on-disk default.

    None of these pass ``output_dir``: the default file sink writes under the
    working directory, which the autouse ``disposable_working_directory`` fixture
    has already pointed somewhere throwaway.
    """

    @pytest.fixture(autouse=True)
    def noop_instrumentor(self, use_instrumentors):
        """Install a single no-op instrumentor for every test in this class.

        These tests are about how ``init`` wires up remote export, never about
        detection, so each one only needs *some* instrumentor present to dodge
        the zero-instrumentor warning -- exactly what a bare ``use_instrumentors()``
        supplies.
        """
        use_instrumentors()

    def test_otlp_true_appends_exporter_alongside_file(
        self, fake_transport, monkeypatch
    ):
        monkeypatch.setenv(
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://env:9000/v1/traces"
        )

        session = argus.init("proj", otlp=True)

        # OTLP rides alongside the default on-disk exporter, not instead of it.
        kinds = [type(e).__name__ for e in session.exporters]
        assert "BufferedOTLPExporter" in kinds
        assert "FileSpanExporter" in kinds
        # True means "resolve the endpoint from the standard OTel env var".
        assert (
            fake_transport.captured["endpoint"] == "http://env:9000/v1/traces"
        )

    def test_otlp_true_without_endpoint_raises(self):
        # otlp=True with no endpoint anywhere is a misconfiguration: fail loudly
        # at init rather than silently posting to a guessed target. (The autouse
        # clean_endpoint_env fixture cleared both endpoint variables.) No
        # transport fixture: the endpoint is resolved on the way into
        # ``_build_transport``, so this fails before there is one to fake.
        with pytest.raises(ValueError, match="OTLP endpoint"):
            argus.init("proj", otlp=True)

    def test_config_endpoint_is_forwarded(self, fake_transport):
        argus.init(
            "proj",
            otlp=OtlpConfig("http://localhost:9000/api/v1/trace/ingest"),
        )

        assert (
            fake_transport.captured["endpoint"]
            == "http://localhost:9000/api/v1/trace/ingest"
        )

    def test_config_headers_and_timeout_reach_the_transport(
        self, fake_transport
    ):
        # The settings that used to need a hand-built exporter passed through
        # ``exporters=`` now ride on the config, so remote export stays a
        # one-argument decision even when customized. The timeout is fractional
        # because the transport takes float seconds, so sub-second budgets have
        # to survive the pass-through intact.
        argus.init(
            "proj",
            otlp=OtlpConfig(
                "http://localhost:9000/ingest",
                headers={"x-tenant": "acme"},
                timeout=0.5,
            ),
        )

        assert fake_transport.captured["timeout"] == 0.5
        assert fake_transport.captured["headers"] == {
            "x-tenant": "acme",
            _AUTH_HEADER: f"Bearer {_KEY}",
        }

    def test_a_blank_config_endpoint_raises(self):
        # Rather than falling back to the environment behind the caller's back
        # (see TestResolveEndpoint), which is the trap otlp="" used to be.
        with pytest.raises(ValueError, match="empty OTLP endpoint"):
            argus.init("proj", otlp=OtlpConfig(""))

    def test_flush_emits_buffered_spans_to_the_backend(
        self, fake_transport, monkeypatch
    ):
        monkeypatch.setenv(
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://env:9000/v1/traces"
        )
        session = argus.init("proj", otlp=True)

        span = session.provider.get_tracer("test").start_span("work")
        span.end()
        session.flush()

        # Exactly one request, carrying the one span the run produced.
        assert len(fake_transport.exported) == 1
        assert len(fake_transport.exported[0]) == 1

    def test_config_api_key_is_forwarded_to_the_otlp_exporter(
        self, fake_transport, monkeypatch
    ):
        monkeypatch.delenv(_API_KEY_ENV, raising=False)

        argus.init(
            "proj",
            otlp=OtlpConfig(
                "http://localhost:9000/api/v1/trace/ingest", api_key=_KEY
            ),
        )

        assert fake_transport.captured["headers"] == {
            "Authorization": f"Bearer {_KEY}"
        }

    def test_api_key_read_from_the_environment(self, fake_transport):
        # No key in the config: the whole point of the env fallback is that a
        # bare init still authenticates.
        argus.init("proj", otlp=OtlpConfig("http://localhost:9000/ingest"))

        assert fake_transport.captured["headers"] == {
            "Authorization": f"Bearer {_KEY}"
        }

    def test_otlp_without_any_api_key_raises(self, monkeypatch):
        monkeypatch.delenv(_API_KEY_ENV, raising=False)

        with pytest.raises(ValueError, match=_API_KEY_ENV):
            argus.init("proj", otlp=OtlpConfig("http://localhost:9000/ingest"))


class TestOtlpArgumentShape:
    """``otlp`` is the only remote-export argument, and it is typed.

    Every remote setting lives on :class:`OtlpConfig`, so there is no way to
    spell a key, header or timeout that has no endpoint to use it -- the
    combination a runtime warning used to have to explain.
    """

    @pytest.fixture(autouse=True)
    def noop_instrumentor(self, use_instrumentors):
        """Install a single no-op instrumentor for every test in this class.

        These tests are about the shape of the ``otlp`` argument, never about
        detection, so each one only needs *some* instrumentor present to dodge
        the zero-instrumentor warning -- exactly what a bare ``use_instrumentors()``
        supplies.
        """
        use_instrumentors()

    def test_init_takes_no_standalone_api_key(self):
        with pytest.raises(TypeError, match="api_key"):
            argus.init("proj", api_key=_KEY)

    def test_an_endpoint_string_names_its_replacement(self):
        # The pre-config spelling. It has to fail loudly rather than be read as
        # a truthy "on", which would silently ignore the endpoint given. The
        # argument is validated before init creates anything at all, so there is
        # nothing here to point at a temporary directory or a fake transport.
        with pytest.raises(TypeError, match="OtlpConfig") as excinfo:
            argus.init("proj", otlp="http://localhost:9000/ingest")

        assert "http://localhost:9000/ingest" in str(excinfo.value)

    def test_an_unsupported_type_is_rejected(self):
        with pytest.raises(TypeError, match="otlp=True"):
            argus.init("proj", otlp=42)

    @pytest.mark.parametrize("value", [None, False])
    def test_off_values_construct_nothing(
        self, value, recording_exporter, monkeypatch
    ):
        def boom(*_a, **_k):
            raise AssertionError("OTLP should not be constructed when off")

        monkeypatch.setattr(session_module, "BufferedOTLPExporter", boom)

        # ``None`` is also the default, so this covers a bare init too: remote
        # export costs nothing at all until something asks for it.
        session = argus.init("proj", otlp=value, exporters=[recording_exporter])

        assert session.exporters == (recording_exporter,)

    def test_a_bare_config_matches_otlp_true(self, fake_transport, monkeypatch):
        monkeypatch.setenv(_TRACES_ENDPOINT_ENV, "http://env:9000/v1/traces")

        argus.init("proj", otlp=OtlpConfig())

        # OtlpConfig() and True say the same thing: on, everything from the
        # environment.
        assert (
            fake_transport.captured["endpoint"] == "http://env:9000/v1/traces"
        )

    def test_a_bad_argument_creates_nothing(self, traces_dir):
        with pytest.raises(TypeError):
            argus.init("proj", otlp=42, output_dir=traces_dir)

        # The one place output_dir is still worth passing: it names the directory
        # this asserts was never created. Validated before any provider, exporter
        # or directory exists, so a rejected call leaves nothing behind to clean
        # up -- including the singleton, which a later init still needs to claim.
        assert not traces_dir.exists()
        assert session_module._session is None
