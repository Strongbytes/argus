# Roadmap

What is planned for Argus, and what has already landed. This is a working
reference for contributors, not user documentation: the [README](README.md)
describes what Argus does today. When an item lands, its rationale moves to
[docs/design-notes.md](docs/design-notes.md) and the item shrinks to one line
under [Done](#done). Each item is one commit, with `black`, `isort`,
`ruff check`, `mypy` and `pytest` clean.

## Done

1. **A simple `anthropic` key**, for the Anthropic SDK used directly. See
   [Two Anthropic keys](docs/design-notes.md#two-anthropic-keys).
2. **Agno + Anthropic, and the Agno double count.** `agno` turns on
   `AgnoInstrumentor` alone and supersedes both provider keys. See
   [Provider keys: supersedes, companions, or
   neither](docs/design-notes.md#provider-keys-supersedes-companions-or-neither).
3. **CrewAI + Anthropic.** `crewai` turns on the provider keys as companions.
   See [CrewAI: wrapper mode, providers as
   companions](docs/design-notes.md#crewai-wrapper-mode-providers-as-companions).
4. **CrewAI + LangChain / LangGraph.** Companions go through supersession, so a
   mixed run drops both. Same design-notes section as item 3.
5. **`init` rejects a project that isn't a string**, raising `TypeError` before
   anything else runs. See the README's [`argus.init(...)`](README.md#argusinit)
   section.

## Open

6. [Nesting work started inside LangChain / LangGraph (optional)](#6-nesting-work-started-inside-langchain--langgraph-optional)
7. [A span scrubbing / redaction hook before export](#7-a-span-scrubbing--redaction-hook-before-export)

Both are independent of each other. Item 6 is optional, so decide first whether
it is wanted (see "Should you?"). Item 7 needs its shape decided before any code.

## 6. Nesting work started inside LangChain / LangGraph (optional)

**The problem.** Anything instrumented that runs _inside_ LangChain or
LangGraph code (a graph node, a tool, a `RunnableLambda`) starts a new trace
instead of nesting under the LangChain span it runs in. That covers a CrewAI
crew, an Agno agent, an OpenAI Agents SDK run, or a raw client call when its key
is on. Verified with a LangGraph graph whose `count` node kicks off a crew,
under `instrument=["crewai", "langgraph", "openai"]`, against the real OpenAI
API with `openinference-instrumentation-langchain` 0.1.78 and LangGraph 1.2.14.
One run produced three traces, written to three files:

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
`crew_with_langgraph_tool_oai.py` in `agents_instrumentation/agents/crewai/`).

**Why.** The LangChain instrumentor is a callback handler. Its `_start_trace`
parents a root run on the current OpenTelemetry context, which is why a graph
inside a crew nests. But it never attaches its own spans to that context, and a
comment in `_tracer.py` says this is deliberate: a callback system can't
guarantee the detach, and a leaked context would mis-parent every later span. So
whatever runs inside a node sees no current span. The provider duplicate that
lands in a trace of its own when LangChain and a provider key are both on has
the same cause (see [One instrumentor, two
names](docs/design-notes.md#one-instrumentor-two-names)).

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
  when LangChain isn't instrumented), and `use_span(None)` would _cut_ the
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
    - when it returns `None`, a span started inside keeps the _outer_ parent
      (the trap above);
    - when the import fails, the block runs and nothing changes.
- Works inside `async def`.
- Verify for real in a separate environment, as with CrewAI: the
  graph-runs-a-crew script above gives one trace with the crew under `count`.

**Commit:** `feat: nest_in_langchain puts work started inside LangChain under its span`.

## 7. A span scrubbing / redaction hook before export

**The gap.** [`blindspot`](README.md#excluding-code-from-tracing-argusblindspot)
keeps a whole scope off the record: the instrumentor spans inside it are never
created. There is no way to keep a span but strip part of it -- an API key in a
tool's arguments, an email address in a prompt, a customer record in a model's
output. Today the choice is a blindspot around the whole call, which loses its
structure and token counts too, or a custom exporter that rewrites spans before
writing them, which only covers the sink it wraps.

**What the existing design already decides.**

- **Before every sink, not inside one.** Both built-in sinks buffer spans and
  write or send them on `emit`, and `exporters=` can add more. A hook in one
  sink would leave the trace files, or the remote backend, with the raw
  payload.
- **Copies, not edits.** An ended `ReadableSpan`'s attributes are read-only, so
  the hook hands the sinks rewritten spans rather than changing them in place.
- **Configured at `init`.** [The session reports, it does not
  rewire](docs/design-notes.md#the-session-reports-it-does-not-rewire), so the
  hook is an `init` argument rather than something attached to a session
  afterwards.
- **It sees what OpenInference wrote.** Payloads arrive flattened into
  attributes (`input.value`, `output.value`, one set per chat message), often as
  JSON strings, so a hook matching on content has to look inside those strings.

**Open questions**

- The hook's shape: a callable per span returning its new attributes, a
  callable per attribute key and value, or a list of key patterns to blank out.
- Whether it may drop a span outright, which overlaps with `blindspot`.
- Unlike `blindspot`, it would naturally cover spans you start yourself, since
  it runs after they end. Decide whether that is documented as a feature or
  scoped away.

## Open questions from landed items

- **Expand companions for an explicit list?** An explicit `instrument="crewai"`
  turns on no LLM instrumentor; the docs say to write `["crewai", "openai"]` or
  `["crewai", "anthropic"]`. Expanding companions for explicit keys would
  remove that footgun, at the price of explicit no longer meaning exact. The
  design notes leave it open ([CrewAI: wrapper mode, providers as
  companions](docs/design-notes.md#crewai-wrapper-mode-providers-as-companions)).
- **Warn when the project name is also a key?** `argus.init("langchain")` is
  valid: it names the project "langchain" and auto-detects, though it reads as
  if it picked the LangChain instrumentor. `agents_instrumentation` writes every
  example that way, so a warning would fire on all of them, and the README
  sentence covers it instead. A warning when `project` equals a key _and_
  `instrument` is unset is noisy for those examples, but they could pass
  `instrument=` or use a neutral project name.

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
