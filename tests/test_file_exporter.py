"""Tests for :class:`argus.exporters.file.FileSpanExporter`."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from argus.exporters import file as file_module
from argus.exporters.file import FileSpanExporter, trace_filename

from tests.factories import make_span

_STAMP = datetime(2026, 1, 31, 14, 5, 9, tzinfo=timezone.utc)


class _FrozenClock(datetime):
    """A clock stuck on one instant, so the collision path is deterministic."""

    @classmethod
    def now(cls, tz=None):
        return _STAMP


def _load(traces_dir, suffix):
    """Return ``{path: parsed_json}`` for every trace file ending in ``suffix``."""
    return {
        path: json.loads(path.read_text())
        for path in sorted(traces_dir.iterdir())
        if path.name.endswith(suffix)
    }


def _load_otlp(traces_dir):
    """Return ``{path: parsed_json}`` for every OTLP file written."""
    return _load(traces_dir, ".otlp.json")


def _load_readable(traces_dir):
    """Return ``{path: parsed_json}`` for every human-readable file written."""
    return _load(traces_dir, ".readable.json")


def _spans_of(request):
    """Flatten every span in an OTLP ``ExportTraceServiceRequest`` dict."""
    return [
        span
        for resource_spans in request.get("resourceSpans", [])
        for scope_spans in resource_spans.get("scopeSpans", [])
        for span in scope_spans.get("spans", [])
    ]


class TestTraceFilename:
    """The naming scheme the README documents, exercised directly.

    Every part of a trace's name -- stamp, script, outcome, tiebreaker, format --
    is assembled by this one function, so the scheme can be asserted without an
    exporter, a temporary directory, or a wait for the clock to tick over.
    """

    def test_the_documented_shape(self):
        assert (
            trace_filename("my_agent", "otlp", timestamp=_STAMP)
            == "2026-01-31_14-05-09_my_agent.otlp.json"
        )

    def test_the_two_formats_differ_only_in_their_marker(self):
        otlp = trace_filename("my_agent", "otlp", timestamp=_STAMP)
        readable = trace_filename("my_agent", "readable", timestamp=_STAMP)

        assert otlp.removesuffix(".otlp.json") == readable.removesuffix(
            ".readable.json"
        )

    def test_failure_is_marked_before_the_format(self):
        # ``.error`` ahead of the format marker keeps the date-first sort intact
        # and makes a crashed run obvious in a listing.
        name = trace_filename(
            "my_agent", "readable", failed=True, timestamp=_STAMP
        )

        assert name == "2026-01-31_14-05-09_my_agent.error.readable.json"

    def test_a_sequence_disambiguates_traces_from_the_same_second(self):
        name = trace_filename(
            "s", "otlp", failed=True, timestamp=_STAMP, sequence=2
        )

        assert name == "2026-01-31_14-05-09_s.error_2.otlp.json"

    def test_the_first_trace_of_a_second_carries_no_tiebreaker(self):
        numbered = trace_filename("s", "otlp", timestamp=_STAMP, sequence=0)

        assert numbered == trace_filename("s", "otlp", timestamp=_STAMP)

    def test_an_aware_timestamp_is_rendered_in_utc(self):
        # Same instant as _STAMP, three hours east: the name must not shift with
        # the caller's timezone, or a listing stops sorting chronologically.
        elsewhere = _STAMP.astimezone(timezone(timedelta(hours=3)))
        in_utc = trace_filename("s", "otlp", timestamp=_STAMP)

        assert trace_filename("s", "otlp", timestamp=elsewhere) == in_utc

    def test_a_naive_timestamp_is_taken_as_utc(self):
        naive = _STAMP.replace(tzinfo=None)
        in_utc = trace_filename("s", "otlp", timestamp=_STAMP)

        assert trace_filename("s", "otlp", timestamp=naive) == in_utc

    def test_names_sort_chronologically(self):
        later = trace_filename("s", "otlp", timestamp=_STAMP)
        earlier = trace_filename(
            "s", "otlp", timestamp=_STAMP.replace(year=2025)
        )

        assert sorted([later, earlier]) == [earlier, later]

    def test_the_stamp_defaults_to_now(self):
        before = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
        name = trace_filename("s", "otlp")
        after = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")

        # Read on both sides, so a tick mid-call can't fail the assertion.
        assert name.startswith(before) or name.startswith(after)


class TestBothFormats:
    def test_each_trace_writes_an_otlp_and_a_readable_file(self, traces_dir):
        exporter = FileSpanExporter(traces_dir, script_name="s")
        exporter.export([make_span(trace_id=1, name="a")])

        exporter.emit()

        # One pair per trace, sharing a stem, differing only by format marker.
        otlp = _load_otlp(traces_dir)
        readable = _load_readable(traces_dir)
        assert len(otlp) == 1
        assert len(readable) == 1
        (otlp_path,) = otlp
        (readable_path,) = readable
        assert otlp_path.name.removesuffix(
            ".otlp.json"
        ) == readable_path.name.removesuffix(".readable.json")


class TestOtlpFormat:
    def test_files_are_otlp_export_requests(self, traces_dir):
        exporter = FileSpanExporter(traces_dir, script_name="s")
        exporter.export([make_span(trace_id=1, name="a")])

        exporter.emit()

        (request,) = _load_otlp(traces_dir).values()
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

        traces = _load_otlp(traces_dir)
        assert len(traces) == 2
        for path in traces:
            assert path.name.endswith(".otlp.json")
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

        (request,) = _load_otlp(traces_dir).values()
        (span,) = _spans_of(request)
        # OTLP/JSON's one departure from proto3 JSON: ids are hex, not base64.
        assert span["traceId"] == f"{1:032x}"
        assert re.fullmatch(r"[0-9a-f]{16}", span["spanId"])

    def test_attributes_are_encoded_in_otlp_shape(self, traces_dir):
        exporter = FileSpanExporter(traces_dir, script_name="s")
        exporter.export([make_span(trace_id=1, name="a", output='{"k": 1}')])

        exporter.emit()

        (request,) = _load_otlp(traces_dir).values()
        (span,) = _spans_of(request)
        # OTLP keeps attributes as a typed key/value list (no readability
        # expansion): the value rides through verbatim as a stringValue.
        assert span["attributes"] == [
            {"key": "output", "value": {"stringValue": '{"k": 1}'}}
        ]

    def test_otlp_spans_written_in_generation_order(self, traces_dir):
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

        (request,) = _load_otlp(traces_dir).values()
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

        (request,) = _load_otlp(traces_dir).values()
        assert [span["name"] for span in _spans_of(request)] == [
            "first",
            "second",
        ]


class TestReadableFormat:
    def test_file_is_a_plain_list_of_spans(self, traces_dir):
        exporter = FileSpanExporter(traces_dir, script_name="s")
        exporter.export(
            [
                make_span(trace_id=1, name="a"),
                make_span(trace_id=1, name="b"),
            ]
        )

        exporter.emit()

        (spans,) = _load_readable(traces_dir).values()
        # A bare JSON array of spans, not the OTLP envelope.
        assert isinstance(spans, list)
        assert {span["name"] for span in spans} == {"a", "b"}

    def test_embedded_json_attributes_are_expanded(self, traces_dir):
        exporter = FileSpanExporter(traces_dir, script_name="s")
        exporter.export([make_span(trace_id=1, name="a", output='{"k": 1}')])

        exporter.emit()

        (spans,) = _load_readable(traces_dir).values()
        (span,) = spans
        # Readability win: the escaped JSON string is parsed back to an object.
        assert span["attributes"]["output"] == {"k": 1}

    def test_readable_spans_written_in_generation_order(self, traces_dir):
        exporter = FileSpanExporter(traces_dir, script_name="s")
        exporter.export(
            [
                make_span(trace_id=1, name="leaf", start_time=500),
                make_span(trace_id=1, name="root", start_time=100),
            ]
        )

        exporter.emit()

        (spans,) = _load_readable(traces_dir).values()
        assert [span["name"] for span in spans] == ["root", "leaf"]


class TestFailureTagging:
    def test_failure_is_tagged_in_both_filenames(self, traces_dir):
        exporter = FileSpanExporter(traces_dir, script_name="myscript")
        exporter.export([make_span(trace_id=7, name="x")])

        exporter.emit(failed=True)

        names = sorted(path.name for path in traces_dir.iterdir())
        assert len(names) == 2
        assert names[0].endswith(".error.otlp.json")
        assert names[1].endswith(".error.readable.json")


class TestNameCollisions:
    def test_traces_from_the_same_second_do_not_share_files(
        self, traces_dir, monkeypatch
    ):
        # With the clock frozen, both traces want the same name; the tiebreaker
        # is the only thing keeping one from overwriting the other.
        monkeypatch.setattr(file_module, "datetime", _FrozenClock)
        exporter = FileSpanExporter(traces_dir, script_name="s")
        exporter.export(
            [make_span(trace_id=1, name="a"), make_span(trace_id=2, name="b")]
        )

        exporter.emit()

        names = sorted(path.name for path in traces_dir.iterdir())
        assert names == [
            "2026-01-31_14-05-09_s.otlp.json",
            "2026-01-31_14-05-09_s.readable.json",
            "2026-01-31_14-05-09_s_1.otlp.json",
            "2026-01-31_14-05-09_s_1.readable.json",
        ]

    def test_a_traces_name_survives_a_later_failing_emit(
        self, traces_dir, monkeypatch
    ):
        # A mid-run flush names the files; a crash afterwards rewrites the same
        # pair rather than leaving a stale success-named pair beside an .error
        # one. The outcome is captured when the name is allocated.
        monkeypatch.setattr(file_module, "datetime", _FrozenClock)
        exporter = FileSpanExporter(traces_dir, script_name="s")
        exporter.export([make_span(trace_id=1, name="a")])
        exporter.emit()

        exporter.export([make_span(trace_id=1, name="b")])
        exporter.emit(failed=True)

        names = sorted(path.name for path in traces_dir.iterdir())
        assert names == [
            "2026-01-31_14-05-09_s.otlp.json",
            "2026-01-31_14-05-09_s.readable.json",
        ]
        (spans,) = _load_readable(traces_dir).values()
        assert [span["name"] for span in spans] == ["a", "b"]


class TestRepeatEmit:
    """A second emit rewrites each trace's files with the whole trace.

    This sink keeps its buffer, so a rewrite carries the complete trace rather
    than the fragment that arrived since the last write, and lands on the files
    the first emit named. See ``docs/design-notes.md`` ("Repeat emits: rewrite or
    clear").
    """

    def test_later_spans_join_the_earlier_ones_in_the_same_files(
        self, traces_dir
    ):
        exporter = FileSpanExporter(traces_dir, script_name="s")
        exporter.export([make_span(trace_id=1, name="a")])
        exporter.emit()

        exporter.export([make_span(trace_id=1, name="b")])
        exporter.emit()

        # Still one pair of files, holding the whole trace rather than a
        # fragment of it each.
        assert len(_load_otlp(traces_dir)) == 1
        (spans,) = _load_readable(traces_dir).values()
        assert [span["name"] for span in spans] == ["a", "b"]

    def test_emitting_again_with_nothing_new_keeps_the_files_intact(
        self, traces_dir
    ):
        exporter = FileSpanExporter(traces_dir, script_name="s")
        exporter.export([make_span(trace_id=1, name="a")])

        exporter.emit()
        exporter.emit()

        assert len(_load_otlp(traces_dir)) == 1
        (spans,) = _load_readable(traces_dir).values()
        assert [span["name"] for span in spans] == ["a"]


class TestDefaults:
    """``FileSpanExporter()`` is the default sink, with nothing to pass.

    Both arguments default to what ``init`` would derive, so keeping the trace
    files while adding a sink of your own stays a one-liner:
    ``exporters=[FileSpanExporter(), MySink()]``.
    """

    def test_base_dir_defaults_to_the_standard_traces_dir(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)

        FileSpanExporter()

        assert (tmp_path / "traces").is_dir()

    def test_script_name_defaults_to_the_detected_script(
        self, traces_dir, monkeypatch
    ):
        monkeypatch.setattr(
            "argus.exporters.file.detect_script_name", lambda: "detected"
        )
        exporter = FileSpanExporter(traces_dir)
        exporter.export([make_span(trace_id=1, name="a")])

        exporter.emit()

        names = [path.name for path in traces_dir.iterdir()]
        assert names and all("detected" in name for name in names)


class TestBaseDirectory:
    def test_creates_base_dir_with_parents(self, tmp_path):
        nested = tmp_path / "deeply" / "nested" / "traces"

        FileSpanExporter(nested, script_name="s")

        assert nested.is_dir()


class TestOtelLifecycleHooks:
    def test_force_flush_and_shutdown_are_noops(self, traces_dir):
        exporter = FileSpanExporter(traces_dir, script_name="s")

        assert exporter.force_flush() is True
        assert exporter.shutdown() is None
