"""Shared fixtures for the Argus test suite.

Argus keeps process-wide state on purpose -- a single active :class:`Session`,
a failure flag, and an installed ``excepthook`` -- so the cardinal rule for the
suite is that every test starts from a clean slate. The autouse
:func:`reset_argus_state` fixture guarantees that by tearing the singleton down
before and after each test.
"""

from __future__ import annotations

import pytest

from argus import session as session_module

from tests.factories import (
    RecordingExporter,
    make_instrumentor,
    patch_resolve_instrumentors,
)


@pytest.fixture(autouse=True)
def reset_argus_state():
    """Tear down Argus's per-process singleton around every test.

    Without this, the first ``init`` would pin its configuration for the whole
    session and later tests would silently get the no-op re-init path.
    ``reset`` does not flush, so tearing a test's session down never writes
    trace files as a side effect.
    """
    session_module.reset()
    yield
    session_module.reset()


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch):
    """Stop ``init`` from reading the developer's real ``.env``.

    ``init`` loads the nearest ``.env`` at or above the working directory -- in
    a checkout, whatever the person running the suite happens to keep there or
    in a parent. Left alone it would leak into tests -- most sharply into the
    ones asserting that ``init`` raises when no API key or endpoint is
    configured, which a stray local value would quietly satisfy. Tests that care
    about environment variables set them explicitly with ``monkeypatch``.
    """
    monkeypatch.setattr(session_module, "_load_dotenv", lambda: None)


@pytest.fixture(autouse=True)
def disposable_working_directory(tmp_path, monkeypatch):
    """Run every test somewhere throwaway, so none can write into the checkout.

    Argus anchors its default output at the working directory (see
    :func:`argus.paths.default_traces_dir`) -- right for a library that should
    not assume it lives in any particular repository, hostile to a suite run from
    one. A bare ``argus.init(project)`` builds a real
    :class:`~argus.exporters.file.FileSpanExporter`, whose constructor creates
    ``<cwd>/traces`` there and then, and a flush would fill it with trace files.

    Doing this once here closes that off for every test, including the ones
    nobody has written yet. Passing ``output_dir=`` still reads better in a test
    whose assertions are *about* where traces land, and the working directory is
    still the subject of a few tests, which set it themselves.
    """
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def traces_dir(tmp_path):
    """A temporary directory to write traces into.

    The same path Argus's own default resolves to, since
    :func:`disposable_working_directory` puts the working directory at
    ``tmp_path``: naming it explicitly is for tests that assert on the files
    written there.
    """
    return tmp_path / "traces"


@pytest.fixture
def recording_exporter():
    """A fresh recording exporter."""
    return RecordingExporter()


@pytest.fixture
def use_instrumentors(monkeypatch):
    """Return a helper that patches detection to yield given instrumentors.

    With no arguments, installs a single no-op instrumentor so tests that do
    not care about detection avoid the zero-instrumentor warning. Pass
    instrumentors explicitly when the selection or count matters; patch
    :func:`tests.factories.patch_resolve_instrumentors` with an empty list to
    simulate detection finding nothing.

    Usage::

        received = use_instrumentors(inst_a, inst_b)
        argus.init("proj")
        assert received == [None]
    """

    def _use(*instances):
        if not instances:
            instances = (make_instrumentor(),)
        return patch_resolve_instrumentors(monkeypatch, list(instances))

    return _use
