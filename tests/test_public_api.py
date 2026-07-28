"""Tests for what Argus exports, and under which name.

The importable names are the part of Argus hardest to change once people depend
on them, so they get an explicit contract rather than only being covered
incidentally by the tests that use them. Two properties matter: everything a
caller needs for a documented one-liner is reachable from the package root, and
no name Argus exports collides with the OpenTelemetry name it sits next to.

The packaged distribution has a contract of its own: the annotations Argus is
written with are only visible downstream if the ``py.typed`` marker ships beside
them.
"""

from __future__ import annotations

from pathlib import Path

import argus
from argus import exporters
from argus.exporters import base, file, otlp


class TestTopLevelSurface:
    def test_exports_exactly_the_documented_names(self):
        assert set(argus.__all__) == {
            "FileSpanExporter",
            "OtlpConfig",
            "Session",
            "blindspot",
            "init",
            "reset",
        }

    def test_every_exported_name_resolves(self):
        assert all(hasattr(argus, name) for name in argus.__all__)

    def test_the_two_init_arguments_are_reachable_from_the_root(self):
        # ``exporters=[FileSpanExporter()]`` and ``otlp=OtlpConfig(...)`` are
        # both documented one-liners, so neither should need a submodule import.
        assert argus.FileSpanExporter is file.FileSpanExporter
        assert argus.OtlpConfig is otlp.OtlpConfig


class TestSessionSurface:
    """What a caller may reach for on the object ``init`` hands back.

    Same reasoning as the importable names above: the session's members are
    hard to take back once someone depends on them, and an undecided surface
    gets depended on by accident. See ``docs/design-notes.md`` ("The session
    reports, it does not rewire").
    """

    def test_exposes_exactly_the_documented_members(self):
        public = {
            name for name in vars(argus.Session) if not name.startswith("_")
        }

        assert public == {
            "exporters",
            "flush",
            "instruments",
            "project",
            "provider",
        }

    def test_everything_readable_is_read_only(self):
        # Every public member but ``flush`` is a property, which is what makes
        # the "these report, they do not rewire" contract enforced rather than
        # merely documented.
        readable = [
            name
            for name in vars(argus.Session)
            if not name.startswith("_") and name != "flush"
        ]

        assert all(
            isinstance(vars(argus.Session)[name], property) for name in readable
        )
        assert all(vars(argus.Session)[name].fset is None for name in readable)


class TestExportersSurface:
    def test_exports_the_sinks_and_the_protocol(self):
        assert set(exporters.__all__) == {
            "BufferedOTLPExporter",
            "BufferedSpanExporter",
            "FileSpanExporter",
            "OtlpConfig",
            "TraceFormat",
            "trace_filename",
        }

    def test_names_resolve_to_their_defining_modules(self):
        assert exporters.BufferedSpanExporter is base.BufferedSpanExporter
        assert exporters.FileSpanExporter is file.FileSpanExporter
        assert exporters.BufferedOTLPExporter is otlp.BufferedOTLPExporter
        assert exporters.OtlpConfig is otlp.OtlpConfig
        assert exporters.trace_filename is file.trace_filename
        assert exporters.TraceFormat is file.TraceFormat

    def test_delivery_is_internal_to_base(self):
        # Delivery is the return type of the private _DeferredExporter._deliver;
        # a third-party sink implements ``emit`` (the BufferedSpanExporter
        # protocol) and never encounters it, so it stays out of the package
        # surface rather than promising a name nothing hands back.
        assert not hasattr(exporters, "Delivery")
        assert "Delivery" not in exporters.__all__

    def test_the_remote_sink_does_not_shadow_otels_own_name(self):
        # OpenTelemetry ships an ``OTLPSpanExporter`` that streams spans;
        # Argus's buffers them and POSTs once. Two different lifecycles must not
        # share one importable name, or an import line stops being self-evident.
        assert not hasattr(exporters, "OTLPSpanExporter")
        assert not hasattr(otlp, "OTLPSpanExporter")

    def test_the_remote_sink_has_a_single_entry_point(self):
        # It used to be constructible through a ``make_otlp_exporter`` factory as
        # well, which made "which one do I call?" a coin flip.
        assert not hasattr(otlp, "make_otlp_exporter")
        assert not hasattr(exporters, "make_otlp_exporter")


class TestTypeHintsAreShipped:
    """The annotations are only worth having if they reach the installed copy."""

    def test_the_package_carries_a_py_typed_marker(self):
        marker = Path(argus.__file__).parent / "py.typed"

        assert marker.is_file(), (
            "PEP 561 marker missing: without src/argus/py.typed a consumer's "
            "type checker ignores every annotation in the package."
        )
