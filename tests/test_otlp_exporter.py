"""Tests for the OTLP exporter and its wiring into :func:`argus.init`.

Mirroring the rest of the suite, these never touch the network or require the
optional ``argus-trace[otlp]`` extra: the transport (the real OpenTelemetry
OTLP exporter) is swapped for a recording fake, and only the endpoint-resolution
and missing-dependency seams exercise the real import path.
"""

from __future__ import annotations

import sys
from typing import Any, List

import pytest
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

import argus
from argus import session as session_module
from argus.exporters.otlp import (
    OTLPSpanExporter,
    _resolve_endpoint,
    make_otlp_exporter,
)

from tests.factories import make_instrumentor

# The module whose absence means the ``otlp`` extra isn't installed.
_OTLP_MODULE = "opentelemetry.exporter.otlp.proto.http.trace_exporter"


class _RecordingTransport(SpanExporter):
    """Stand-in for the real OTLP transport: records what it's asked to send."""

    def __init__(self) -> None:
        self.exported: List[Any] = []
        self.shutdown_count = 0

    def export(self, spans) -> SpanExportResult:
        self.exported.append(list(spans))
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self.shutdown_count += 1


@pytest.fixture
def fake_transport(monkeypatch):
    """Patch transport construction so no real OTLP dependency is needed.

    Returns the single :class:`_RecordingTransport` every ``OTLPSpanExporter``
    built during the test will send through, and captures the endpoint/headers
    /timeout it was asked to build with.
    """
    transport = _RecordingTransport()
    captured: dict = {}

    def fake_build(endpoint, headers, timeout):
        captured["endpoint"] = endpoint
        captured["headers"] = headers
        captured["timeout"] = timeout
        return transport

    monkeypatch.setattr("argus.exporters.otlp._build_transport", fake_build)
    transport.captured = captured
    return transport


class TestResolveEndpoint:
    ENV = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"

    def test_explicit_argument_wins(self, monkeypatch):
        monkeypatch.setenv(self.ENV, "http://env:9000/v1/traces")

        assert (
            _resolve_endpoint("http://arg:9000/ingest")
            == "http://arg:9000/ingest"
        )

    def test_env_var_used_when_no_argument(self, monkeypatch):
        monkeypatch.setenv(self.ENV, "http://env:9000/v1/traces")

        assert _resolve_endpoint(None) == "http://env:9000/v1/traces"

    def test_raises_when_no_endpoint_configured(self, monkeypatch):
        # No explicit endpoint and no env var: Argus has no default, so this
        # is a misconfiguration we surface loudly rather than guessing a target.
        monkeypatch.delenv(self.ENV, raising=False)

        with pytest.raises(ValueError, match=self.ENV):
            _resolve_endpoint(None)


class TestMissingDependency:
    def test_construction_raises_actionable_error(self, monkeypatch):
        # Simulate the extra not being installed: a None entry in sys.modules
        # makes the deferred import fail, just like a genuinely absent package.
        monkeypatch.setitem(sys.modules, _OTLP_MODULE, None)

        with pytest.raises(ImportError, match=r"argus-trace\[otlp\]"):
            make_otlp_exporter("http://localhost:9000/v1/traces")


class TestBufferAndEmit:
    def test_symmetry_with_file_exporter_hook(self, fake_transport):
        # The whole point: OTLP exposes the same on-exit hook as the file sink.
        exporter = OTLPSpanExporter("http://localhost:9000/ingest")

        assert callable(getattr(exporter, "emit", None))

    def test_export_buffers_without_sending(self, fake_transport):
        exporter = OTLPSpanExporter("http://localhost:9000/ingest")

        exporter.export(["span-a", "span-b"])
        exporter.export(["span-c"])

        # Nothing sent yet -- spans are only buffered until emit.
        assert fake_transport.exported == []

    def test_emit_posts_all_buffered_spans_in_one_request(self, fake_transport):
        exporter = OTLPSpanExporter("http://localhost:9000/ingest")
        exporter.export(["span-a", "span-b"])
        exporter.export(["span-c"])

        exporter.emit(failed=False)

        # A single request carrying every buffered span across the run.
        assert fake_transport.exported == [["span-a", "span-b", "span-c"]]

    def test_emit_with_empty_buffer_sends_nothing(self, fake_transport):
        exporter = OTLPSpanExporter("http://localhost:9000/ingest")

        exporter.emit()

        assert fake_transport.exported == []

    def test_emit_is_a_noop_the_second_time(self, fake_transport):
        exporter = OTLPSpanExporter("http://localhost:9000/ingest")
        exporter.export(["span-a"])

        exporter.emit()
        exporter.emit()

        assert fake_transport.exported == [["span-a"]]

    def test_endpoint_is_resolved_and_forwarded_to_transport(
        self, fake_transport
    ):
        OTLPSpanExporter("http://localhost:9000/api/v1/trace/ingest")

        assert (
            fake_transport.captured["endpoint"]
            == "http://localhost:9000/api/v1/trace/ingest"
        )

    def test_shutdown_releases_transport(self, fake_transport):
        exporter = OTLPSpanExporter("http://localhost:9000/ingest")

        exporter.shutdown()

        assert fake_transport.shutdown_count == 1


class TestInitOtlpIntegration:
    def test_otlp_true_appends_exporter_alongside_file(
        self, use_instrumentors, fake_transport, traces_dir, monkeypatch
    ):
        use_instrumentors(make_instrumentor())
        monkeypatch.setenv(
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://env:9000/v1/traces"
        )

        session = argus.init(
            "proj", otlp=True, output_dir=traces_dir, load_dotenv=False
        )

        # OTLP rides alongside the default on-disk exporter, not instead of it.
        kinds = [type(e).__name__ for e in session.exporters]
        assert "OTLPSpanExporter" in kinds
        assert "FileSpanExporter" in kinds
        # True means "resolve the endpoint from the standard OTel env var".
        assert (
            fake_transport.captured["endpoint"] == "http://env:9000/v1/traces"
        )

    def test_otlp_true_without_endpoint_raises(
        self, use_instrumentors, fake_transport, traces_dir, monkeypatch
    ):
        use_instrumentors(make_instrumentor())
        monkeypatch.delenv(
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False
        )

        # otlp=True with no endpoint anywhere is a misconfiguration: fail loudly
        # at init rather than silently posting to a guessed target.
        with pytest.raises(ValueError, match="OTLP endpoint"):
            argus.init(
                "proj", otlp=True, output_dir=traces_dir, load_dotenv=False
            )

    def test_otlp_string_forwards_endpoint(
        self, use_instrumentors, fake_transport, traces_dir
    ):
        use_instrumentors(make_instrumentor())

        argus.init(
            "proj",
            otlp="http://localhost:9000/api/v1/trace/ingest",
            output_dir=traces_dir,
            load_dotenv=False,
        )

        assert (
            fake_transport.captured["endpoint"]
            == "http://localhost:9000/api/v1/trace/ingest"
        )

    def test_flush_emits_buffered_spans_to_the_backend(
        self, use_instrumentors, fake_transport, traces_dir, monkeypatch
    ):
        use_instrumentors(make_instrumentor())
        monkeypatch.setenv(
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://env:9000/v1/traces"
        )
        session = argus.init(
            "proj", otlp=True, output_dir=traces_dir, load_dotenv=False
        )

        span = session.provider.get_tracer("test").start_span("work")
        span.end()
        session.flush()

        # Exactly one request, carrying the one span the run produced.
        assert len(fake_transport.exported) == 1
        assert len(fake_transport.exported[0]) == 1

    def test_otlp_off_by_default(
        self, use_instrumentors, recording_exporter, monkeypatch, traces_dir
    ):
        use_instrumentors(make_instrumentor())

        def boom(*a, **k):
            raise AssertionError("OTLP should not be constructed when off")

        monkeypatch.setattr(session_module, "make_otlp_exporter", boom)

        session = argus.init(
            "proj",
            exporters=[recording_exporter],
            output_dir=traces_dir,
            load_dotenv=False,
        )

        assert session.exporters == [recording_exporter]
