"""Tests for trace output location and the script-name label.

:func:`~argus.paths.detect_script_name` feeds the ``service.name`` default and,
more consequentially, is embedded verbatim into every trace filename -- so a
name that isn't a portable filename component has to fall back rather than land
in a file name that is invalid (``<stdin>`` on Windows) or awkward.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from argus.paths import detect_script_name


@pytest.fixture
def fake_main(monkeypatch):
    """Set ``__main__`` to a stand-in whose ``__file__`` the test controls."""

    def _set(file):
        module = SimpleNamespace()
        if file is not None:
            module.__file__ = file
        monkeypatch.setitem(sys.modules, "__main__", module)

    return _set


class TestDetectScriptName:
    def test_uses_the_script_stem(self, fake_main):
        fake_main("/home/me/agents/my_agent.py")

        assert detect_script_name() == "my_agent"

    def test_keeps_hyphens_and_dots(self, fake_main):
        # Real script names carry these, and both are portable in a filename.
        fake_main("/srv/run-nightly.batch.py")

        assert detect_script_name() == "run-nightly.batch"

    def test_no_file_falls_back(self, fake_main):
        # A REPL or embedded interpreter: __main__ has no __file__ at all.
        fake_main(None)

        assert detect_script_name() == "session"

    def test_piped_stdin_falls_back(self, fake_main):
        # ``python - < script.py`` (and heredocs) set __file__ to "<stdin>",
        # whose angle brackets are invalid on Windows -- so it must not reach a
        # filename verbatim.
        fake_main("<stdin>")

        assert detect_script_name() == "session"

    def test_a_name_with_spaces_falls_back(self, fake_main):
        # A space is not a portable, clean filename component, so the sentinel
        # is preferable to embedding it verbatim.
        fake_main("/home/me/my agent.py")

        assert detect_script_name() == "session"
