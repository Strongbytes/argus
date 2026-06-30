"""Tests for :class:`argus.exporters.file.FileSpanExporter`."""

from __future__ import annotations

import json

from argus.exporters.file import FileSpanExporter

from tests.factories import make_span


def _load_all(traces_dir):
    """Return ``{path: parsed_json}`` for every trace file written."""
    return {
        path: json.loads(path.read_text())
        for path in sorted(traces_dir.iterdir())
    }


class TestWriteToDisk:
    def test_one_file_per_trace_grouping_spans(self, traces_dir):
        exporter = FileSpanExporter(traces_dir, script_name="myscript")
        exporter.export(
            [
                make_span(trace_id=1, name="a"),
                make_span(trace_id=1, name="b"),
                make_span(trace_id=2, name="c"),
            ]
        )

        exporter.write_to_disk(failed=False)

        traces = _load_all(traces_dir)
        assert len(traces) == 2
        for path in traces:
            assert path.name.endswith(".json")
            assert "myscript" in path.name
            assert ".error" not in path.name
        span_counts = sorted(len(spans) for spans in traces.values())
        assert span_counts == [1, 2]

    def test_failure_is_tagged_in_the_filename(self, traces_dir):
        exporter = FileSpanExporter(traces_dir, script_name="myscript")
        exporter.export([make_span(trace_id=7, name="x")])

        exporter.write_to_disk(failed=True)

        (path,) = list(traces_dir.iterdir())
        assert path.name.endswith(".error.json")

    def test_embedded_json_is_expanded(self, traces_dir):
        exporter = FileSpanExporter(traces_dir, script_name="s")
        exporter.export([make_span(trace_id=1, output='{"k": 1}')])

        exporter.write_to_disk()

        (spans,) = _load_all(traces_dir).values()
        assert spans[0]["output"] == {"k": 1}


class TestMisc:
    def test_creates_base_dir_with_parents(self, tmp_path):
        nested = tmp_path / "deeply" / "nested" / "traces"

        FileSpanExporter(nested, script_name="s")

        assert nested.is_dir()

    def test_force_flush_and_shutdown_are_noops(self, traces_dir):
        exporter = FileSpanExporter(traces_dir, script_name="s")

        assert exporter.force_flush() is True
        assert exporter.shutdown() is None
