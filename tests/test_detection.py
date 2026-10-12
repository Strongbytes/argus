"""Tests for :mod:`argus.detection` -- the contract, selection, and detection."""

from __future__ import annotations

import sys
from importlib import invalidate_caches
from types import ModuleType
from typing import get_args

import pytest

from argus import detection

from tests.factories import FakeInstrumentor


class _FakeA(FakeInstrumentor):
    pass


class _FakeB(FakeInstrumentor):
    pass


class _FakeEntryPoint:
    """Stand-in for an ``importlib.metadata.EntryPoint``.

    ``load()`` is the whole of what detection calls on one, and the whole of
    what can go wrong with one: an instrumentor that publishes itself but cannot
    be imported is exactly the case ``instrument="all"`` promises to survive.
    """

    def __init__(self, loads=None, *, raises=None):
        self._loads = loads
        self._raises = raises

    def load(self):
        if self._raises is not None:
            raise self._raises
        return self._loads


@pytest.fixture
def published_instrumentors(monkeypatch):
    """Return a helper publishing entry points for detection to discover.

    Patches ``importlib.metadata.entry_points`` rather than Argus's own
    ``_entry_point_classes``, so ``instrument="all"`` runs the real discovery
    loop -- including the skip that keeps a broken instrumentor from aborting
    the run. Returns the groups asked for, so a test can pin which one Argus
    reads::

        groups = published_instrumentors(_FakeEntryPoint(_FakeA))
    """
    groups: list[str] = []

    def _publish(*entry_points):
        def fake_entry_points(*, group):
            groups.append(group)
            return list(entry_points)

        monkeypatch.setattr(
            "importlib.metadata.entry_points", fake_entry_points
        )
        return groups

    return _publish


class TestInstrumentorProtocol:
    """``Instrumentor`` is what Argus requires: turn on, and turn back off."""

    def test_what_detection_resolves_satisfies_it(
        self, published_instrumentors
    ):
        published_instrumentors(_FakeEntryPoint(_FakeA))

        (resolved,) = detection.resolve_instrumentors("all")

        assert isinstance(resolved, detection.Instrumentor)

    def test_an_object_with_neither_method_does_not(self):
        assert not isinstance(object(), detection.Instrumentor)

    def test_instrumenting_without_a_way_back_does_not(self):
        # ``reset`` tears instrumentors down, so ``uninstrument`` is required.
        class OneWay:
            def instrument(self, **kwargs):
                pass

        assert not isinstance(OneWay(), detection.Instrumentor)


class TestEntryPointDiscovery:
    """``instrument="all"``: turn on whatever advertises itself, survive a dud.

    Patched at ``importlib.metadata`` rather than at Argus's own
    ``_entry_point_classes``, so the loop the README's ``instrument="all"``
    paragraph describes is the code actually running here.
    """

    def test_every_published_instrumentor_is_turned_on(
        self, published_instrumentors
    ):
        published_instrumentors(
            _FakeEntryPoint(_FakeA), _FakeEntryPoint(_FakeB)
        )

        result = detection.resolve_instrumentors("all")

        assert [type(i) for i in result] == [_FakeA, _FakeB]

    def test_only_the_openinference_group_is_read(
        self, published_instrumentors
    ):
        groups = published_instrumentors(_FakeEntryPoint(_FakeA))

        detection.resolve_instrumentors("all")

        # Reading a wider group would load and instantiate entry points that
        # have nothing to do with instrumentation.
        assert groups == [detection._ENTRY_POINT_GROUP]

    def test_a_broken_instrumentor_is_skipped_not_fatal(
        self, published_instrumentors
    ):
        published_instrumentors(
            _FakeEntryPoint(raises=ImportError("no such optional dependency")),
            _FakeEntryPoint(_FakeB),
        )

        result = detection.resolve_instrumentors("all")

        # The documented promise: a half-installed extra or a version mismatch
        # costs its own instrumentor and nothing else -- neither the run nor the
        # instrumentors published after it.
        assert [type(i) for i in result] == [_FakeB]

    def test_every_instrumentor_failing_is_still_not_fatal(
        self, published_instrumentors
    ):
        published_instrumentors(_FakeEntryPoint(raises=RuntimeError("boom")))

        # Resolving to nothing is init's to complain about, and it does (see
        # ``tests/test_session.py``); discovery's job is to return rather than
        # raise.
        assert detection.resolve_instrumentors("all") == []


class TestResolveInstrumentors:
    """How ``instrument=``'s shapes are normalized before any lookup runs.

    ``_classes_for_keys`` is stubbed here to record what it is handed: these pin
    the argument handling alone, and what the recorded keys then resolve to is
    :class:`TestCuratedDetectionForReal`'s job.
    """

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
            lambda _keys: [_FakeA, _FakeA, _FakeB],
        )

        result = detection.resolve_instrumentors(["x"])

        assert [type(i) for i in result] == [_FakeA, _FakeB]


class TestCuratedDetectionForReal:
    """The default selection end to end: real detection, real registry entries.

    :class:`TestAutoKeys` and :class:`TestClassesForKeys` pin the two halves with
    their seams stubbed, which is what makes them precise. These drive the whole
    path -- sys.modules probe, supersession, key lookup, import, dedupe -- with
    nothing patched but ``sys.modules`` itself, against the frameworks the
    registry really lists rather than a fictional key. The exact module paths
    each key resolves to stay :class:`TestClassesForKeys`'s job, since the
    fixture below builds its stand-ins from those very paths.
    """

    @pytest.fixture(autouse=True)
    def registry_imports_resolve(self, monkeypatch):
        """Make every ``module:Class`` path in the registry import to a fake.

        The real instrumentors are optional third-party packages the suite never
        touches (see ``tests/factories.py``), and ``_load`` imports through
        ``sys.modules``, so a stand-in module there is enough for the real
        registry to resolve on any machine. One class per path, shared by every
        key that names it, so ``resolve_instrumentors``'s dedupe sees exactly
        what it would in a real run.
        """
        modules: dict[str, ModuleType] = {}
        for framework in detection._FRAMEWORKS:
            for path in framework.instrumentors:
                module_path, _, attr = path.partition(":")
                module = modules.setdefault(
                    module_path, ModuleType(module_path)
                )
                setattr(module, attr, type(attr, (FakeInstrumentor,), {}))
        for module_path, module in modules.items():
            monkeypatch.setitem(sys.modules, module_path, module)

    @pytest.fixture
    def frameworks_in_use(self, monkeypatch):
        """Return a helper making exactly the named frameworks look in use.

        Detection prefers what is already imported, so a detector module in
        ``sys.modules`` is the real signal. Every other detector is taken out for
        the duration, because the machine running the suite may genuinely have
        one installed -- and a dev environment has all of them.

        Companions are found by importability instead, which the machine would
        answer just as unpredictably, so that is pinned too: importable are the
        loaded detectors, the ones named in ``installed`` (an SDK present but
        not yet imported), and every registry instrumentor, which the fixture
        above makes resolve.
        """
        instrumentor_modules = {
            path.partition(":")[0]
            for framework in detection._FRAMEWORKS
            for path in framework.instrumentors
        }

        def _in_use(*keys, installed=()):
            for framework in detection._FRAMEWORKS:
                monkeypatch.delitem(
                    sys.modules, framework.detector, raising=False
                )
            for key in keys:
                detector = detection._BY_KEY[key].detector
                monkeypatch.setitem(sys.modules, detector, ModuleType(detector))
            importable = instrumentor_modules | {
                detection._BY_KEY[key].detector for key in (*keys, *installed)
            }
            monkeypatch.setattr(
                detection, "_module_available", lambda name: name in importable
            )

        return _in_use

    @pytest.mark.parametrize("selection", [None, "curated"])
    def test_a_detected_framework_turns_on_its_registry_entry(
        self, selection, frameworks_in_use
    ):
        frameworks_in_use("agno")

        result = detection.resolve_instrumentors(selection)

        assert [type(i).__name__ for i in result] == ["AgnoInstrumentor"]

    @pytest.mark.parametrize("provider", ["openai", "anthropic"])
    def test_agno_does_not_double_up_with_a_provider(
        self, provider, frameworks_in_use
    ):
        # Agno's instrumentor records every model call itself, whatever the
        # provider; the provider instrumentor would nest a second LLM span with
        # the same tokens under each one.
        frameworks_in_use("agno", provider)

        result = detection.resolve_instrumentors(None)

        assert [type(i).__name__ for i in result] == ["AgnoInstrumentor"]

    def test_explicit_keys_can_still_pair_agno_with_openai(self):
        # The way back to the raw request parameters the OpenAI span carries,
        # accepting the double count.
        result = detection.resolve_instrumentors(["agno", "openai"])

        assert [type(i).__name__ for i in result] == [
            "AgnoInstrumentor",
            "OpenAIInstrumentor",
        ]

    def test_a_superseded_key_does_not_double_up(self, frameworks_in_use):
        frameworks_in_use("openai_agents", "openai")

        result = detection.resolve_instrumentors(None)

        # The load-bearing supersession, with both frameworks genuinely
        # detectable: the agents instrumentor covers OpenAI client calls itself,
        # so the standalone key drops out instead of patching them twice.
        assert [type(i).__name__ for i in result] == [
            "OpenAIAgentsInstrumentor"
        ]

    def test_langchain_is_detected_through_langchain_core(
        self, frameworks_in_use
    ):
        # The detector is the module the instrumentor patches, not the
        # ``langchain`` package: LangGraph and ``langchain-openai`` run without
        # the latter, never without the former.
        frameworks_in_use("langchain")

        result = detection.resolve_instrumentors(None)

        assert detection._BY_KEY["langchain"].detector == "langchain_core"
        assert [type(i).__name__ for i in result] == ["LangChainInstrumentor"]

    def test_langgraph_alone_turns_on_the_langchain_instrumentor(
        self, frameworks_in_use
    ):
        # OpenInference has no LangGraph instrumentor; the LangChain one traces
        # graph runs because both sit on ``langchain_core``'s callbacks.
        frameworks_in_use("langgraph")

        result = detection.resolve_instrumentors(None)

        assert [type(i).__name__ for i in result] == ["LangChainInstrumentor"]

    def test_langchain_and_langgraph_together_instrument_once(
        self, frameworks_in_use
    ):
        # The common case -- a LangGraph app imports both -- must not patch
        # ``langchain_core`` twice, which would add the tracer handler twice
        # and double every span.
        frameworks_in_use("langchain", "langgraph")

        result = detection.resolve_instrumentors(None)

        assert [type(i).__name__ for i in result] == ["LangChainInstrumentor"]

    @pytest.mark.parametrize("provider", ["openai", "anthropic"])
    @pytest.mark.parametrize("key", ["langchain", "langgraph"])
    def test_langchain_does_not_double_up_with_a_provider(
        self, key, provider, frameworks_in_use
    ):
        # ``langchain-openai`` and ``langchain-anthropic`` call the provider
        # SDKs, so the standalone instrumentor would report each model call a
        # second time, with the same token counts.
        frameworks_in_use(key, provider)

        result = detection.resolve_instrumentors(None)

        assert [type(i).__name__ for i in result] == ["LangChainInstrumentor"]

    @pytest.mark.parametrize(
        ("provider", "instrumentor"),
        [
            ("openai", "OpenAIInstrumentor"),
            ("anthropic", "AnthropicInstrumentor"),
        ],
    )
    def test_explicit_keys_can_still_pair_langchain_with_a_provider(
        self, provider, instrumentor
    ):
        # The documented escape hatch for a node that calls the raw client:
        # supersession shapes auto-detection only, never an explicit list.
        result = detection.resolve_instrumentors(["langchain", provider])

        assert [type(i).__name__ for i in result] == [
            "LangChainInstrumentor",
            instrumentor,
        ]

    def test_the_anthropic_client_alone_turns_on_its_instrumentor(
        self, frameworks_in_use
    ):
        frameworks_in_use("anthropic")

        result = detection.resolve_instrumentors(None)

        assert [type(i).__name__ for i in result] == ["AnthropicInstrumentor"]

    def test_the_claude_agent_sdk_and_the_anthropic_client_are_both_kept(
        self, frameworks_in_use
    ):
        # Two Anthropic products with no overlap: the Agent SDK drives the
        # Claude Code CLI in a subprocess and never calls the ``anthropic``
        # package, so neither key supersedes the other.
        frameworks_in_use("claude", "anthropic")

        result = detection.resolve_instrumentors(None)

        assert [type(i).__name__ for i in result] == [
            "ClaudeAgentSDKInstrumentor",
            "AnthropicInstrumentor",
        ]

    def test_a_crew_gets_its_llm_spans_from_the_openai_key(
        self, frameworks_in_use
    ):
        # ``import crewai`` imports ``openai``, so this is what a crew looks
        # like to detection. CrewAI's instrumentor emits no LLM spans; the
        # OpenAI one, reached through its own key, supplies them.
        frameworks_in_use("crewai", "openai")

        result = detection.resolve_instrumentors(None)

        assert [type(i).__name__ for i in result] == [
            "CrewAIInstrumentor",
            "OpenAIInstrumentor",
        ]

    def test_a_claude_crew_gets_the_anthropic_companion(
        self, frameworks_in_use
    ):
        # What ``init`` sees for a crew on Claude in the usual script order:
        # CrewAI imports its Anthropic provider only once an agent is built, so
        # ``anthropic`` is installed but not yet loaded.
        frameworks_in_use("crewai", "openai", installed=("anthropic",))

        result = detection.resolve_instrumentors(None)

        assert [type(i).__name__ for i in result] == [
            "CrewAIInstrumentor",
            "AnthropicInstrumentor",
            "OpenAIInstrumentor",
        ]

    def test_the_openai_companion_does_not_rely_on_crewai_importing_it(
        self, frameworks_in_use
    ):
        frameworks_in_use("crewai", installed=("openai",))

        result = detection.resolve_instrumentors(None)

        assert [type(i).__name__ for i in result] == [
            "CrewAIInstrumentor",
            "OpenAIInstrumentor",
        ]

    def test_crewai_with_langchain_reports_chatopenai_calls_once(
        self, frameworks_in_use
    ):
        # Why CrewAI's providers are companions rather than a pair: as keys
        # they stay subject to supersession, so LangChain drops both here
        # exactly as it does without CrewAI. A pair would bring them back as
        # classes, past that rule, and report every ``ChatOpenAI`` and
        # ``ChatAnthropic`` call twice.
        frameworks_in_use(
            "crewai", "langchain", "openai", installed=("anthropic",)
        )

        result = detection.resolve_instrumentors(None)

        assert [type(i).__name__ for i in result] == [
            "LangChainInstrumentor",
            "CrewAIInstrumentor",
        ]

    def test_explicit_crewai_alone_has_no_llm_instrumentor(self):
        # Explicit means exact, so companions don't expand it: an explicit
        # selection names the provider too, as ``["crewai", "openai"]``.
        result = detection.resolve_instrumentors("crewai")

        assert [type(i).__name__ for i in result] == ["CrewAIInstrumentor"]

    @pytest.mark.parametrize(
        ("provider", "instrumentor"),
        [
            ("openai", "OpenAIInstrumentor"),
            ("anthropic", "AnthropicInstrumentor"),
        ],
    )
    def test_explicit_keys_can_pair_crewai_and_langchain_with_a_provider(
        self, provider, instrumentor
    ):
        # The escape hatch for a mixed run whose CrewAI agents call the
        # provider natively, accepting the duplicate for each LangChain model
        # call on it.
        result = detection.resolve_instrumentors(
            ["crewai", "langchain", provider]
        )

        assert [type(i).__name__ for i in result] == [
            "CrewAIInstrumentor",
            "LangChainInstrumentor",
            instrumentor,
        ]

    def test_every_framework_on_its_own_turns_something_on(
        self, frameworks_in_use
    ):
        # One key at a time, reached the way a real run reaches it. An entry
        # added with an empty instrumentor tuple would otherwise detect happily
        # and instrument nothing.
        for key in detection._BY_KEY:
            frameworks_in_use(key)

            result = detection.resolve_instrumentors(None)

            assert result
            assert all(isinstance(i, detection.Instrumentor) for i in result)


class TestInstrumentVocabulary:
    """``instrument=``'s keys as a type, so a typo is caught before it runs.

    ``_Framework.key`` is annotated with ``InstrumentKey``, so mypy already
    rejects a registry entry naming something the type doesn't offer. These pin
    what a type checker cannot: that the type offers nothing the registry lacks,
    which would autocomplete happily and then raise ``ValueError``.
    """

    def test_every_key_the_type_offers_is_in_the_registry(self):
        assert set(get_args(detection.InstrumentKey)) == set(detection._BY_KEY)

    def test_a_strategy_is_not_a_registry_key(self):
        # "curated" and "all" choose *how* keys are picked, and
        # ``resolve_instrumentors`` branches on them before any lookup -- so a
        # framework of either name would be unreachable.
        assert set(get_args(detection.InstrumentStrategy)).isdisjoint(
            detection._BY_KEY
        )


class TestSupersession:
    """Supersession is a field on the framework that does it, not a side table.

    That makes adding a framework one self-contained entry, and it lets any
    framework supersede any key -- which the previous hardcoded rule could not
    express, and which is why the invariants below are worth asserting now.
    """

    def test_every_superseded_key_exists(self):
        for framework in detection._FRAMEWORKS:
            assert set(framework.supersedes) <= set(detection._BY_KEY)

    def test_no_framework_supersedes_one_that_supersedes_it(self):
        # A mutual pair (or a self-reference) would drop both keys and leave the
        # run with nothing instrumented, which the old openai-only rule made
        # impossible and the general one does not.
        for framework in detection._FRAMEWORKS:
            for key in framework.supersedes:
                assert framework.key not in detection._BY_KEY[key].supersedes

    def test_a_framework_supersedes_nothing_by_default(self):
        # The common case stays a three-field entry.
        framework = detection._Framework("openai", "openai", ("mod:Cls",))

        assert framework.supersedes == ()
        assert framework.companions == ()

    def test_every_companion_exists(self):
        for framework in detection._FRAMEWORKS:
            assert set(framework.companions) <= set(detection._BY_KEY)

    def test_no_framework_both_supersedes_and_companions_a_key(self):
        # It would add the key and drop it again in one detection pass.
        for framework in detection._FRAMEWORKS:
            assert set(framework.companions).isdisjoint(framework.supersedes)

    def test_companions_are_one_level_deep(self):
        # ``_auto_keys`` adds the companions of detected frameworks only, not
        # of the companions it added, so a companion's own would be ignored.
        for framework in detection._FRAMEWORKS:
            for key in framework.companions:
                assert detection._BY_KEY[key].companions == ()

    def test_no_framework_is_its_own_companion(self):
        for framework in detection._FRAMEWORKS:
            assert framework.key not in framework.companions


class TestClassesForKeys:
    """Which instrumentor paths a key maps to, asserted as the paths themselves.

    ``_load`` is stubbed to return the path it was given, which is what lets the
    registry's exact targets be read back here; the import it really performs is
    pinned in :class:`TestLoad`.
    """

    def test_unknown_key_raises_with_known_keys(self):
        with pytest.raises(ValueError, match="Unknown instrument key"):
            detection._classes_for_keys(["definitely-not-a-key"])

    def test_resolves_each_path_for_a_known_key(self, monkeypatch):
        monkeypatch.setattr(detection, "_load", lambda path: path)

        assert detection._classes_for_keys(["agno", "openai"]) == [
            "openinference.instrumentation.agno:AgnoInstrumentor",
            "openinference.instrumentation.openai:OpenAIInstrumentor",
        ]

    def test_agno_resolves_to_its_instrumentor_alone(self, monkeypatch):
        # Its instrumentor records the model calls itself, so it pairs nothing.
        monkeypatch.setattr(detection, "_load", lambda path: path)

        assert detection._classes_for_keys(["agno"]) == [
            "openinference.instrumentation.agno:AgnoInstrumentor"
        ]

    @pytest.mark.parametrize("key", ["langchain", "langgraph"])
    def test_langchain_and_langgraph_resolve_to_the_same_instrumentor(
        self, key, monkeypatch
    ):
        monkeypatch.setattr(detection, "_load", lambda path: path)

        assert detection._classes_for_keys([key]) == [
            "openinference.instrumentation.langchain:LangChainInstrumentor"
        ]

    def test_crewai_resolves_to_its_instrumentor_alone(self, monkeypatch):
        monkeypatch.setattr(detection, "_load", lambda path: path)

        assert detection._classes_for_keys(["crewai"]) == [
            "openinference.instrumentation.crewai:CrewAIInstrumentor"
        ]

    def test_anthropic_resolves_to_its_instrumentor(self, monkeypatch):
        monkeypatch.setattr(detection, "_load", lambda path: path)

        assert detection._classes_for_keys(["anthropic"]) == [
            "openinference.instrumentation.anthropic:AnthropicInstrumentor"
        ]


class TestLoad:
    """The lazy-import seam, run rather than stubbed.

    An instrumentor class is imported only once its framework is selected, so an
    unused extra never has to be installed -- which is also why the registry's
    real targets cannot be imported here. The test suite's own module stands in:
    it is always importable, and ``_load`` cannot tell the difference.
    """

    def test_imports_the_attribute_a_path_names(self):
        assert (
            detection._load("tests.factories:FakeInstrumentor")
            is FakeInstrumentor
        )

    def test_a_missing_module_raises(self):
        # What a caller who asked for a key whose extra is not installed sees.
        # It propagates: ``instrument="all"`` catches it per instrumentor, an
        # explicit key does not, and either way the message names the package.
        with pytest.raises(ModuleNotFoundError):
            detection._load("argus_no_such_instrumentation:Thing")

    def test_a_missing_attribute_raises(self):
        # An installed package that no longer exports the class the registry
        # names -- a rename upstream, which must not pass silently.
        with pytest.raises(AttributeError):
            detection._load("tests.factories:NoSuchInstrumentor")


class TestAutoKeys:
    def test_prefers_already_imported_modules(self, monkeypatch):
        monkeypatch.setattr(
            detection, "_module_loaded", lambda name: name == "agents"
        )
        monkeypatch.setattr(detection, "_module_available", lambda _name: True)

        assert detection._auto_keys() == ["openai_agents"]

    def test_falls_back_to_available_modules(self, monkeypatch):
        monkeypatch.setattr(detection, "_module_loaded", lambda _name: False)
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

    @pytest.mark.parametrize("provider", ["openai", "anthropic"])
    def test_agno_supersedes_the_provider_keys(self, provider, monkeypatch):
        monkeypatch.setattr(
            detection,
            "_module_loaded",
            lambda name: name in {"agno", provider},
        )

        # Load-bearing: Agno's instrumentor records the model calls itself, so
        # a provider key alongside it would count each one twice.
        assert detection._auto_keys() == ["agno"]

    def test_langgraph_app_is_detected_without_the_langchain_package(
        self, monkeypatch
    ):
        # A LangGraph app on ``langchain-openai`` never imports ``langchain``;
        # what it does import is ``langchain_core`` and, usually, ``openai``.
        monkeypatch.setattr(
            detection,
            "_module_loaded",
            lambda name: name in {"langchain_core", "langgraph", "openai"},
        )

        assert detection._auto_keys() == ["langchain", "langgraph"]

    @pytest.mark.parametrize("key", ["langchain", "langgraph"])
    def test_langchain_drops_standalone_anthropic(self, key, monkeypatch):
        # ``langchain-anthropic`` imports ``anthropic``, so a ``ChatAnthropic``
        # app looks like this to detection. ``langgraph`` is checked on its own
        # because its entry carries its own ``supersedes``.
        detector = detection._BY_KEY[key].detector
        monkeypatch.setattr(
            detection,
            "_module_loaded",
            lambda name: name in {detector, "anthropic"},
        )

        assert detection._auto_keys() == [key]

    def test_keeps_anthropic_when_it_is_the_only_framework(self, monkeypatch):
        monkeypatch.setattr(
            detection, "_module_loaded", lambda name: name == "anthropic"
        )

        assert detection._auto_keys() == ["anthropic"]

    @staticmethod
    def _importable(monkeypatch, *names):
        """Make exactly ``names`` importable, plus every registry instrumentor.

        Companions are checked by importability, which the machine running the
        suite would otherwise answer.
        """
        importable = set(names) | {
            path.partition(":")[0]
            for framework in detection._FRAMEWORKS
            for path in framework.instrumentors
        }
        monkeypatch.setattr(
            detection, "_module_available", lambda name: name in importable
        )

    def test_crewai_keeps_the_openai_key(self, monkeypatch):
        monkeypatch.setattr(
            detection,
            "_module_loaded",
            lambda name: name in {"crewai", "openai"},
        )
        self._importable(monkeypatch, "crewai", "openai")

        # Unlike agno, crewai supersedes nothing: the provider keys are where
        # its LLM spans come from.
        assert detection._auto_keys() == ["crewai", "openai"]

    def test_crewai_companions_are_found_by_importability(self, monkeypatch):
        # Only ``crewai`` is loaded: CrewAI imports its Anthropic provider once
        # an agent is built, after ``init``. Returned in registry order, not
        # in the order they were found.
        monkeypatch.setattr(
            detection, "_module_loaded", lambda name: name == "crewai"
        )
        self._importable(monkeypatch, "crewai", "openai", "anthropic")

        assert detection._auto_keys() == ["crewai", "anthropic", "openai"]

    def test_a_companion_whose_sdk_is_not_installed_is_skipped(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            detection, "_module_loaded", lambda name: name == "crewai"
        )
        self._importable(monkeypatch, "crewai", "openai")

        assert detection._auto_keys() == ["crewai", "openai"]

    def test_a_companion_whose_instrumentor_is_missing_is_skipped(
        self, monkeypatch
    ):
        # ``anthropic`` pulled in by something else, without the instrumentor:
        # turning the key on would make ``_load`` raise out of ``init`` for a
        # key nobody asked for.
        monkeypatch.setattr(
            detection, "_module_loaded", lambda name: name == "crewai"
        )
        monkeypatch.setattr(
            detection,
            "_module_available",
            lambda name: name
            in {
                "crewai",
                "openai",
                "anthropic",
                "openinference.instrumentation.openai",
            },
        )

        assert detection._auto_keys() == ["crewai", "openai"]

    def test_companions_need_their_framework_detected(self, monkeypatch):
        # ``crewai`` installed but not imported, with something else loaded:
        # its companions don't come along.
        monkeypatch.setattr(
            detection, "_module_loaded", lambda name: name == "agents"
        )
        self._importable(monkeypatch, "agents", "crewai", "anthropic")

        assert detection._auto_keys() == ["openai_agents"]

    def test_langchain_drops_both_companions_from_a_crew(self, monkeypatch):
        # Companions go through supersession like any other key.
        monkeypatch.setattr(
            detection,
            "_module_loaded",
            lambda name: name in {"crewai", "langchain_core"},
        )
        self._importable(
            monkeypatch, "crewai", "langchain_core", "openai", "anthropic"
        )

        assert detection._auto_keys() == ["langchain", "crewai"]

    def test_agno_drops_both_companions_from_a_crew(self, monkeypatch):
        # Supersession is process-wide, as the patching is: a self-recording
        # framework in the run costs the crew its LLM spans.
        monkeypatch.setattr(
            detection,
            "_module_loaded",
            lambda name: name in {"agno", "crewai"},
        )
        self._importable(monkeypatch, "agno", "crewai", "openai", "anthropic")

        assert detection._auto_keys() == ["agno", "crewai"]

    def test_crewai_keeps_the_openai_key_when_nothing_is_imported(
        self, monkeypatch
    ):
        # The importability fallback reaches the same pair: ``openai`` is a hard
        # dependency of CrewAI, so it is installed wherever crewai is.
        monkeypatch.setattr(detection, "_module_loaded", lambda _name: False)
        monkeypatch.setattr(
            detection,
            "_module_available",
            lambda name: name in {"crewai", "openai"},
        )

        assert detection._auto_keys() == ["crewai", "openai"]

    def test_langchain_drops_openai_from_a_crew(self, monkeypatch):
        monkeypatch.setattr(
            detection,
            "_module_loaded",
            lambda name: name in {"crewai", "langchain_core", "openai"},
        )
        self._importable(monkeypatch, "crewai", "langchain_core", "openai")

        assert detection._auto_keys() == ["langchain", "crewai"]

    def test_keeps_openai_when_it_is_the_only_framework(self, monkeypatch):
        monkeypatch.setattr(
            detection, "_module_loaded", lambda name: name == "openai"
        )

        assert detection._auto_keys() == ["openai"]


class TestModuleProbes:
    """The two questions detection asks about a framework, asked for real.

    :class:`TestAutoKeys` stubs both to control what is detected; these pin what
    they actually answer. Neither may import the module it is asked about:
    importing a framework to find out whether the program uses it would be a
    side effect Argus has no business causing.
    """

    @pytest.fixture
    def installed_but_unimported(self, tmp_path, monkeypatch):
        """An importable module that nothing in this process has imported."""
        name = "argus_fake_framework"
        (tmp_path / f"{name}.py").write_text("")
        monkeypatch.syspath_prepend(tmp_path)
        invalidate_caches()
        return name

    def test_loaded_sees_a_module_this_process_imported(self):
        # Nothing injected: argus is genuinely in sys.modules, since this file
        # imported it.
        assert detection._module_loaded("argus")

    def test_loaded_is_false_for_one_merely_installed(
        self, installed_but_unimported
    ):
        assert not detection._module_loaded(installed_but_unimported)
        assert installed_but_unimported not in sys.modules

    def test_available_finds_an_installed_module(
        self, installed_but_unimported
    ):
        assert detection._module_available(installed_but_unimported)
        # Answered from the module's spec, leaving it unimported -- which is what
        # makes this safe as the fallback signal.
        assert installed_but_unimported not in sys.modules

    def test_available_is_false_for_a_module_that_is_not_there(self):
        assert not detection._module_available("argus_no_such_framework")

    def test_a_companion_needs_its_sdk_and_its_instrumentor(
        self, installed_but_unimported
    ):
        def companion(detector, path):
            return detection._Framework("openai", detector, (path,))

        assert detection._companion_available(
            companion(installed_but_unimported, "tests.factories:Cls")
        )
        assert not detection._companion_available(
            companion(installed_but_unimported, "argus_no_such.pkg:Cls")
        )
        assert not detection._companion_available(
            companion("argus_no_such_framework", "tests.factories:Cls")
        )
        # Asked without importing the SDK, as detection's other probes are.
        assert installed_but_unimported not in sys.modules

    def test_available_is_false_when_the_probe_itself_fails(self):
        # A name whose parent package is missing makes the spec lookup raise
        # rather than return nothing, and an oddly-packaged module can do the
        # same. Both mean "not usable here", not "take the run down".
        assert not detection._module_available("argus_no_such_parent.child")
