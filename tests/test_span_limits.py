"""Tests for Argus's span-attribute ceiling and the env vars that set it.

The subject lives in :mod:`argus.limits`: reading the two OTel limit variables
and falling back to Argus's raised default is a self-contained concern
(``_attribute_cap_from_env`` and ``_resolve_span_limits``), separable from the
session lifecycle it feeds.
"""

from __future__ import annotations

import pytest

import argus
from argus import limits as limits_module


class TestAttributeCapFromEnv:
    """The three answers a limit variable can give, in one return value.

    ``_resolve_span_limits`` reads two variables and falls back to Argus's own
    default, so each read has to say "nothing usable here" as distinctly as it
    says "no ceiling at all" -- two states an ``Optional[int]`` would otherwise
    collapse into ``None``.
    """

    ENV = "ARGUS_TEST_ATTRIBUTE_LIMIT"

    @pytest.fixture(autouse=True)
    def clean_env(self, monkeypatch):
        monkeypatch.delenv(self.ENV, raising=False)

    def test_an_unset_variable_supplies_nothing(self):
        cap = limits_module._attribute_cap_from_env(
            self.ENV, empty_means_unlimited=True
        )

        assert cap is None

    def test_a_number_is_the_ceiling(self, monkeypatch):
        # Surrounding whitespace and all: a value read from a file or a shell
        # often arrives with a trailing newline.
        monkeypatch.setenv(self.ENV, " 8000 ")

        cap = limits_module._attribute_cap_from_env(
            self.ENV, empty_means_unlimited=True
        )

        assert cap == 8000

    def test_empty_is_unlimited_where_that_is_meaningful(self, monkeypatch):
        monkeypatch.setenv(self.ENV, "")

        cap = limits_module._attribute_cap_from_env(
            self.ENV, empty_means_unlimited=True
        )

        assert cap == limits_module._UNLIMITED

    def test_empty_supplies_nothing_where_it_is_not(self, monkeypatch):
        monkeypatch.setenv(self.ENV, "")

        cap = limits_module._attribute_cap_from_env(
            self.ENV, empty_means_unlimited=False
        )

        # OpenTelemetry cannot tell an empty generic variable from an unset one,
        # so neither does Argus: the next source decides.
        assert cap is None

    @pytest.mark.parametrize("value", ["garbage", "-5", "1.5"])
    def test_an_unusable_value_supplies_nothing(self, monkeypatch, value):
        monkeypatch.setenv(self.ENV, value)

        cap = limits_module._attribute_cap_from_env(
            self.ENV, empty_means_unlimited=True
        )

        assert cap is None

    def test_unlimited_cannot_be_mistaken_for_a_ceiling(self):
        # What makes one Optional[int] enough for three states: the sentinel is
        # negative, and a negative ceiling is never returned as a cap.
        assert limits_module._UNLIMITED < 0


class TestSpanLimits:
    """The raised span attribute ceiling and its env-var escape hatch.

    OpenTelemetry drops a span's oldest attributes once it exceeds 128, which
    silently loses the model's output on long conversations (OpenInference
    flattens each message into several attributes). Argus raises that ceiling.
    """

    ENV = limits_module._SPAN_ATTRIBUTE_COUNT_ENV_VAR
    GENERIC_ENV = limits_module._ATTRIBUTE_COUNT_ENV_VAR
    DEFAULT = limits_module._DEFAULT_MAX_SPAN_ATTRIBUTES

    @pytest.fixture(autouse=True)
    def clean_limit_env(self, monkeypatch):
        """Ignore the developer's own OTel limit vars; tests set what they need.

        Either variable can decide the ceiling, so a machine that happens to
        export one would satisfy the tests asserting the default applies.
        """
        monkeypatch.delenv(self.ENV, raising=False)
        monkeypatch.delenv(self.GENERIC_ENV, raising=False)

    def test_default_raises_ceiling_when_env_absent(self):
        limits = limits_module._resolve_span_limits()

        assert limits.max_span_attributes == self.DEFAULT

    def test_env_var_overrides_default(self, monkeypatch):
        monkeypatch.setenv(self.ENV, "8000")

        limits = limits_module._resolve_span_limits()

        assert limits.max_span_attributes == 8000

    def test_empty_env_var_means_unlimited(self, monkeypatch):
        monkeypatch.setenv(self.ENV, "")

        limits = limits_module._resolve_span_limits()

        assert limits.max_span_attributes is None

    @pytest.mark.parametrize("value", ["garbage", "-5"])
    def test_invalid_env_var_falls_back_to_default(self, monkeypatch, value):
        monkeypatch.setenv(self.ENV, value)

        limits = limits_module._resolve_span_limits()

        assert limits.max_span_attributes == self.DEFAULT

    def test_generic_attribute_env_var_is_honored(self, monkeypatch):
        monkeypatch.setenv(self.GENERIC_ENV, "256")

        limits = limits_module._resolve_span_limits()

        # OpenTelemetry applies the generic ceiling to span attributes only as
        # the default for the span-specific limit -- which the explicit value
        # Argus passes would shadow, silently beating a cap an operator set.
        assert limits.max_span_attributes == 256

    def test_span_specific_env_var_wins_over_generic(self, monkeypatch):
        monkeypatch.setenv(self.ENV, "8000")
        monkeypatch.setenv(self.GENERIC_ENV, "256")

        limits = limits_module._resolve_span_limits()

        assert limits.max_span_attributes == 8000

    def test_empty_generic_env_var_leaves_the_default_in_place(
        self, monkeypatch
    ):
        monkeypatch.setenv(self.GENERIC_ENV, "")

        limits = limits_module._resolve_span_limits()

        # Unlike the span-specific variable, an empty generic one is not "no
        # limit": OpenTelemetry cannot tell it from an unset one, so neither do
        # we, and our raised default stands.
        assert limits.max_span_attributes == self.DEFAULT

    def test_generic_env_var_still_governs_other_attribute_limits(
        self, monkeypatch
    ):
        monkeypatch.setenv(self.ENV, "8000")
        monkeypatch.setenv(self.GENERIC_ENV, "256")

        limits = limits_module._resolve_span_limits()

        # Only the span attribute count is ours to raise; event and link
        # attributes keep whatever OpenTelemetry resolves for them.
        assert limits.max_attributes == 256
        assert limits.max_event_attributes == 256
        assert limits.max_link_attributes == 256

    def test_init_provider_carries_raised_limit(
        self, use_instrumentors, recording_exporter
    ):
        use_instrumentors()

        session = argus.init("proj", exporters=[recording_exporter])

        # A private OTel attribute on purpose: it pins the exact ceiling, which
        # the behavioral test cannot. A rename here is the warning to recheck.
        assert session.provider._span_limits.max_span_attributes == self.DEFAULT

    def test_provider_retains_attributes_past_otel_default(
        self, use_instrumentors, recording_exporter
    ):
        # The regression guard: a span with far more than OTel's default of
        # 128 attributes must keep every one, so a long agent conversation
        # never loses its final output message to silent truncation.
        use_instrumentors()
        session = argus.init("proj", exporters=[recording_exporter])

        span = session.provider.get_tracer("test").start_span("response")
        for i in range(200):
            span.set_attribute(f"llm.input_messages.{i}.message.role", "tool")
        span.end()

        assert len(span.attributes) == 200
        assert span.dropped_attributes == 0
