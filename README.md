# Argus

A thin wrapper over [OpenInference](https://github.com/Arize-ai/openinference)
and [OpenTelemetry](https://opentelemetry.io/) that captures LLM agent traces.

Argus is the all-seeing companion to Aegis: it watches what your agents do and
records it. One call detects the agent framework in use, turns on the matching
OpenInference instrumentor(s), and persists each run's spans to disk as both
canonical OTLP/JSON (replayable to any OTLP backend) and a human-readable
rendering. The same call can also ship those spans to a remote backend over
OTLP/HTTP -- see [Remote export over OTLP](#remote-export-over-otlp).

```python
import argus
from agents import Agent, Runner   # OpenAI Agents SDK

argus.init("my_project_name")      # "my_project_name" is the project name; the
                                   # framework is auto-detected and traces
                                   # flush on exit

# ... run your agent ...
```

On process exit, each trace is written under `traces/` as **two** files sharing
a base name of `YYYY-MM-DD_HH-MM-SS_<script>` -- the timestamp is **UTC**, not
local time -- differing only by a format marker:

- `..._<script>.otlp.json` -- canonical OTLP/JSON, the exact shape the wire
  protocol uses, so it can be POSTed straight back to any OTLP backend.
- `..._<script>.readable.json` -- a human-readable rendering: a plain list of
  spans with embedded JSON payloads (prompts, completions, tool arguments)
  unescaped into real structure.

The date-first name keeps a directory listing sorted chronologically. A run that
ends in an unhandled exception is still captured, tagged with an `.error` marker
before the format suffix (`..._<script>.error.otlp.json` and
`..._<script>.error.readable.json`).

The scheme in full -- including the numeric tiebreaker a trace gets when a
sibling trace from the same run landed in the same second -- is one function,
`argus.exporters.trace_filename`, if you need to reproduce or parse a name
rather than read one. The tiebreaker is scoped to a single run: two processes
writing to the same directory in the same second, under the same script name,
can still overwrite each other's files.

## Documentation

Three places, each with one job:

| Where                                          | What's in it                                                                                     |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| This README                                    | The reference guide: every argument, default, and environment variable, with what each one means. |
| [docs/examples.md](docs/examples.md)            | A cookbook of complete, task-shaped examples -- per framework, per exporter, blindspots, notebooks, custom sinks. Start here if you'd rather read code than prose. |
| [docs/design-notes.md](docs/design-notes.md)    | The design decisions and their rationale: why spans are buffered until exit, why there is no default OTLP endpoint, why the blindspot decorator refuses generators, and the rest. |

## Installation

The package is named `argus-trace` and imported as `argus`. Install it with
`pip`, picking the extra that matches your agent framework:

```bash
pip install "argus-trace[openai-agents]"   # OpenAI Agents SDK
pip install "argus-trace[claude]"          # Claude Agent SDK
pip install "argus-trace[agno]"            # Agno
pip install "argus-trace[openai]"          # OpenAI client, used directly
pip install "argus-trace[otlp]"            # remote OTLP/HTTP export
```

The bare `pip install argus-trace` pulls only the thin core, which is three
packages:

| Package                                    | Why it's core                                                                                    |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------ |
| `opentelemetry-sdk`                        | The tracing pipeline Argus configures -- provider, span processors, exporters.                    |
| `opentelemetry-exporter-otlp-proto-common` | The OTLP protobuf span encoder (and, transitively, `protobuf`), which the default file sink needs to write canonical OTLP/JSON. This is the encoding layer only, not a network transport. |
| `python-dotenv`                            | Reads the nearest `.env` on `init`, which is what lets an API key stay out of your source (see [Authentication](#authentication)). |

Nothing else comes with it: the instrumentors and the network transport are
extras, which is what keeps the core light.

Argus is annotated throughout and ships a PEP 561 `py.typed` marker, so your type
checker reads those annotations straight out of the installed package -- no stub
package, no `Any` where an `OtlpConfig` or a `Session` should be.

The `[otlp]` extra installs the OpenTelemetry OTLP/HTTP exporter package that
backs the built-in remote export (see [Remote export over OTLP](#remote-export-over-otlp)).

## Local development

To work on Argus itself, install from a checkout in editable mode together with
the `dev` dependency group -- black, isort, ruff, mypy, pytest, pytest-cov, and
commitizen:

```bash
pip install -e . --group dev
```

`--group` reads the group from `pyproject.toml` and needs pip 25.1 or newer
(`python -m pip install -U pip`). There is no `requirements.txt`:
`pyproject.toml` is the single source of truth for every dependency Argus has --
runtime, extras, and dev.

Add the relevant `[…]` extra from above if you want to exercise a particular
instrumentor locally, e.g. `pip install -e ".[otlp]" --group dev`.

### Formatting, linting, and type checking

Four tools, each with a single job, all configured in `pyproject.toml` so none of
them needs flags:

```bash
black src tests      # formatting (80 columns)
isort src tests      # import order
ruff check src tests # lint: unused imports and arguments, likely bugs
mypy                 # type check src (strictly) and tests (lightly)
```

`black` and `isort` take `--check` if you want them to report rather than
rewrite. Ruff is a linter here only -- its formatter is deliberately not used, so
there is one formatter to answer to. Since the package ships a `py.typed` marker,
its annotations are part of its contract: every function in `src/` is annotated
and `mypy` enforces that.

### Running the tests

Run the suite with `pytest` from the repo root:

```bash
pytest
```

The tests use lightweight fakes for the instrumentors and exporters (see
`tests/factories.py`), so no agent-framework extras are required to run them.

To narrow a run down to a single file, test, or keyword while iterating:

```bash
pytest tests/test_session.py                 # one file
pytest tests/test_session.py::TestInit       # one class
pytest -k otlp                               # any test matching a keyword
```

Coverage is opt-in. Pass `--cov` to get a terminal report (the measured
package and the `term-missing` output are preconfigured in `pyproject.toml`, so
the bare flag is enough):

```bash
pytest --cov                       # terminal report with missing lines
pytest --cov --cov-report=html     # also write an htmlcov/ report to browse
```

## `argus.init(...)`

| Argument      | Default              | Notes                                                                                                                                |
| ------------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `project`     | (required)           | Argus's logical run umbrella; stamped onto every span as `argus.project`. May span several services.                                 |
| `service`     | script name          | Observed app identity; stamped as OpenTelemetry `service.name`. Defaults to the running script's name, or `session` where there is no script file (a REPL, an embedded interpreter). |
| `instrument`  | `None`               | `None`/`"curated"` = curated auto-detection; `"all"` = entry-point discovery; a key or list of keys (`"openai_agents"`, `["agno"]`). An unknown key raises `ValueError` listing the known ones. |
| `output_dir`  | `<cwd>/traces`       | Directory the default file exporter writes to. Configures that exporter only, so it has no effect (and warns) alongside `exporters`.  |
| `exporters`   | `[FileSpanExporter]` | Replace the default sink with your own OpenTelemetry exporters. See [Custom exporters](#custom-exporters).                            |
| `otlp`        | `None`               | Enable remote OTLP/HTTP export alongside the others. `True` takes the endpoint and key from the environment; an `OtlpConfig(...)` sets them (plus headers and timeout) explicitly. See below. |

`project` and `service` land on every span as resource attributes, alongside a
third Argus stamps itself: `argus.version`, the version of Argus that recorded
the trace, so a trace file read later says which release produced it.

`init` returns a `Session` that flushes automatically via `atexit`. It can also
be used as a context manager for deterministic, scoped flushing:

```python
with argus.init("my_project_name"):
    run_my_agent()
```

A flush is not a point of no return. Leaving the `with` block writes what has
been captured so far, but tracing stays on: spans produced by code that keeps
running afterwards are emitted by the next flush, which the `atexit` hook always
performs. Flushing twice with nothing new in between does no work, so the
context manager and the automatic flush cost nothing together.

### The `Session` object

Four read-only properties and one method are the whole of it:

| Member        | What it gives you                                                                                                                                             |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `provider`    | The `TracerProvider` spans come from. Emit your own spans through `provider.get_tracer(...)`, or point an instrumentor Argus doesn't know at it — both in [docs/examples.md](docs/examples.md). |
| `project`     | The run umbrella passed to `init`.                                                                                                                            |
| `instruments` | Class names of the instrumentors `init` turned on, e.g. `('OpenAIAgentsInstrumentor',)`.                                                                       |
| `exporters`   | The sinks a flush drives, as a tuple.                                                                                                                         |
| `flush()`     | Emit what has been captured so far. Safe to call repeatedly, and not terminal.                                                                                |

```python
session = argus.init("support-bot")
print(session.project)      # 'support-bot'
print(session.instruments)  # ('OpenAIAgentsInstrumentor',)
```

They report; they don't rewire. Argus drives the exporters and instrumentors on
flush, on exit and on `reset`, and the provider already carries this session's
span processors — so these are handed out to read, and none of them can be
replaced. Appending to `session.exporters` wouldn't work either, which is why it
is a tuple: a sink added after `init` gets driven on flush but never receives a
span, so it would write an empty trace. To change what is recorded, call
`argus.reset()` and `init` again. Everything else on the session — the
constructor included — is Argus's own and may change between releases; the class
is exported so you can annotate what `init` handed you.

### Re-initializing in a notebook or REPL (`argus.reset()`)

Argus is a per-process singleton: a second `argus.init(...)` warns and returns
the first session unchanged, because instrumentors are global and a second
provider could not reliably receive their spans. For a script that is what you
want. For a notebook or a REPL it isn't -- re-running the `init` cell is how you
change a setting -- so `argus.reset()` retires the active session and lets the
next `init` configure everything afresh:

```python
argus.reset()
argus.init("my_project_name", otlp=True)
```

`reset` does **not** flush, and the `atexit` hook only flushes the _active_
session -- so whatever the retired one had buffered is gone. Write it out first
if you still want it:

```python
session.flush()    # the Session that init returned
argus.reset()
```

### The span attribute ceiling

OpenTelemetry caps a span at **128 attributes** and silently evicts the oldest
ones beyond that. OpenInference spends several attributes on every chat message
(role, content, each tool call's id, name and arguments), so a long agent
conversation crosses 128 mid-run -- and what gets evicted includes the model's
final output message. `init` therefore raises the ceiling to **50,000**: far past
any realistic run, while keeping a rail against a pathological one.

It is deliberately not an `init` argument, since choosing the value well means
knowing how OpenInference flattens messages and a too-low value fails silently.
The standard OpenTelemetry variables are the escape hatch for the rare case that
needs one, in OpenTelemetry's own precedence:

| Variable                          | Read as                                                                                                                    |
| --------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT` | The span attribute ceiling. Set it to nothing at all for **no limit**.                                                      |
| `OTEL_ATTRIBUTE_COUNT_LIMIT`      | The all-signals fallback, consulted only when the span-specific variable gives nothing usable. Set to nothing at all it is indistinguishable from unset, so it is ignored rather than read as "no limit". |

Argus's 50,000 applies only when neither variable gives a usable value, and
anything that is not a non-negative integer counts as unusable and falls through
to the next source. Only the attribute _count_ is raised: the limits on events,
links, and attribute value lengths keep whatever OpenTelemetry resolves for them.
Why the generic variable is honored at all -- and why this isn't an argument --
is in [the design notes](docs/design-notes.md#the-raised-span-attribute-ceiling).

## Remote export over OTLP

Besides the on-disk JSON, Argus can send spans to a backend over standard
OTLP/HTTP (protobuf POSTs). It runs _alongside_ the file exporter -- you keep the
local trace files and get remote ingest too. Install the extra and flip `otlp`
on:

```bash
pip install "argus-trace[otlp]"
```

```python
import argus

# otlp=True reads the endpoint from the standard OTel env vars below and the
# API key from AEGIS_API_KEY (and raises if either is unset -- there is no
# default endpoint, and ingest is authenticated):
argus.init("my_project_name", otlp=True)

# Or configure it explicitly. OtlpConfig carries every remote setting:
argus.init(
    "my_project_name",
    otlp=argus.OtlpConfig("http://localhost:9000/api/v1/trace/ingest"),
)
```

`otlp` is the only remote-export argument, and `OtlpConfig` holds all four
settings, so there is no way to pass a key, a header or a timeout with no
endpoint to use it:

| Field      | Default                | Notes                                                                     |
| ---------- | ---------------------- | ------------------------------------------------------------------------- |
| `endpoint` | the OTel env vars      | Complete POST URL, used verbatim. See the resolution order below.         |
| `api_key`  | `AEGIS_API_KEY`        | Sent as `Authorization: Bearer <key>`. Required. See [Authentication](#authentication). |
| `headers`  | none                   | Extra HTTP headers sent alongside the credential (tenant or routing hints). |
| `timeout`  | the transport's own    | Per-export timeout in seconds; otherwise `OTEL_EXPORTER_OTLP_TRACES_TIMEOUT` applies. |

A bare `OtlpConfig()` therefore means the same thing as `otlp=True`. Pass `None`,
or omit `otlp`, to keep remote export off.

Argus ships **no default endpoint**: it's a library anyone can install, so
rather than guess a target (and risk quietly shipping traces to the wrong
backend) it requires one to be set. Resolution follows OpenTelemetry's own
precedence, first match winning:

| Source                                | Treated as                                        |
| ------------------------------------- | ------------------------------------------------- |
| `OtlpConfig(endpoint="https://...")`  | the complete POST URL, used verbatim              |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`  | the complete POST URL, used verbatim              |
| `OTEL_EXPORTER_OTLP_ENDPOINT`         | a base for all signals, with `v1/traces` appended |

So a collector reachable at `http://localhost:4318` can be configured the
ordinary OpenTelemetry way, and Argus will POST to
`http://localhost:4318/v1/traces`. The first two sources get no path appended --
they are already the full route. Where OpenTelemetry would fall back to
`http://localhost:4318` with nothing set at all, Argus raises `ValueError`
instead.

A blank endpoint -- `OtlpConfig("")`, which is what `os.getenv("MY_ENDPOINT", "")`
hands you when the variable is missing -- raises rather than falling back to the
environment, since it reads as an endpoint that failed to resolve rather than as
a request to look elsewhere. Surrounding whitespace is stripped, so a URL with a
trailing newline still works.

The headers variables -- `OTEL_EXPORTER_OTLP_TRACES_HEADERS` and
`OTEL_EXPORTER_OTLP_HEADERS` -- **never take effect**. They apply only when no
headers are passed to the exporter in code, and Argus always passes them, since
that is how the API key below travels. Use `OtlpConfig(headers={...})` instead.

### Authentication

Remote export is authenticated with an Aegis API key, sent as an
`Authorization: Bearer <key>` header. Keep it in the environment so it never
reaches your source -- `init` loads a `.env`, searching your working directory
and then **each parent directory above it**, nearest one winning (so a script in
a monorepo can pick up a key from a shared parent). Anything already set in the
environment is left alone, so real deployment configuration always wins. That
makes this enough on its own:

```bash
AEGIS_API_KEY=sk_...
```

```python
argus.init("my_project_name", otlp=True)   # key read from AEGIS_API_KEY
```

Pass it explicitly when it comes from somewhere else, like a secrets manager:

```python
argus.init("my_project_name", otlp=argus.OtlpConfig(api_key=fetch_secret("aegis")))
```

The key is **required** whenever `otlp` is on, and it is resolved and checked
when `init` runs, so a missing or malformed key raises `ValueError` right at
your `argus.init(...)` line. That is deliberate: because spans are buffered and
POSTed once on exit, a credential rejected at send time would surface as a
warning during interpreter shutdown -- with the run over and the whole trace
already gone. There is no unauthenticated remote export; the local trace files
are always written regardless.

The OTLP exporter follows the same lifecycle as the file exporter: it **buffers
spans in memory and POSTs the whole run once, on exit** (not streamed mid-run),
so the backend is hit a single time per run instead of absorbing a trickle of
batches. The trade-off is identical to the file sink's -- a hard kill before exit
loses the trace, since nothing was sent yet -- and a run's failure is carried on
each span's own status rather than in a filename. Spans the backend has accepted
are dropped from the buffer, so a program that flushes mid-run and keeps going
sends only the new spans in its next request, never a duplicate.

A customized remote sink stays a one-argument decision, because `OtlpConfig`
carries the headers and timeout too -- and it still runs alongside the trace
files. Authentication comes from `api_key`/`AEGIS_API_KEY` either way; an
`Authorization` entry in `headers` is rejected rather than silently overwritten:

```python
from argus import OtlpConfig

argus.init(
    "my_project_name",
    otlp=OtlpConfig(
        "http://localhost:9000/api/v1/trace/ingest",
        api_key="sk_…",              # or leave it to AEGIS_API_KEY
        headers={"x-tenant": "acme"},
        timeout=10,
    ),
)
```

For anything beyond those four settings -- compression, a shared HTTP session --
construct the exporter yourself and pass it through `exporters=`. It is named
`BufferedOTLPExporter` rather than `OTLPSpanExporter` so it doesn't shadow
OpenTelemetry's streaming exporter of that name. Since `exporters=` replaces the
default sink, name the file one too when you want both:

```python
from argus import FileSpanExporter
from argus.exporters import BufferedOTLPExporter

argus.init(
    "my_project_name",
    exporters=[
        FileSpanExporter(),
        BufferedOTLPExporter(
            "http://localhost:9000/api/v1/trace/ingest",
            timeout=10,
        ),
    ],
)
```

## Custom exporters

`exporters=` accepts any OpenTelemetry `SpanExporter`, and how Argus drives it
depends on whether it opts into the deferred lifecycle.

A plain `SpanExporter` needs nothing special. Spans are handed to its `export`
method synchronously as they end, Argus calls `force_flush()` on each flush
that has new spans to drain, and `shutdown()` once at process exit:

```python
from opentelemetry.sdk.trace.export import ConsoleSpanExporter

argus.init("my_project_name", exporters=[ConsoleSpanExporter()])
```

An exporter that also defines `emit(failed: bool)` satisfies Argus's
`BufferedSpanExporter` protocol and opts into the buffer-now, write-once
lifecycle its own two sinks use. Argus then calls `emit` instead of
`force_flush`, passing whether the run ended in an unhandled exception -- which
is what lets the file exporter tag a failed run in its filename. Buffer in
`export`, do the real work in `emit`:

```python
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

class SummaryExporter(SpanExporter):
    def __init__(self):
        self._spans = []

    def export(self, spans):
        self._spans.extend(spans)      # buffer only; nothing leaves yet
        return SpanExportResult.SUCCESS

    def emit(self, failed: bool = False):
        verdict = "failed" if failed else "ok"
        print(f"{len(self._spans)} spans, run {verdict}")
        self._spans = []

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True

    def shutdown(self) -> None:
        pass
```

`BufferedSpanExporter` is a runtime-checkable `typing.Protocol`, so the class
above needs no inheritance to be recognized -- Argus discovers it with
`isinstance`. Import it when you want a type checker to hold your sink to the
contract:

```python
from argus.exporters import BufferedSpanExporter

def build_sink() -> BufferedSpanExporter:
    return SummaryExporter()
```

`emit` can be called more than once: a scoped flush emits what has accumulated,
and spans produced afterwards are emitted by the next flush. So decide what a
repeat call should do — Argus's own two sinks answer that differently, each way
suited to its destination. The file exporter keeps its buffer and rewrites the
trace's files, so each file is always the complete trace; the OTLP exporter
clears its buffer once the backend confirms the batch, so no span is POSTed
twice. The example above follows the OTLP shape, reporting only what is new.

Because `exporters=` replaces the default sink, `output_dir` no longer applies
(Argus warns if you pass both). To keep the trace files while adding a sink of
your own, name the file exporter alongside it -- with no arguments it is exactly
the sink `init` would have built, writing to `<cwd>/traces` under the running
script's name:

```python
from argus import FileSpanExporter

argus.init(
    "my_project_name",
    exporters=[FileSpanExporter(), SummaryExporter()],
)
```

Both of its arguments override what `init` would derive, for the cases where you
want something else: `FileSpanExporter("./my_traces", script_name="my_agent")`.

## Excluding code from tracing (`argus.blindspot`)

Argus records everything by default. When a particular workflow -- or a slice
of one -- should stay off the record (secrets, PII, or just noise), wrap it in
a `blindspot`. Inside the scope no spans are created at all: suppression happens
at the source, so nothing is buffered or written.

It works as a context manager (sync or async) and as a decorator on either
kind of function:

```python
import argus

argus.init("my_project_name")

with argus.blindspot():            # synchronous block
    run_sensitive_step()

async with argus.blindspot():      # asynchronous block
    await run_sensitive_step()

@argus.blindspot()                 # whole function, sync or async
def internal_workflow(...):
    ...
```

The suppression rides on the active context, so it follows `await` points and
copies into tasks spawned inside the block. It does **not** reach threads you
start yourself (a raw `threading.Thread` or a `ThreadPoolExecutor`), which begin
from a fresh context unless you explicitly copy it.

### Generators

The decorator refuses generator and async-generator functions, raising
`TypeError` at decoration time. A generator runs inside whichever context its
consumer is in, so suppression held across a `yield` would leak out and silence
the consumer's code too -- and a wrapper that avoided the leak would leave the
generator's body unsuppressed instead. Both are silent failures, so Argus
declines rather than appear to protect a stream it isn't protecting.

Wrap the loop that consumes the generator instead. That covers the generator's
body and the code handling each item, which is usually what you wanted anyway:

```python
def stream_answer(prompt):        # left undecorated
    yield from llm.stream(prompt)

with argus.blindspot():           # covers the generator and the loop body
    for chunk in stream_answer(sensitive_prompt):
        handle(chunk)
```

## Instrumentor detection

By default Argus uses a curated registry, detecting the framework actually in
use (preferring already-imported modules) and avoiding double-instrumentation:

| Key             | Detected via       | Instrumentors                             |
| --------------- | ------------------ | ----------------------------------------- |
| `openai_agents` | `agents`           | `OpenAIAgentsInstrumentor`                |
| `claude`        | `claude_agent_sdk` | `ClaudeAgentSDKInstrumentor`              |
| `agno`          | `agno`             | `AgnoInstrumentor` + `OpenAIInstrumentor` |
| `openai`        | `openai`           | `OpenAIInstrumentor`                      |

Pass `instrument="all"` to instead load every instrumentor registered under
the `openinference_instrumentor` entry-point group.

Those keys are typed rather than merely documented: `instrument=` takes
`argus.detection.InstrumentKey` literals, so an editor completes them and a type
checker catches a misspelling before the run. A key assembled dynamically -- from
a config file, say -- still resolves at run time, where an unknown one raises
`ValueError` listing the known keys.

If auto-detection finds none of these, `init` warns (a `RuntimeWarning` naming
the known keys and `instrument=`) rather than silently tracing nothing. Pass
`instrument=[]` to set up exporters without instrumenting, deliberately.

Whichever route they arrive by, what Argus needs from an instrumentor is two
methods -- `instrument(tracer_provider=...)` and `uninstrument()` -- expressed as
the runtime-checkable `argus.detection.Instrumentor` protocol. OpenInference's
instrumentors satisfy it by way of OpenTelemetry's `BaseInstrumentor`, and so
does anything of your own that defines the two, with nothing to inherit from.

## Roadmap

- Span scrubbing/redaction hook before export.
