"""Tests for Argus's raised span-attribute ceiling.

OpenTelemetry drops a span's oldest attributes once it exceeds 128, which
silently loses the model's output on long conversations (OpenInference flattens
each message into several attributes). Argus fixes the ceiling far higher, and
does not let it be reconfigured -- so these pin the raised value and the
behavior it buys.
"""

from __future__ import annotations

import argus
from argus import session as session_module


class TestSpanLimits:
    DEFAULT = session_module._MAX_SPAN_ATTRIBUTES

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
