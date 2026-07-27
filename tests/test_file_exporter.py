"""Tests for :class:`argus.exporters.file.FileSpanExporter`."""

from __future__ import annotations

import json
import re

from argus.exporters.file import FileSpanExporter

from tests.factories import make_span


def _load_all(traces_dir):
    """Return ``{path: parsed_json}`` for every trace file written."""
    return {
        path: json.loads(path.read_text())
        for path in sorted(traces_dir.iterdir())
    }


def _spans_of(request):
    """Flatten every span in an OTLP ``ExportTraceServiceRequest`` dict."""
    return [
        span
        for resource_spans in request.get("resourceSpans", [])
        for scope_spans in resource_spans.get("scopeSpans", [])
        for span in scope_spans.get("spans", [])
    ]


class TestEmit:
    def test_files_are_otlp_export_requests(self, traces_dir):
        exporter = FileSpanExporter(traces_dir, script_name="s")
        exporter.export([make_span(trace_id=1, name="a")])

        exporter.emit()

        (request,) = _load_all(traces_dir).values()
        # The canonical OTLP/JSON envelope, not a bare list of spans.
        assert "resourceSpans" in request
        assert [span["name"] for span in _spans_of(request)] == ["a"]

    def test_one_file_per_trace_grouping_spans(self, traces_dir):
        exporter = FileSpanExporter(traces_dir, script_name="myscript")
        exporter.export(
            [
                make_span(trace_id=1, name="a"),
                make_span(trace_id=1, name="b"),
                make_span(trace_id=2, name="c"),
            ]
        )

        exporter.emit(failed=False)

        traces = _load_all(traces_dir)
        assert len(traces) == 2
        for path in traces:
            assert path.name.endswith(".json")
            assert "myscript" in path.name
            assert ".error" not in path.name
        span_counts = sorted(
            len(_spans_of(request)) for request in traces.values()
        )
        assert span_counts == [1, 2]

    def test_trace_and_span_ids_are_hex_not_base64(self, traces_dir):
        exporter = FileSpanExporter(traces_dir, script_name="s")
        exporter.export([make_span(trace_id=1, name="a")])

        exporter.emit()

        (request,) = _load_all(traces_dir).values()
        (span,) = _spans_of(request)
        # OTLP/JSON's one departure from proto3 JSON: ids are hex, not base64.
        assert span["traceId"] == f"{1:032x}"
        assert re.fullmatch(r"[0-9a-f]{16}", span["spanId"])

    def test_failure_is_tagged_in_the_filename(self, traces_dir):
        exporter = FileSpanExporter(traces_dir, script_name="myscript")
        exporter.export([make_span(trace_id=7, name="x")])

        exporter.emit(failed=True)

        (path,) = list(traces_dir.iterdir())
        assert path.name.endswith(".error.json")

    def test_attributes_are_encoded_in_otlp_shape(self, traces_dir):
        exporter = FileSpanExporter(traces_dir, script_name="s")
        exporter.export([make_span(trace_id=1, name="a", output='{"k": 1}')])

        exporter.emit()

        (request,) = _load_all(traces_dir).values()
        (span,) = _spans_of(request)
        # OTLP keeps attributes as a typed key/value list (no readability
        # expansion): the value rides through verbatim as a stringValue.
        assert span["attributes"] == [
            {"key": "output", "value": {"stringValue": '{"k": 1}'}}
        ]

    def test_spans_written_in_generation_order(self, traces_dir):
        # Spans arrive end-time-first (leaf before the parent that wraps it),
        # so the parent -- started first -- shows up last on the wire.
        exporter = FileSpanExporter(traces_dir, script_name="s")
        exporter.export(
            [
                make_span(trace_id=1, name="leaf", start_time=500),
                make_span(trace_id=1, name="root", start_time=100),
            ]
        )

        exporter.emit()

        (request,) = _load_all(traces_dir).values()
        assert [span["name"] for span in _spans_of(request)] == [
            "root",
            "leaf",
        ]

    def test_spans_without_start_time_keep_arrival_order(self, traces_dir):
        exporter = FileSpanExporter(traces_dir, script_name="s")
        exporter.export(
            [
                make_span(trace_id=1, name="first"),
                make_span(trace_id=1, name="second"),
            ]
        )

        exporter.emit()

        (request,) = _load_all(traces_dir).values()
        assert [span["name"] for span in _spans_of(request)] == [
            "first",
            "second",
        ]


class TestMisc:
    def test_creates_base_dir_with_parents(self, tmp_path):
        nested = tmp_path / "deeply" / "nested" / "traces"

        FileSpanExporter(nested, script_name="s")

        assert nested.is_dir()

    def test_force_flush_and_shutdown_are_noops(self, traces_dir):
        exporter = FileSpanExporter(traces_dir, script_name="s")

        assert exporter.force_flush() is True
        assert exporter.shutdown() is None
