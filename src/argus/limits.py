"""Parse OpenTelemetry's attribute-limit env vars into a :class:`SpanLimits`.

A self-contained "read the OTel attribute-limit environment variables" concern,
kept out of :mod:`argus.session` so the parsing rules can be read and tested on
their own. :func:`init` calls :func:`_resolve_span_limits` and hands the result
to the :class:`~opentelemetry.sdk.trace.TracerProvider`. See
``docs/design-notes.md`` ("The raised span-attribute ceiling").
"""

from __future__ import annotations

import os

from opentelemetry.sdk.trace import SpanLimits

# OpenTelemetry caps span attributes at 128, low enough that a long agent
# conversation silently loses the model's final output. Argus raises the ceiling
# far past any realistic run, deliberately without an ``init`` argument, leaving
# the standard OTel env vars as the escape hatch. See ``docs/design-notes.md``
# ("The raised span-attribute ceiling").
_DEFAULT_MAX_SPAN_ATTRIBUTES = 50_000
_SPAN_ATTRIBUTE_COUNT_ENV_VAR = "OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT"
_ATTRIBUTE_COUNT_ENV_VAR = "OTEL_ATTRIBUTE_COUNT_LIMIT"
# "No ceiling at all", which is what the standard variables mean when set to
# nothing and what OpenTelemetry wants passed for it: given explicitly, ``UNSET``
# short-circuits its own env resolution and lands as ``None`` on the limits. It
# is negative, and a negative ceiling is never accepted from the environment,
# which is what keeps it distinguishable from a real cap below.
_UNLIMITED = SpanLimits.UNSET


def _attribute_cap_from_env(
    env_var: str, *, empty_means_unlimited: bool
) -> int | None:
    """Read an attribute-count ceiling from ``env_var``.

    Args:
        env_var: Name of the environment variable to read.
        empty_means_unlimited: How to read a variable set to nothing, mirroring
            OpenTelemetry's own split between the two variables.

    Returns:
        The ceiling the variable asks for, :data:`_UNLIMITED` for no ceiling at
        all, or ``None`` when it supplies nothing usable -- unset, malformed or
        negative -- so the caller falls through to the next source.
    """
    raw = os.environ.get(env_var)
    if raw is None:
        return None
    raw = raw.strip()
    if raw == "":
        return _UNLIMITED if empty_means_unlimited else None
    try:
        cap = int(raw)
    except ValueError:
        return None
    return cap if cap >= 0 else None


def _resolve_span_limits() -> SpanLimits:
    """Build span limits that raise the attribute ceiling past OTel's default.

    The standard environment variables win, in OpenTelemetry's own precedence:
    ``OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT`` first, then the generic
    ``OTEL_ATTRIBUTE_COUNT_LIMIT``; Argus's raised default applies only when
    neither is usable. Only the span attribute *count* is touched -- every other
    limit keeps whatever OpenTelemetry resolves for it. See
    ``docs/design-notes.md`` ("The raised span-attribute ceiling").
    """
    cap = _attribute_cap_from_env(
        _SPAN_ATTRIBUTE_COUNT_ENV_VAR, empty_means_unlimited=True
    )
    if cap is None:
        cap = _attribute_cap_from_env(
            _ATTRIBUTE_COUNT_ENV_VAR, empty_means_unlimited=False
        )
    if cap is None:
        cap = _DEFAULT_MAX_SPAN_ATTRIBUTES
    return SpanLimits(max_span_attributes=cap)
