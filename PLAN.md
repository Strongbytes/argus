# Plan: Anthropic, Agno and CrewAI provider coverage

What is left to implement. The LangChain, LangGraph and CrewAI work is done and
its rationale is in `docs/design-notes.md`. This is a working reference, not user
documentation: when the work lands, the rationale moves to the design notes and
this file can be deleted.

1. [A simple `anthropic` key](#1-a-simple-anthropic-key)
2. [Agno + Anthropic (and the Agno double count)](#2-agno--anthropic-and-the-agno-double-count)
3. [CrewAI + Anthropic](#3-crewai--anthropic)
4. [CrewAI + LangChain / LangGraph](#4-crewai--langchain--langgraph)

Findings were checked on 2026-10-09 with real Agno 3.1.2, CrewAI 1.15.26,
`openinference-instrumentation-agno` 1.0.14, `-crewai` 1.1.20, `-anthropic`
3.0.1 and `-openai` 0.1.64, against stub OpenAI and Anthropic servers. Versions
will drift.

## The rule all four follow

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

## Order and commits

1, then 2, then 3. Item 4 is checked as part of 3. One commit each, so the
changelog reads cleanly:

- `feat: anthropic key for the Anthropic SDK used directly`
- `fix: agno no longer pairs the OpenAI instrumentor` (`BREAKING CHANGE:`
  footer)
- `feat: crewai turns on its provider instrumentors as companions`

`black`, `isort`, `ruff check`, `mypy` and `pytest` clean for each.

## Out of scope

- Gemini, Bedrock and LiteLLM provider keys. Each is one key plus a line in
  `crewai`'s companions and in the self-recording frameworks' `supersedes`, once
  their instrumentors are checked.
- Dropping a provider LLM span only when it nests under a framework's LLM span
  (a span-processor dedupe). It would let Agno keep the raw OpenAI span without
  double counting, but can't help LangChain, whose spans don't nest.
