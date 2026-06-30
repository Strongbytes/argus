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
    """
    session_module._reset()
    yield
    session_module._reset()


@pytest.fixture
def traces_dir(tmp_path):
    """A temporary directory to write traces into."""
    return tmp_path / "traces"


@pytest.fixture
def instrumentor():
    """A single fresh fake instrumentor."""
    return make_instrumentor()


@pytest.fixture
def recording_exporter():
    """A fresh recording exporter."""
    return RecordingExporter()


@pytest.fixture
def use_instrumentors(monkeypatch):
    """Return a helper that patches detection to yield given instrumentors.

    Usage::

        received = use_instrumentors(inst_a, inst_b)
        argus.init("proj")
        assert received == [None]
    """

    def _use(*instances):
        return patch_resolve_instrumentors(monkeypatch, list(instances))

    return _use
