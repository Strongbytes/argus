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
        """

        def _in_use(*keys):
            for framework in detection._FRAMEWORKS:
                monkeypatch.delitem(
                    sys.modules, framework.detector, raising=False
                )
            for key in keys:
                detector = detection._BY_KEY[key].detector
                monkeypatch.setitem(sys.modules, detector, ModuleType(detector))

        return _in_use

    @pytest.mark.parametrize("selection", [None, "curated"])
    def test_a_detected_framework_turns_on_its_registry_entry(
        self, selection, frameworks_in_use
    ):
        frameworks_in_use("agno")

        result = detection.resolve_instrumentors(selection)

        # Agno's entry names two: its own instrumentor, plus the OpenAI one for
        # the client calls underneath it that the first does not cover.
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
        assert (
            detection._Framework("openai", "openai", ("mod:Cls",)).supersedes
            == ()
        )


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

        assert detection._classes_for_keys(["agno"]) == [
            "openinference.instrumentation.agno:AgnoInstrumentor",
            "openinference.instrumentation.openai:OpenAIInstrumentor",
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

    def test_agno_also_supersedes_standalone_openai(self, monkeypatch):
        monkeypatch.setattr(
            detection,
            "_module_loaded",
            lambda name: name in {"agno", "openai"},
        )

        # agno already pairs in the OpenAI instrumentor, so dropping the
        # standalone "openai" key changes the detected keys, not the classes.
        assert detection._auto_keys() == ["agno"]

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

    def test_available_is_false_when_the_probe_itself_fails(self):
        # A name whose parent package is missing makes the spec lookup raise
        # rather than return nothing, and an oddly-packaged module can do the
        # same. Both mean "not usable here", not "take the run down".
        assert not detection._module_available("argus_no_such_parent.child")
