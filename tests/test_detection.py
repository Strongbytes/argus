"""Tests for :mod:`argus.detection` -- selection, dedup, and auto-detection."""

from __future__ import annotations

import pytest

from argus import detection


class _FakeA:
    pass


class _FakeB:
    pass


class TestResolveInstrumentors:
    def test_all_uses_entry_points(self, monkeypatch):
        monkeypatch.setattr(detection, "_entry_point_classes", lambda: [_FakeA])

        result = detection.resolve_instrumentors("all")

        assert [type(i) for i in result] == [_FakeA]

    @pytest.mark.parametrize("selection", [None, "curated"])
    def test_curated_uses_auto_detection(self, monkeypatch, selection):
        monkeypatch.setattr(detection, "_auto_keys", lambda: ["k1"])
        monkeypatch.setattr(
            detection,
            "_classes_for_keys",
            lambda keys: [_FakeA] if list(keys) == ["k1"] else [],
        )

        result = detection.resolve_instrumentors(selection)

        assert [type(i) for i in result] == [_FakeA]

    def test_single_key_is_wrapped_in_a_list(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            detection,
            "_classes_for_keys",
            lambda keys: seen.append(list(keys)) or [_FakeA],
        )

        detection.resolve_instrumentors("openai")

        assert seen == [["openai"]]

    def test_sequence_of_keys_passed_through(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            detection,
            "_classes_for_keys",
            lambda keys: seen.append(list(keys)) or [_FakeA],
        )

        detection.resolve_instrumentors(["a", "b"])

        assert seen == [["a", "b"]]

    def test_duplicate_classes_are_instantiated_once(self, monkeypatch):
        monkeypatch.setattr(
            detection,
            "_classes_for_keys",
            lambda keys: [_FakeA, _FakeA, _FakeB],
        )

        result = detection.resolve_instrumentors(["x"])

        assert [type(i) for i in result] == [_FakeA, _FakeB]


class TestClassesForKeys:
    def test_unknown_key_raises_with_known_keys(self):
        with pytest.raises(ValueError, match="Unknown instrument key"):
            detection._classes_for_keys(["definitely-not-a-key"])

    def test_resolves_each_path_for_a_known_key(self, monkeypatch):
        monkeypatch.setattr(detection, "_load", lambda path: path)

        assert detection._classes_for_keys(["agno"]) == [
            "openinference.instrumentation.agno:AgnoInstrumentor",
            "openinference.instrumentation.openai:OpenAIInstrumentor",
        ]


class TestAutoKeys:
    def test_prefers_already_imported_modules(self, monkeypatch):
        monkeypatch.setattr(
            detection, "_module_loaded", lambda name: name == "agents"
        )
        monkeypatch.setattr(detection, "_module_available", lambda name: True)

        assert detection._auto_keys() == ["openai_agents"]

    def test_falls_back_to_available_modules(self, monkeypatch):
        monkeypatch.setattr(detection, "_module_loaded", lambda name: False)
        monkeypatch.setattr(
            detection,
            "_module_available",
            lambda name: name == "claude_agent_sdk",
        )

        assert detection._auto_keys() == ["claude"]

    def test_drops_standalone_openai_when_superseded(self, monkeypatch):
        monkeypatch.setattr(
            detection,
            "_module_loaded",
            lambda name: name in {"agents", "openai"},
        )

        assert detection._auto_keys() == ["openai_agents"]

    def test_keeps_openai_when_it_is_the_only_framework(self, monkeypatch):
        monkeypatch.setattr(
            detection, "_module_loaded", lambda name: name == "openai"
        )

        assert detection._auto_keys() == ["openai"]
