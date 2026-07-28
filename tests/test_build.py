"""Tests for the pieces ``init`` assembles before any provider exists.

Split out of ``tests/test_session.py``: building the resource every span
carries (``_build_resource``) and deciding which exporters a run writes through
(``_build_sinks``) are settled ahead of the TracerProvider and the session
singleton, so they can be pinned without either in the way.
"""

from __future__ import annotations

import pytest

import argus
from argus import session as session_module
from argus.exporters import BufferedOTLPExporter

from tests.factories import PlainSpanExporter


@pytest.fixture
def fake_otlp_transport(monkeypatch):
    """Patch transport construction, so a remote sink can be built offline.

    Transport construction is the OTLP module's single seam (see
    ``tests/test_otlp_exporter.py``, which exercises it properly); patching it
    here keeps these tests off the network and free of the optional ``otlp``
    extra, while still building a real ``BufferedOTLPExporter``.
    """
    monkeypatch.setattr(
        "argus.exporters.otlp._build_transport",
        lambda *_: PlainSpanExporter(),
    )


class TestBuildResource:
    """The attributes every span carries, decided in one place."""

    def test_stamps_project_service_and_version(self):
        resource = session_module._build_resource("proj", "svc")

        assert resource.attributes["service.name"] == "svc"
        assert resource.attributes["argus.project"] == "proj"
        assert resource.attributes["argus.version"] == argus.__version__

    def test_service_falls_back_to_the_script_name(self):
        resource = session_module._build_resource("proj", None)

        # The observed app still gets an identity when the caller names none.
        assert (
            resource.attributes["service.name"]
            == session_module.detect_script_name()
        )


class TestBuildSinks:
    """Which exporters a run writes through, settled before any provider exists.

    ``init`` delegates the whole ``exporters``/``output_dir``/``otlp`` decision
    here, which is what lets these rules be pinned without a TracerProvider or a
    session singleton in the way.
    """

    def test_defaults_to_a_file_sink_in_the_output_dir(self, traces_dir):
        (sink,) = session_module._build_sinks(None, traces_dir, None)

        assert isinstance(sink, argus.FileSpanExporter)
        assert traces_dir.is_dir()

    def test_an_explicit_list_replaces_the_default(self, recording_exporter):
        sinks = session_module._build_sinks([recording_exporter], None, None)

        assert sinks == [recording_exporter]

    def test_the_callers_list_is_not_mutated(
        self, recording_exporter, fake_otlp_transport
    ):
        given = [recording_exporter]

        session_module._build_sinks(
            given,
            None,
            argus.OtlpConfig("https://backend.test/v1/traces", api_key="k"),
        )

        # The remote sink is appended to Argus's copy; a caller who reuses their
        # list must not find a sink they never asked for in it.
        assert given == [recording_exporter]

    def test_the_remote_sink_is_added_alongside_the_others(
        self, recording_exporter, fake_otlp_transport
    ):
        sinks = session_module._build_sinks(
            [recording_exporter],
            None,
            argus.OtlpConfig("https://backend.test/v1/traces", api_key="k"),
        )

        # Remote export layers on top rather than replacing: the local sink and
        # the backend both see the run.
        assert sinks[0] is recording_exporter
        assert isinstance(sinks[1], BufferedOTLPExporter)

    def test_output_dir_alongside_an_explicit_list_warns(
        self, recording_exporter, traces_dir
    ):
        with pytest.warns(RuntimeWarning, match="output_dir has no effect"):
            session_module._build_sinks([recording_exporter], traces_dir, None)

        # Nothing was built to write there, so nothing created it either.
        assert not traces_dir.exists()
