"""Tests for :mod:`argus.bindings` -- the pre-init import that traces nothing.

Every test here stands up a fake framework: a module in ``sys.modules`` holding
a free function, a registry entry declaring it, and other modules bound to that
function the way a caller's ``from framework import query`` would bind it.
Patching is done by hand, since what the check reads is not *how* a name was
rebound but only that its object changed -- exactly what an instrumentor's
``wrap_function_wrapper`` does to ``claude_agent_sdk.query``.

The fixtures deliberately keep the original function out of any module
namespace of their own (it is built inside a fixture, never assigned at module
level here), because a module-level reference in this file would itself be a
stale holder and show up in the findings.
"""

from __future__ import annotations

import sys
import warnings
from types import ModuleType

import pytest

import argus
from argus import bindings, detection

from tests.factories import FakeInstrumentor

_FRAMEWORK_MODULE = "argus_fake_sdk"


@pytest.fixture
def framework(monkeypatch):
    """A fake framework, imported already, whose ``query`` can be patched.

    Returns the stand-in module. Its registry entry is the only one detection
    knows for the duration, so nothing the machine running the suite happens to
    have installed can take part.
    """
    module = ModuleType(_FRAMEWORK_MODULE)
    # Defined here rather than at module level: a reference in this file's own
    # namespace would be a stale holder itself, and turn up in the findings.
    module.query = lambda: "original"
    monkeypatch.setitem(sys.modules, _FRAMEWORK_MODULE, module)
    monkeypatch.setattr(
        bindings,
        "_FRAMEWORKS",
        (
            detection._Framework(
                "claude",
                _FRAMEWORK_MODULE,
                ("argus_fake_instrumentation:Instrumentor",),
                free_functions=("query",),
            ),
        ),
    )
    return module


@pytest.fixture
def holder(monkeypatch):
    """Return a helper that binds names in a stand-in module, as an import would.

    ``holder("app", query=original)`` is what ``from argus_fake_sdk import
    query`` leaves behind in a module called ``app``.
    """

    def _holding(name, **bindings_to_make):
        module = ModuleType(name)
        for attribute, value in bindings_to_make.items():
            setattr(module, attribute, value)
        monkeypatch.setitem(sys.modules, name, module)
        return module

    return _holding


@pytest.fixture
def instrument(framework):
    """Return a helper that patches the framework the way an instrumentor does.

    Rebinding the module attribute to a wrapper is the whole of what
    ``ClaudeAgentSDKInstrumentor`` does to ``claude_agent_sdk.query``, and the
    whole of what the check detects.
    """

    def _instrument(attribute="query"):
        def wrapper():
            return "instrumented"

        setattr(framework, attribute, wrapper)
        return wrapper

    return _instrument


class TestSnapshot:
    """What is worth watching, decided before any instrumentor runs."""

    def test_watches_a_declared_free_function_of_a_loaded_framework(
        self, framework
    ):
        (watched,) = bindings.snapshot_free_functions()

        assert watched.key == "claude"
        assert watched.module == _FRAMEWORK_MODULE
        assert watched.attribute == "query"
        # The object itself, not its name: comparing identity later is the whole
        # of how a patch is recognized.
        assert watched.original is framework.query

    def test_skips_a_framework_that_is_not_imported_yet(
        self, framework, monkeypatch
    ):
        monkeypatch.delitem(sys.modules, _FRAMEWORK_MODULE)

        # Calling init first is the recommended order, and it needs no check: a
        # from-import binding cannot exist before the import that creates it.
        assert bindings.snapshot_free_functions() == ()

    def test_skips_a_framework_that_declares_no_free_functions(
        self, framework, monkeypatch
    ):
        monkeypatch.setattr(
            bindings,
            "_FRAMEWORKS",
            (detection._Framework("openai", _FRAMEWORK_MODULE, ("mod:Cls",)),),
        )

        # The class-based frameworks. Their methods resolve through the class at
        # call time, so an import before init is harmless and must not warn.
        assert bindings.snapshot_free_functions() == ()

    def test_skips_a_declared_name_the_framework_does_not_have(
        self, framework, monkeypatch
    ):
        monkeypatch.delattr(framework, "query")

        # An upstream rename. Watching an absent attribute would compare None
        # against None, or worse against a name that appears later.
        assert bindings.snapshot_free_functions() == ()

    def test_skips_a_name_that_does_not_hold_something_callable(
        self, framework
    ):
        # What ``import claude_agent_sdk.query`` leaves on the package: the
        # submodule, shadowing the function re-exported under the same name.
        framework.query = ModuleType(f"{_FRAMEWORK_MODULE}.query")

        assert bindings.snapshot_free_functions() == ()


class TestFindStaleBindings:
    """Which pre-patch names are still reachable, once instrumentation has run."""

    def test_a_framework_nothing_patched_yields_nothing(
        self, framework, holder
    ):
        holder("app", query=framework.query)
        watched = bindings.snapshot_free_functions()

        # instrument=[], or a framework no instrumentor covered. The name bound
        # in ``app`` is the live function, so there is nothing to report.
        assert bindings.find_stale_bindings(watched) == []

    def test_a_patched_function_nobody_else_holds_yields_nothing(
        self, framework, holder, instrument
    ):
        holder("app", framework=sys.modules[_FRAMEWORK_MODULE])
        watched = bindings.snapshot_free_functions()
        instrument()

        # ``import argus_fake_sdk`` then ``argus_fake_sdk.query(...)``: the
        # module attribute is read at call time, so the patch is picked up.
        assert bindings.find_stale_bindings(watched) == []

    def test_a_name_bound_before_the_patch_is_reported(
        self, framework, holder, instrument
    ):
        holder("app", query=framework.query)
        watched = bindings.snapshot_free_functions()
        instrument()

        (stale,) = bindings.find_stale_bindings(watched)

        assert stale.key == "claude"
        assert stale.module == _FRAMEWORK_MODULE
        assert stale.attribute == "query"
        # Named where it is bound, which is the line that has to change.
        assert stale.holders == ("app.query",)

    def test_an_aliased_import_is_reported_too(
        self, framework, holder, instrument
    ):
        holder("app", shortcut=framework.query)
        watched = bindings.snapshot_free_functions()
        instrument()

        (stale,) = bindings.find_stale_bindings(watched)

        # ``from argus_fake_sdk import query as shortcut``. Matching on the
        # object rather than on the attribute name is what finds this.
        assert stale.holders == ("app.shortcut",)

    def test_every_holder_is_reported_in_a_stable_order(
        self, framework, holder, instrument
    ):
        holder("second", query=framework.query)
        holder("first", query=framework.query)
        watched = bindings.snapshot_free_functions()
        instrument()

        (stale,) = bindings.find_stale_bindings(watched)

        # Sorted rather than left in sys.modules order, so the warning a caller
        # reads twice reads the same way twice.
        assert stale.holders == ("first.query", "second.query")

    def test_a_name_bound_after_the_patch_is_not_reported(
        self, framework, holder, instrument
    ):
        watched = bindings.snapshot_free_functions()
        wrapper = instrument()
        holder("app", query=wrapper)

        # The documented fix -- init first, import after -- must not be reported
        # as the bug it avoids.
        assert bindings.find_stale_bindings(watched) == []

    def test_the_frameworks_own_package_is_not_reported(
        self, framework, holder, instrument
    ):
        original = framework.query
        holder(f"{_FRAMEWORK_MODULE}.query", query=original)
        watched = bindings.snapshot_free_functions()
        instrument()

        # The submodule the function is defined in still refers to it, and a
        # caller can do nothing with that.
        assert bindings.find_stale_bindings(watched) == []

    @pytest.mark.parametrize(
        "module_name",
        [
            "argus.somewhere",
            "openinference.instrumentation.argus_fake_sdk",
            "wrapt.internals",
        ],
    )
    def test_the_instrumentation_machinery_is_not_reported(
        self, module_name, framework, holder, instrument
    ):
        holder(module_name, query=framework.query)
        watched = bindings.snapshot_free_functions()
        instrument()

        # An instrumentor keeps the original precisely so ``uninstrument`` can
        # put it back; reporting that would be a false alarm on every run.
        assert bindings.find_stale_bindings(watched) == []

    def test_an_unreadable_module_is_skipped_not_fatal(
        self, framework, monkeypatch, instrument
    ):
        class Shim:
            """A lazy-import stand-in whose namespace cannot be read."""

            @property
            def __dict__(self):
                raise RuntimeError("not a real module")

        monkeypatch.setitem(sys.modules, "argus_fake_shim", Shim())
        monkeypatch.setitem(sys.modules, "argus_fake_absent", None)
        watched = bindings.snapshot_free_functions()
        instrument()

        # ``sys.modules`` holds whatever an import hook put there. Neither entry
        # can be the caller's stale binding, and neither may take the run down.
        assert bindings.find_stale_bindings(watched) == []

    def test_a_framework_removed_after_the_snapshot_is_handled(
        self, framework, holder, instrument, monkeypatch
    ):
        holder("app", query=framework.query)
        watched = bindings.snapshot_free_functions()
        instrument()
        monkeypatch.delitem(sys.modules, _FRAMEWORK_MODULE)

        # A module deleted from sys.modules reads as patched (the attribute is
        # gone), and the binding in ``app`` is genuinely un-instrumented, so
        # reporting it is right -- and looking it up must not raise.
        (stale,) = bindings.find_stale_bindings(watched)

        assert stale.holders == ("app.query",)


class TestWarning:
    """The one warning a caller sees, and what it has to tell them."""

    @pytest.fixture
    def warned(self, framework, holder, instrument):
        """Return the warnings raised for one stale binding in ``app``."""
        holder("app", query=framework.query)
        watched = bindings.snapshot_free_functions()
        instrument()

        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            bindings.warn_stale_bindings(watched)
        return recorded

    def test_it_is_a_runtime_warning(self, warned):
        (warning,) = warned

        assert warning.category is RuntimeWarning

    def test_it_names_the_stale_binding_and_what_was_patched(self, warned):
        message = str(warned[0].message)

        # Everything needed to act without reading Argus's source: which import
        # is stale, and which function it should have reached.
        assert "app.query" in message
        assert f"{_FRAMEWORK_MODULE}.query" in message
        assert "imported before argus.init()" in message

    def test_it_names_both_fixes(self, warned):
        message = str(warned[0].message)

        # Either is enough, and they suit different code: move init above the
        # import, or call through the module.
        assert f"argus.init() before importing {_FRAMEWORK_MODULE}" in message
        assert f"calling {_FRAMEWORK_MODULE}.query(...)" in message

    def test_it_says_what_the_run_loses(self, warned):
        message = str(warned[0].message)

        # The symptom the caller actually arrived with: a run that produced
        # nothing at all, with no error to explain it.
        assert "no spans" in message
        assert "no trace file" in message

    def test_nothing_stale_stays_quiet(
        self, framework, holder, instrument, recwarn
    ):
        holder("app", framework=sys.modules[_FRAMEWORK_MODULE])
        watched = bindings.snapshot_free_functions()
        instrument()

        bindings.warn_stale_bindings(watched)

        assert list(recwarn) == []

    def test_one_warning_per_framework_however_many_names(
        self, framework, holder, instrument
    ):
        holder("app", query=framework.query, shortcut=framework.query)
        holder("helpers", query=framework.query)
        watched = bindings.snapshot_free_functions()
        instrument()

        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            bindings.warn_stale_bindings(watched)

        # One import order is one mistake to fix; repeating the same paragraph
        # per binding would bury it.
        (warning,) = recorded
        message = str(warning.message)
        assert "app.query" in message
        assert "app.shortcut" in message
        assert "helpers.query" in message

    def test_it_can_be_promoted_to_an_error(
        self, framework, holder, instrument
    ):
        holder("app", query=framework.query)
        watched = bindings.snapshot_free_functions()
        instrument()

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with pytest.raises(RuntimeWarning, match="app.query"):
                bindings.warn_stale_bindings(watched)

    def test_the_wording_agrees_with_a_single_name(
        self, framework, holder, instrument
    ):
        holder("app", query=framework.query)
        watched = bindings.snapshot_free_functions()
        instrument()

        message = bindings._describe(bindings.find_stale_bindings(watched))

        # A warning about one import must not read as though there were several.
        assert "app.query still points at" in message
        assert "through it" in message

    def test_the_wording_agrees_with_several(
        self, framework, holder, instrument
    ):
        holder("app", query=framework.query)
        holder("helpers", query=framework.query)
        watched = bindings.snapshot_free_functions()
        instrument()

        message = bindings._describe(bindings.find_stale_bindings(watched))

        assert "still point at" in message
        assert "through them" in message


class TestTheRealRegistry:
    """What Argus's own registry declares, rather than a fixture's stand-in."""

    def test_claude_declares_the_free_function_its_instrumentor_rebinds(self):
        # ClaudeAgentSDKInstrumentor wraps ``claude_agent_sdk.query`` and then
        # re-exports the wrapper on the package, so this is the name a
        # ``from claude_agent_sdk import query`` freezes.
        assert detection._BY_KEY["claude"].free_functions == ("query",)

    def test_the_class_based_frameworks_declare_none(self):
        # Their entry points are methods (``Runner.run``, ``agent.arun``,
        # ``client.chat.completions.create``), looked up through the class at
        # call time, so an import before init cannot freeze them. Declaring one
        # here would warn about an import order that is fine.
        for key in ("openai_agents", "agno", "openai"):
            assert detection._BY_KEY[key].free_functions == ()

    def test_declared_free_functions_are_plain_attribute_names(self):
        # They are read off the detector module with ``getattr``, so a dotted
        # path (``client.query``) would never match anything and the check would
        # quietly cover nothing.
        for framework in detection._FRAMEWORKS:
            for attribute in framework.free_functions:
                assert attribute.isidentifier()


class TestThroughInit:
    """The check as ``argus.init`` runs it, rather than called directly.

    What only this can cover is the ordering: ``init`` has to snapshot before it
    turns the instrumentors on, since a snapshot taken afterwards would record
    the wrapper and find every binding healthy. See ``docs/design-notes.md``
    ("Stale bindings from an import before init").
    """

    @pytest.fixture
    def patching_instrumentor(self, framework):
        """An instrumentor that rebinds the framework's free function, as a real
        one does, and is otherwise the suite's ordinary fake."""

        class PatchingInstrumentor(FakeInstrumentor):
            def instrument(self, **kwargs):
                super().instrument(**kwargs)
                framework.query = lambda: "instrumented"

        return PatchingInstrumentor()

    def test_init_reports_a_name_imported_before_it(
        self,
        framework,
        holder,
        patching_instrumentor,
        use_instrumentors,
        recording_exporter,
    ):
        use_instrumentors(patching_instrumentor)
        holder("app", query=framework.query)

        with pytest.warns(RuntimeWarning, match="app.query") as record:
            argus.init("proj", exporters=[recording_exporter])

        # The init() line is where the import order is decided, so that is what
        # the warning has to point at -- as every other init warning does.
        assert record[0].filename == __file__

    def test_init_stays_quiet_when_the_import_comes_after(
        self,
        framework,
        holder,
        patching_instrumentor,
        use_instrumentors,
        recording_exporter,
        recwarn,
    ):
        use_instrumentors(patching_instrumentor)

        argus.init("proj", exporters=[recording_exporter])
        holder("app", query=framework.query)

        # The documented order. Everything ``app`` binds is already the wrapper,
        # so the recommended fix must not be warned about.
        assert [w for w in recwarn if "argus.init()" in str(w.message)] == []

    def test_init_stays_quiet_when_nothing_patched_the_framework(
        self,
        framework,
        holder,
        use_instrumentors,
        recording_exporter,
        recwarn,
    ):
        use_instrumentors()
        holder("app", query=framework.query)

        argus.init("proj", exporters=[recording_exporter])

        # An instrumentor that leaves the free function alone (or an
        # ``instrument=[]`` run) leaves ``app.query`` pointing at the live
        # function. There is no stale binding to report.
        assert [w for w in recwarn if "imported before" in str(w.message)] == []
