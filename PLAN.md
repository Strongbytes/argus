# Plan: provider coverage, nesting under LangChain, and `init` validation

What is left to implement. The LangChain, LangGraph and CrewAI work is done and
its rationale is in `docs/design-notes.md`. This is a working reference, not user
documentation: when the work lands, the rationale moves to the design notes and
this file can be deleted.

1. [A simple `anthropic` key](#1-a-simple-anthropic-key)
2. [Agno + Anthropic (and the Agno double count)](#2-agno--anthropic-and-the-agno-double-count)
3. [CrewAI + Anthropic](#3-crewai--anthropic)
4. [CrewAI + LangChain / LangGraph](#4-crewai--langchain--langgraph)
5. [`init` rejects a project that isn't a string](#5-init-rejects-a-project-that-isnt-a-string)
6. [Nesting work started inside LangChain / LangGraph (optional)](#6-nesting-work-started-inside-langchain--langgraph-optional)

Findings for items 1 to 4 were checked on 2026-10-09 with real Agno 3.1.2,
CrewAI 1.15.26, `openinference-instrumentation-agno` 1.0.14, `-crewai` 1.1.20,
`-anthropic` 3.0.1 and `-openai` 0.1.64, against stub OpenAI and Anthropic
servers. Items 5 and 6 came out of writing the CrewAI examples in
`agents_instrumentation/agents/crewai/`, run against the real OpenAI API with
`-langchain` 0.1.78 and LangGraph 1.2.14. Versions will drift.

## The rule items 1 to 4 follow

No registry entry pairs an instrumentor it doesn't own. A framework entry lists
only its own instrumentor, and relates to the provider keys (`openai`,
`anthropic`) in one of three ways:

| Framework's instrumentor...         | Entry declares                    | Keys                                              |
| ----------------------------------- | --------------------------------- | ------------------------------------------------- |
| records the model calls itself      | `supersedes` the provider keys    | `openai_agents`, `agno`, `langchain`, `langgraph` |
| records structure only              | `companions`: the provider keys   | `crewai`                                          |
| has no Python provider SDK under it | neither                           | `claude`                                          |

Explicit lists stay exact: never trimmed by supersession, never expanded by
companions. In one sentence: *provider spans are on unless a detected framework
already records the model calls.*

Patching is process-wide, so supersession is too. In a mixed run, a
structure-only framework (CrewAI) loses its LLM spans whenever a self-recording
one is detected, and a raw client call goes untraced next to any self-recording
framework. That is inherent, not an inconsistency. Naming the keys explicitly is
the escape hatch.

Target registry:

```python
_Framework("openai_agents", "agents", (OPENAI_AGENTS,), supersedes=("openai",)),
_Framework("claude", "claude_agent_sdk", (CLAUDE_AGENT_SDK,), free_functions=("query",)),
_Framework("agno", "agno", (AGNO,), supersedes=("openai", "anthropic")),
_Framework("langchain", "langchain_core", (LANGCHAIN,), supersedes=("openai", "anthropic")),
_Framework("langgraph", "langgraph", (LANGCHAIN,), supersedes=("openai", "anthropic")),
_Framework("crewai", "crewai", (CREWAI,), companions=("openai", "anthropic")),
_Framework("anthropic", "anthropic", (ANTHROPIC,)),
_Framework("openai", "openai", (OPENAI,)),
```

## 1. A simple `anthropic` key

For a script calling the Anthropic Python SDK directly, as `openai` is for the
OpenAI one.

**Findings**

- `openinference-instrumentation-anthropic` wraps class methods
  (`anthropic.resources.messages.Messages.create`, `.stream`, `.parse`, their
  async and `beta` counterparts), so `free_functions=()`. It is in the
  `openinference_instrumentor` entry-point group, so `instrument="all"` already
  reaches it. Python `>=3.10,<3.15`.
- `langchain-anthropic` depends on `anthropic`, and `ChatAnthropic` calls it, so
  this is the same duplication as `ChatOpenAI` (a second LLM span, in a separate
  trace). `langchain` and `langgraph` must supersede `anthropic` too.
- The Claude Agent SDK (`claude`) doesn't depend on `anthropic`. It drives the
  Claude Code CLI as a subprocess, so the model calls happen in another process.
  No overlap and no supersession either way. A script using both the Agent SDK
  and the raw client gets both keys, which is correct.
- The OpenAI Agents SDK reaches non-OpenAI models through LiteLLM, which doesn't
  call the Anthropic SDK. It is believed to need no `anthropic` supersession;
  verify during implementation.

**Changes**

- `detection.py`: `"anthropic"` in `InstrumentKey`. Entry `("anthropic",
  "anthropic", ("openinference.instrumentation.anthropic:AnthropicInstrumentor",))`,
  placed with `openai` at the end. `langchain` and `langgraph` gain `"anthropic"`
  in `supersedes`.
- `pyproject.toml`: `anthropic = ["openinference-instrumentation-anthropic"]`.
- Docs: README table row, install line, "eight keys". One sentence on the names:
  `claude` is the Claude Agent SDK, `anthropic` is the Anthropic API SDK. A short
  "Anthropic client, used directly" section in `docs/examples.md`, next to the
  OpenAI one. The LangChain paragraphs mention `ChatAnthropic` alongside
  `ChatOpenAI`.

**Tests**

- `anthropic` alone → `[AnthropicInstrumentor]`. `TestClassesForKeys` pins its
  path.
- LangChain / LangGraph + `anthropic` → LangChain only, at the `_auto_keys` level
  and end to end. Explicit `["langchain", "anthropic"]` keeps both.
- `test_bindings.py`: `anthropic` declares no free functions.
- `test_session.py`: the no-instrumentors warning lists `anthropic`.

**Verify:** a raw `anthropic` client call gives one `messages.create` LLM span
with tokens. `ChatAnthropic` under LangChain gives one LLM span.

## 2. Agno + Anthropic (and the Agno double count)

**Findings**

- `AgnoInstrumentor` walks every module under `agno.models` and wraps each model
  class's `invoke` / `ainvoke` / `invoke_stream` / `ainvoke_stream`. It emits an
  LLM span with `llm.provider`, model name and token counts, for every provider.
  The registry comment "Agno's instrumentor does not cover OpenAI calls" is wrong.
- So the released `agno` entry, which pairs `OpenAIInstrumentor`, double-counts.
  `agents_instrumentation/agents/agno/agent_with_tool_oai.py`'s shape (`arun`, a
  tool, a bare `init`):

  ```text
  Agent.arun [AGENT]
    OpenAIChat.ainvoke [LLM] provider=OpenAI prompt_tokens=11
      ChatCompletion [LLM] prompt_tokens=11       <- paired OpenAIInstrumentor
    get_weather [TOOL]
    OpenAIChat.ainvoke [LLM] provider=OpenAI prompt_tokens=11
      ChatCompletion [LLM] prompt_tokens=11       <- again
  ```

  The spans nest, but a backend summing tokens over LLM spans counts each call
  twice.
- An Agno agent on `Claude` already gets exactly one LLM span (`Claude.invoke`,
  provider=Anthropic, tokens). Turning on the Anthropic instrumentor as well
  (tried via `instrument="all"`) nests a second `messages.create` LLM span with
  the same tokens. So Agno + Anthropic needs **no** pairing: it needs Agno to
  supersede `anthropic`.
- The model wrapping already exists in `openinference-instrumentation-agno`
  0.1.5, so no version floor is needed. The instrumentor's README instruments
  Agno alone.
- Agno has no hard dependency on `openai` or `anthropic`; `import agno` loads
  neither.

**Changes**

- `detection.py`: the `agno` entry becomes `AgnoInstrumentor` alone, with
  `supersedes=("openai", "anthropic")`. That supersession is load-bearing now, so
  rewrite the "Cosmetic" comment with the finding.
- `pyproject.toml`: the `agno` extra drops `openinference-instrumentation-openai`.
- **What users see:** an Agno run on OpenAI loses the nested `ChatCompletion`
  span. Coverage is unchanged and tokens are counted once. The raw request
  parameters that span carried are available through `instrument=["agno",
  "openai"]`, accepting the double count. `instrument="agno"` now resolves to one
  instrumentor. A raw `openai` / `anthropic` client call inside an Agno script
  goes untraced, the same trade as LangChain's. Agno + LangChain stops
  duplicating `ChatOpenAI` calls as a side effect.
- Docs: README table row (Agno → `AgnoInstrumentor`) and the behaviour change.
  The `docs/examples.md` Agno section: one instrumentor, any model, Claude
  included. Design notes: why the pair went.

**Tests**

- Agno alone, Agno + `openai`, Agno + `anthropic` → `[AgnoInstrumentor]`, end to
  end and at `_auto_keys`.
- `TestClassesForKeys`: `agno` → one path.
- Rewrite the tests that pin the pair: `test_a_detected_framework_turns_on_its_registry_entry`
  (expects Agno + OpenAI), `test_resolves_each_path_for_a_known_key`, and
  `test_agno_also_supersedes_standalone_openai` (its "cosmetic" comment).
- Explicit `["agno", "openai"]` keeps both.

**Verify:** `agent_with_tool_oai.py`'s shape gives one LLM span per model call;
the same on Claude.

**Commit:** this changes what a released key turns on, so use `fix:` with a
`BREAKING CHANGE:` footer. Under `major_version_zero`, Commitizen bumps the
minor.

## 3. CrewAI + Anthropic

**Findings**

- CrewAI's instrumentor records structure only. LLM spans come from the provider
  instrumentors.
- Its native Anthropic provider (`anthropic/...` and `claude-...` models,
  `crewai[anthropic]`) calls the Anthropic SDK. But CrewAI imports that provider
  lazily, in `LLM._get_native_provider`, when an agent with a Claude model is
  built. In the usual script order (imports, then `argus.init`, then agents),
  `anthropic` is **not** in `sys.modules` when `init` runs. And because `crewai`
  is loaded, the importability fallback doesn't run. A plain `anthropic` key
  would therefore never be detected for a Claude-backed crew.
- `import crewai` does import `openai` (a hard dependency), which is why OpenAI
  crews work today through the `openai` key.

**Changes**

This is a structural change to the registry, the "stop and revisit" the earlier
plan anticipated: a new field and one extra step in `_auto_keys`. Supersession
itself is unchanged.

- `_Framework` gains `companions: tuple[InstrumentKey, ...] = ()`: provider keys a
  detected framework turns on alongside itself, checked by **importability**
  (`find_spec`) rather than `sys.modules`, because the framework imports its
  providers after `init`. Document it next to `supersedes`.
- `_auto_keys`: after finding candidates, add each detected framework's
  companions that aren't already candidates and pass the guard. Then apply
  supersession to the whole set as today, and return it in registry order.
- **Guard:** add a companion only if its detector (the provider SDK) **and** its
  instrumentor module are importable. The SDK can be installed without the
  instrumentor (pulled in by something else), and `_load` would otherwise raise
  out of `init` for a key nobody asked for. A companion whose instrumentor is
  missing is skipped.
- `crewai` entry: `companions=("openai", "anthropic")`. `openai` is redundant
  today, but listing it says what the entry means, and stops depending on
  CrewAI's import graph. Rewrite the entry comment around companions and the
  lazy provider import.
- `pyproject.toml`: the `crewai` extra adds
  `openinference-instrumentation-anthropic`, so the guard is a safety net, not
  the normal path.
- **Cost:** with `anthropic` installed but the crew on OpenAI, the Anthropic
  instrumentor patches a client that is never called. Harmless.
- **Explicit lists are not expanded**, which keeps "explicit means exact".
  `instrument="crewai"` still has no LLM instrumentor; the docs say
  `["crewai", "openai"]` / `["crewai", "anthropic"]`. **Open question:**
  expanding companions for explicit keys would remove that footgun, at the price
  of explicit no longer meaning exact.
- Docs: README CrewAI paragraph (Claude crews now get LLM spans; Gemini, Bedrock
  and LiteLLM still don't). The `docs/examples.md` CrewAI section, with a Claude
  example. Rewrite the design-notes subsection "CrewAI: wrapper mode, OpenAI
  through its own key" around companions, and add the rule above under "Curated
  detection over entry points".

**Tests**

- End to end: `crewai` loaded, `anthropic` importable but not loaded → CrewAI +
  Anthropic (+ OpenAI).
- `_auto_keys`: companions found by importability while `sys.modules` has only
  `crewai`. A companion skipped when its SDK is importable but its instrumentor
  isn't. Registry order kept.
- `TestSupersession` gains companion invariants: every companion is a known key;
  no framework both supersedes and companions the same key; a companion has no
  companions of its own (one level only); no framework is its own companion.
- Explicit `instrument="crewai"` → `[CrewAIInstrumentor]` (not expanded).

**Verify:** a crew on Claude, agents built after `init`, bare `init` →
`messages.create` LLM spans nested under the task, with tokens.
`instrument="all"` is unaffected (entry points, no companions).

**Commit:** `feat: crewai turns on its provider instrumentors as companions`.

## 4. CrewAI + LangChain / LangGraph

**Already done for OpenAI.** `crewai` doesn't pair `OpenAIInstrumentor`, so
`langchain`'s supersession drops the `openai` key in a mixed run, and each
`ChatOpenAI` call is reported once. This was verified with real CrewAI and
`langchain-openai`. The crew's own OpenAI calls lose their LLM spans, and
`instrument=["crewai", "langchain", "openai"]` restores them. Tests and docs are
in place.

**What the items above must preserve:**

- Companions go through supersession. CrewAI + LangChain must drop **both**
  companions, `openai` (as today) and `anthropic` (new, via item 1's
  `supersedes`). Add the test: `crewai` + `langchain_core` loaded, `anthropic`
  importable → LangChain + CrewAI only.
- Update the existing tests and docs that name only `openai` in this
  combination, and the escape hatch: `instrument=["crewai", "langchain",
  "anthropic"]` for a Claude crew.

**Verify:** a Claude crew plus a `ChatAnthropic` call: one LLM span for
`ChatAnthropic`, none for the crew's model calls; the explicit list restores
them.

## 5. `init` rejects a project that isn't a string

**The gotcha.** `init(project, *, instrument=None, ...)` takes the project name
first and the instrumentor selection only as a keyword. So
`argus.init(["crewai", "openai"], output_dir=...)` doesn't select instrumentors:

- The list becomes the project and is stamped as `argus.project`. OpenTelemetry
  accepts a string-array attribute, so nothing fails.
- `instrument` stays `None`, so auto-detection runs instead.

This happened while writing the CrewAI examples. For a plain crew,
auto-detection picked the same CrewAI + OpenAI pair, so nothing looked wrong.
For the crew with a LangChain tool, it silently dropped the `openai` key, and
the CrewAI agent's LLM spans were missing. Only reading the trace showed it.

mypy catches the call for a typed caller. The runtime check is for the
untyped scripts most users write, for the same reason `resolve_instrumentors`
still validates keys at runtime.

**Changes**

- `session.py`: `init` raises `TypeError` when `project` isn't a `str`, before
  anything else runs (the reinit check, `.env` loading, detection). The message
  names the type it got and points at the keyword, e.g. "project must be a
  string, got list. To choose instrumentors, pass them as
  `instrument=[...]`." A list or tuple is the likely mistake, but any non-`str`
  is rejected.
- `Session.__init__` needs no check of its own; `init` is the only public way
  in.
- Docs: in the README's `init` section, one sentence saying the first argument
  is the project name and the instrumentors go in `instrument=`.

**Not changed: a project name that is also a key.** `argus.init("langchain")` is
valid: it names the project "langchain" and auto-detects. It reads as if it
picked the LangChain instrumentor, and `agents_instrumentation` writes every
example that way. A warning would fire on all of them, so the docs sentence
covers it instead. **Open question:** warn when `project` equals a key *and*
`instrument` is unset? It is noisy for the examples, but they could pass
`instrument=` or use a neutral project name.

**Tests** (`test_session.py`)

- A list, a tuple and `None` as `project` → `TypeError` that mentions
  `instrument=`. No session is created and no instrumentor is turned on.
- The error is raised before `.env` is loaded (monkeypatch `_load_dotenv` and
  assert it wasn't called).
- A string project still works, including one that equals a key.

**Commit:** `fix: init rejects a project that isn't a string`. Only calls that
were already wrong start raising, so no `BREAKING CHANGE:` footer.

## 6. Nesting work started inside LangChain / LangGraph (optional)

**The problem.** Anything instrumented that runs *inside* LangChain or
LangGraph code (a graph node, a tool, a `RunnableLambda`) starts a new trace
instead of nesting under the LangChain span it runs in. That covers a CrewAI
crew, an Agno agent, an OpenAI Agents SDK run, or a raw client call when its key
is on. Verified with a LangGraph graph whose `count` node kicks off a crew,
under `instrument=["crewai", "langgraph", "openai"]`. One run produced three
traces, written to three files:

```text
LangGraph [CHAIN]                <- trace 1
  count [CHAIN]
  add_up [CHAIN]
    ChatOpenAI [LLM]
Crew.kickoff [CHAIN]             <- trace 2: should sit under `count`
  Shell operator._execute_core [AGENT]
    ChatCompletion [LLM] ...
ChatCompletion [LLM]             <- trace 3: add_up's OpenAI SDK duplicate
```

The other direction is fine. A crew whose tool runs a chain or a graph nests
everything under the tool span (`crew_with_langchain_tool_oai.py`,
`crew_with_langgraph_tool_oai.py`).

**Why.** The LangChain instrumentor is a callback handler. Its `_start_trace`
parents a root run on the current OpenTelemetry context, which is why a graph
inside a crew nests. But it never attaches its own spans to that context, and a
comment in `_tracer.py` says this is deliberate: a callback system can't
guarantee the detach, and a leaked context would mis-parent every later span. So
whatever runs inside a node sees no current span. Item 4's "separate trace"
duplicates have the same cause.

**The escape hatch exists.** `openinference.instrumentation.langchain` exports
`get_current_span()`. It reads LangChain's `var_child_runnable_config` context
variable and returns the span of the innermost running run. Verified: wrapping
the node body in `use_span(get_current_span(), end_on_exit=False)` puts the crew
under `count`, in one trace:

```text
LangGraph [CHAIN]
  count [CHAIN]
    Crew.kickoff [CHAIN]
      Shell operator._execute_core [AGENT]
        ChatCompletion [LLM]
        bash.run [TOOL]
        ChatCompletion [LLM]
```

The `with` block scopes the attach, so the leak upstream worries about doesn't
apply.

**Options**

- **A. Docs only.** Show the two-line escape hatch in `docs/examples.md`. No
  code, but users must know about it. They also need to handle the trap
  `get_current_span()` sets: it returns `None` outside a LangChain run (or
  when LangChain isn't instrumented), and `use_span(None)` would *cut* the
  block off from any outer parent rather than do nothing.
- **B. A small Argus helper (recommended, if you do this at all).** A context
  manager, e.g. `argus.nest_in_langchain()` (name open), used inside a node or
  tool:

  ```python
  def count(state):
      with argus.nest_in_langchain():
          return {"counts": crew.kickoff().raw}
  ```

  It does nothing when the LangChain instrumentor isn't installed or active, or
  when no run is current (`None`). Otherwise it is `use_span(span,
  end_on_exit=False)`. It is explicit, small, testable, and doesn't depend on
  LangGraph internals. Only a sync node was tried. Async nodes and parallel
  branches (which LangGraph runs on worker threads) should work, since
  LangChain copies its context variable into those threads, but verify them.
- **C. Automatic.** Wrap LangGraph's node runner (`RunnableCallable` in
  `langgraph._internal`), `RunnableLambda` and `BaseTool.run` so that every
  user function body runs with its LangChain span attached. No user code, but
  it patches private internals of two fast-moving packages. It would need the
  sync, async and streaming paths, and it doubles down on something upstream
  chose not to do. Worth it only if B proves too manual in practice.

**Should you?** It matters if LangGraph is used as an orchestrator over other
agents: a node that runs a crew, an Agno agent or an OpenAI Agents run. That is
a common pattern, and today each one lands in its own file. If your users only
call LangChain from inside other frameworks, or not at all, skip it. In auto mode
a raw client call inside a node isn't recorded at all (LangChain supersedes
`openai`), so another framework inside a node is the real case.

**Changes (option B)**

- New public function in `argus/__init__.py`, implemented next to the session
  code. It imports `openinference.instrumentation.langchain` lazily and treats
  `ImportError` as "do nothing". It doesn't check whether LangChain was
  selected: `get_current_span()` returns `None` when it wasn't, and that is
  the same no-op.
- Docs: a "Running other frameworks inside LangGraph" subsection in
  `docs/examples.md` with the node example and the before/after trees. A
  README sentence in the LangChain paragraph. A design-notes subsection on why
  upstream doesn't attach and why a scoped `with` is safe.

**Tests**

- The dev group has no LangChain, so fake
  `openinference.instrumentation.langchain.get_current_span`:
  - when it returns a span, a span started inside the block is its child;
  - when it returns `None`, a span started inside keeps the *outer* parent
    (the trap above);
  - when the import fails, the block runs and nothing changes.
- Works inside `async def`.
- Verify for real in a separate environment, as with CrewAI: the
  graph-runs-a-crew script above gives one trace with the crew under `count`.

**Commit:** `feat: nest_in_langchain puts work started inside LangChain under its span`.

## Order and commits

1, then 2, then 3. Item 4 is checked as part of 3. Item 5 is small and
independent, so it can go first or any time. Item 6 is optional, so decide
first whether it is wanted (see "Should you?"). One commit each, so the
changelog reads cleanly:

- `feat: anthropic key for the Anthropic SDK used directly`
- `fix: agno no longer pairs the OpenAI instrumentor` (`BREAKING CHANGE:`
  footer)
- `feat: crewai turns on its provider instrumentors as companions`
- `fix: init rejects a project that isn't a string`
- `feat: nest_in_langchain puts work started inside LangChain under its span`
  (if done)

`black`, `isort`, `ruff check`, `mypy` and `pytest` clean for each.

## Out of scope

- Gemini, Bedrock and LiteLLM provider keys. Each is one key plus a line in
  `crewai`'s companions and in the self-recording frameworks' `supersedes`, once
  their instrumentors are checked.
- Dropping a provider LLM span only when it nests under a framework's LLM span
  (a span-processor dedupe). It would let Agno keep the raw OpenAI span without
  double counting, but can't help LangChain, whose spans don't nest.
- CrewAI's framework adapters (`LangGraphAgentAdapter`, `OpenAIAgentAdapter`).
  They are broken upstream, so there is nothing for Argus to do.
  - They can't be created: `BaseAgent` gained abstract methods the adapters
    don't implement. It is two methods since 1.0 and three since 1.8, and still
    broken in 1.15.26, the latest release.
  - With those methods stubbed, three more problems surface in turn:
    - The `llm` field rejects a `ChatOpenAI`.
    - The task runner reads an `agent.last_messages` the adapter doesn't have.
    - The CrewAI tools never reach the graph.
  - Where it got far enough, CrewAI's task span already contained the LangGraph
    spans, so no Argus change should be needed once upstream fixes them. Until
    then, a crew reaches LangGraph through a tool
    (`crew_with_langgraph_tool_oai.py`).
