# Plan: LangChain, LangGraph and CrewAI support

Status:

- **LangChain and LangGraph: implemented** (uncommitted at the time of writing),
  with tests, README, examples and design notes, and verified against the real
  instrumentor. Examples are in `agents_instrumentation`
  (`agents/langchain/`, `agents/langgraph/`).
- **CrewAI: not started.**

This is a working reference for the implementation, not user documentation. When
the work lands, the rationale moves to `docs/design-notes.md` (the LangChain and
LangGraph part already has) and this file can be deleted.

Where the implementation differs from what this plan first said, the plan has
been corrected in place: the OpenAI duplicate is a separate trace, not a child
span (see Findings and Decision 2).

## Goal

Extend the curated registry in `src/argus/detection.py` so a bare
`argus.init(project)` detects and instruments LangChain, LangGraph and CrewAI,
the same way it already does for OpenAI Agents, Claude and Agno. Examples for
each framework come later, in `agents_instrumentation`.

## Findings (verified against PyPI and the instrumentor source)

Versions below are as of 2026-10-09 and will drift.

### LangChain and LangGraph share one instrumentor

- There is **no** `openinference-instrumentation-langgraph` on PyPI. Only
  `openinference-instrumentation-langchain` (0.1.78) exists.
- `LangChainInstrumentor` wraps
  `langchain_core.callbacks.BaseCallbackManager.__init__` and adds its tracer as
  an inheritable handler. LangGraph is built on `langchain-core`'s callback
  system, so the same handler traces graph runs. The package README states
  LangChain 1.x is built on LangGraph, and the tracer has LangGraph-specific
  handling (`on_interrupt`, streamed-message fallbacks, `RemoveMessage`).
- LangGraph can be installed without the `langchain` package, but never without
  `langchain-core`. So the correct detection signal is **`langchain_core`**, not
  `langchain`. Detecting on `langchain` would miss `langgraph` + `langchain-openai`
  setups, which are common.
- The instrumentor patches a class method, not a free function, so
  `free_functions=()`. No stale-binding risk, and `argus.bindings` needs no
  change.
- The tracer checks `_SUPPRESS_INSTRUMENTATION_KEY`, so `argus.blindspot`
  should work (to be verified across LangGraph's thread boundaries).
- `langchain-openai` calls the OpenAI SDK underneath. Running
  `OpenAIInstrumentor` alongside reports each model call twice with the same
  token counts, which double-counts on a backend. Verified with a real
  `ChatOpenAI`: the OpenAI span is **not** a child of LangChain's but a separate
  root trace, because `OpenInferenceTracer` never attaches its spans to the
  OpenTelemetry context (see its comment in `_tracer.py::_start_trace`). The run is
  double-counted and split.
- Requires Python `>=3.10,<3.15`.

### CrewAI is separate and does not emit LLM spans by default

- `openinference-instrumentation-crewai` (1.1.20) wraps class methods:
  `Crew.kickoff`, `Task._execute_core`, `Agent.kickoff`, `Flow.kickoff`,
  `Flow.kickoff_async`, `Flow._execute_method`, `BaseTool.run`, `Tool.run`, and
  the legacy memory classes where they still exist. It also patches
  `Agent._execute_without_timeout` and
  `CrewAgentExecutor._execute_single_native_tool_call` to propagate context into
  the timeout thread. `free_functions=()`.
- In the default (wrapper) mode it produces crew, task, agent and tool spans but
  **no LLM spans**; its README says to instrument the LLM client separately.
- CrewAI 1.x depends on `openai` (`>=2.30,<3`), so it pairs with
  `OpenAIInstrumentor`, exactly like Agno.
- An alternative `use_event_listener=True` mode creates LLM spans from CrewAI's
  `LLMCall*` events for any provider. Its README recommends it only for
  AMP/low-code usage and calls wrapper mode the recommended path for standard
  Python apps.
- Requires Python `>=3.10,<3.14`.

### Dependency conflict (matters for the examples step)

As of today `crewai` requires `openai<3` while `openai-agents` requires
`openai>=3`, and `agents_instrumentation/requirements.txt` pins
`openai==3.11.0`. CrewAI cannot share a venv with the current examples
requirements. Consequences:

- Do **not** add an `[all]` extra to Argus.
- The examples repo needs a separate environment or requirements file per
  framework.
- The examples repo's `.venv` is already out of step with its
  `requirements.txt` (`openai 1.109.1`, `argus-trace 0.7.0` installed).

## Decisions

1. **Add `langgraph` as an alias key alongside `langchain`.**
   Both resolve to the same `LangChainInstrumentor`; `resolve_instrumentors`
   already dedupes by class, so enabling both yields one instrumentor. The alias
   lets `instrument="langgraph"` and `argus-trace[langgraph]` work and lets the
   README list LangGraph honestly, for about six lines. Without it, a user typing
   `langgraph` gets an "Unknown instrument key" error. Document that the two
   names enable the same instrumentor.

2. **LangChain and LangGraph supersede the standalone `openai` key.**
   Avoids the duplicate-LLM-span and double-counted-token problem above. Known
   cost: a LangGraph node that calls the raw `openai` client directly goes
   untraced. Escape hatch: `instrument=["langchain", "openai"]` (explicit lists
   are not subject to supersession), which accepts the duplication for every
   LangChain model call. Document both.

3. **CrewAI uses wrapper mode paired with `OpenAIInstrumentor`**, not event-listener
   mode. Known limitation: LLM spans exist only for OpenAI-backed crews.
   Anthropic, Gemini or LiteLLM crews get structure spans but no LLM spans.
   Fixing that needs conditional pairing (add instrumentor X only if module Y is
   present), which the registry cannot express today. Deferred; document the
   limit.

4. **Two PRs.** LangChain + LangGraph first, CrewAI second, because CrewAI
   carries the LLM-coverage and environment questions. One `feat:` commit per
   framework so the Commitizen changelog reads cleanly.

## Implementation: Argus

### `src/argus/detection.py`

- Extend `InstrumentKey` with `"langchain"`, `"langgraph"`, `"crewai"`.
- Add registry entries:

```python
_Framework(
    "langchain",
    "langchain_core",
    ("openinference.instrumentation.langchain:LangChainInstrumentor",),
    supersedes=("openai",),
),
_Framework(
    "langgraph",
    "langgraph",
    ("openinference.instrumentation.langchain:LangChainInstrumentor",),
    supersedes=("openai",),
),
_Framework(
    "crewai",
    "crewai",
    (
        "openinference.instrumentation.crewai:CrewAIInstrumentor",
        "openinference.instrumentation.openai:OpenAIInstrumentor",
    ),
    supersedes=("openai",),
),
```

- Comment each entry: why `langchain_core` is the detector, why the `langgraph`
  alias exists, why `supersedes=("openai",)` is load-bearing for LangChain but
  cosmetic for CrewAI (which pairs the OpenAI instrumentor itself, as Agno does).
- Keep `openai` last in the registry as today. Registry order is application
  order, and it is not what prevents double instrumentation.
- No structural change to `_Framework`, `_auto_keys` or `resolve_instrumentors`
  is expected. If implementation shows otherwise, stop and revisit.

### `pyproject.toml`

```toml
langchain = ["openinference-instrumentation-langchain"]
langgraph = ["openinference-instrumentation-langchain"]
crewai = [
    "openinference-instrumentation-crewai",
    "openinference-instrumentation-openai",
]
```

- As with the existing extras, these install the instrumentor only, not the
  framework itself.
- Consider a `python_version < "3.14"` marker on the `crewai` extra.
- No `[all]` extra (see the dependency conflict).

### Tests

The fixture in `tests/test_detection.py` builds fake modules from the registry,
so new entries get stand-ins automatically. Add or extend:

- `langchain_core` in `sys.modules` selects `LangChainInstrumentor`.
- `langchain` and `langgraph` detected together yield exactly one instrumentor.
- `langchain` + `openai` detected yields only the LangChain instrumentor
  (`_auto_keys` level and the end-to-end `TestCuratedDetectionForReal` level).
- `crewai` selects `CrewAIInstrumentor` + `OpenAIInstrumentor` and drops
  standalone `openai`.
- `TestClassesForKeys`: the exact paths each new key resolves to.
- The existing invariants (`TestInstrumentVocabulary`, `TestSupersession`,
  "every framework turns something on") should pass with no edits. If they
  don't, that is a signal to investigate, not to loosen.
- `tests/test_session.py` / `test_bindings.py`: confirm nothing assumes four
  keys. Add a check that the new frameworks declare no free functions.

### Docs

- `README.md`: the instrumentor table, the install block (new extras), the
  `instrument` argument row, and the sentence "Of the four keys, `claude` is
  currently the only one with a free function...", which becomes seven keys.
  Note that `langchain` and `langgraph` enable the same instrumentor, that
  LangChain supersedes `openai` (with the escape hatch), and the CrewAI
  OpenAI-only LLM-span limit.
- `docs/examples.md`: one section per framework.
- `docs/design-notes.md`, under "Curated detection over entry points": one
  instrumentor with several names; detecting on `langchain_core`; why LangChain
  supersedes `openai`; why CrewAI stays in wrapper mode.
- `CHANGELOG.md` is generated by `cz bump` from the commit messages. Don't edit
  it by hand.

### Quality gates

`black`, `isort`, `ruff check`, `mypy` and `pytest` all clean, per the README.

## Verification with real runs

These need the real frameworks, so they belong to the examples step in
`agents_instrumentation`, each framework in its own environment.

LangChain / LangGraph:

- `ChatOpenAI` plus a tool: LLM and tool spans appear, with **no** duplicate
  OpenAI span. (Checked once against a local stub server: auto-detection gives one
  LLM span; forcing both gives a second, separate trace.)
- A `StateGraph` with a node calling `ChatOpenAI`; a prebuilt agent (LangChain 1.x
  `create_agent` or LangGraph's `create_react_agent`).
- Parallel branches (LangGraph's thread pool): parent/child nesting is correct.
- Async, and streaming (`stream` / `astream`).
- A node that raises: the run writes an `.error` trace file.
- `argus.blindspot` suppresses spans across LangGraph's thread boundaries.

CrewAI:

- A crew with a tool: LLM spans nest under agent and task spans and carry token
  counts.
- Context propagates through CrewAI's timeout thread.
- `Flow` execution.
- `argus.blindspot` behaves.
- `argus.reset()` followed by `init` re-instruments cleanly.

## Out of scope

- Examples in `agents_instrumentation` (next step), including per-framework
  environments.
- Conditional instrumentor pairing for non-OpenAI CrewAI providers (Anthropic,
  Gemini, LiteLLM).
- An event-listener mode option for CrewAI.
- Span scrubbing and redaction (already on the README roadmap).

## Open risks

- LangChain spans are not attached to the OpenTelemetry context, so any span
  started inside a LangChain node (a raw client call, a manual span) does not
  nest under it. CrewAI uses a different mechanism, so check that its OpenAI spans
  do nest under its agent and task spans.

- Version drift: the instrumentors and frameworks move quickly, and the Python
  ceilings (<3.14 for CrewAI, <3.15 for LangChain) may tighten.
- Detecting on `langchain_core` instruments any program that imports it, such as
  a library that pulls it in transitively. Detection prefers `sys.modules`, so
  only an actual import triggers it, but it is broader than detecting on
  `langchain`.
- On Python 3.10, LangChain async callbacks need explicit config passing in some
  cases. Argus supports 3.10; the examples run on 3.13.
