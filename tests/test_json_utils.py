"""Tests for :func:`argus.json_utils.expand_embedded_json`."""

from __future__ import annotations

import pytest

from argus.json_utils import expand_embedded_json


def test_expands_embedded_object_string():
    assert expand_embedded_json('{"k": 1}') == {"k": 1}


def test_expands_embedded_array_string():
    assert expand_embedded_json("[1, 2, 3]") == [1, 2, 3]


def test_leaves_plain_text_untouched():
    assert expand_embedded_json("just a string") == "just a string"


def test_invalid_json_object_string_is_returned_verbatim():
    assert expand_embedded_json("{not valid json}") == "{not valid json}"


def test_recurses_into_dicts_and_lists():
    value = {
        "outer": '{"inner": "[1, 2]"}',
        "items": ['{"a": 1}', "plain"],
    }

    assert expand_embedded_json(value) == {
        "outer": {"inner": [1, 2]},
        "items": [{"a": 1}, "plain"],
    }


@pytest.mark.parametrize("value", [1, 1.5, True, None])
def test_non_string_scalars_pass_through(value):
    assert expand_embedded_json(value) == value
