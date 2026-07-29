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

The naming scheme in full -- including the numeric tiebreaker for two traces
from the same run and second -- is one function, `argus.exporters.trace_filename`,
if you need to reproduce or parse a name rather than read one
([recipe](docs/examples.md#reproducing-a-traces-file-name),
[rationale](docs/design-notes.md#trace-filenames)). The tiebreaker is scoped to a
single run, so two processes writing to the same directory, second, and script
name can still overwrite each other's files.

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
| `service`     | script name          | Observed app identity; stamped as OpenTelemetry `service.name`. Defaults to the running script's name, falling back to `session` where there is no usable script name (a REPL, an embedded interpreter, or piped stdin). |
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

A flush is not a point of no return: leaving the `with` block writes what has
been captured so far, but tracing stays on and later spans are emitted by the
next flush (the `atexit` hook always performs one). See
[docs/examples.md](docs/examples.md#scoped-flushing-with-the-session) for the
recipe and [the design notes](docs/design-notes.md#a-flush-is-not-terminal) for
why repeat flushes are safe.

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

They report; they don't rewire. These are read-only (and `exporters` is a tuple)
because Argus drives the sinks and instrumentors wired to *this* session -- a
replacement or an appended sink would never receive spans. To change what is
recorded, call `argus.reset()` and `init` again. The class is exported so you can
annotate what `init` handed you; everything else on it, the constructor included,
is Argus's own and may change between releases. Why the surface is drawn this way
is in [the design notes](docs/design-notes.md#the-session-reports-it-does-not-rewire).

### Re-initializing in a notebook or REPL (`argus.reset()`)

Argus is a per-process singleton: a second `argus.init(...)` warns and returns
the first session unchanged, because instrumentors are global and a second
provider could not reliably receive their spans. That is right for a script and
wrong for a notebook, where re-running the `init` cell is how you change a
setting. `argus.reset()` retires the active session so the next `init` configures
everything afresh. It does **not** flush, and the `atexit` hook only flushes the
_active_ session, so whatever the retired one had buffered is gone unless you
flush it first.

See [docs/examples.md](docs/examples.md#notebooks-and-repls) for the recipe and
[the design notes](docs/design-notes.md#one-session-per-process) for why the
singleton exists.

### The span attribute ceiling

OpenTelemetry caps a span at **128 attributes** and silently evicts the oldest
ones beyond that. OpenInference spends several attributes on every chat message
(role, content, each tool call's id, name and arguments), so a long agent
conversation crosses 128 mid-run -- and what gets evicted includes the model's
final output message. `init` therefore raises the ceiling to **50,000**: far past
any realistic run, while keeping a rail against a pathological one.

The ceiling is fixed and not configurable -- it is neither an `init` argument
nor read from an environment variable. The only thing a lower value could buy is
silently capturing less of your trace, which is the exact loss the raised ceiling
exists to prevent, and the ceiling costs no memory until that many attributes
actually exist -- so there is no upside to tuning it down. Only the attribute
_count_ is raised: the limits on events, links, and attribute value lengths keep
whatever OpenTelemetry resolves for them. The reasoning is in [the design
notes](docs/design-notes.md#the-raised-span-attribute-ceiling).

## Remote export over OTLP

Besides the on-disk JSON, Argus can send spans to a backend over standard
OTLP/HTTP (protobuf POSTs). It runs _alongside_ the file exporter -- you keep the
local trace files and get remote ingest too. Install the extra and flip `otlp`
on:

```bash
pip install "argus-trace[otlp]"
```

Enabling `otlp` without that extra installed raises `ImportError` at your
`init(...)` line, naming the exact `pip install` needed. It fails there rather
than at exit -- the same fail-early stance the endpoint and key below follow.

```python
# True reads the endpoint from the standard OTel env vars and the key from
# AEGIS_API_KEY; an OtlpConfig(...) sets them (and headers/timeout) explicitly.
argus.init("my_project_name", otlp=True)
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
or omit `otlp`, to keep remote export off. For runnable setups -- `.env`, in-code
config, both together -- see [docs/examples.md](docs/examples.md#remote-export-over-otlp).

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
environment is left alone, so real deployment configuration always wins, making
this enough on its own:

```bash
AEGIS_API_KEY=sk_...
```

Pass it explicitly -- `OtlpConfig(api_key=...)` -- when it comes from somewhere
else, like a secrets manager.

The key is **required** whenever `otlp` is on, and it is resolved and checked
when `init` runs, so a missing or malformed key raises `ValueError` right at your
`argus.init(...)` line rather than during interpreter shutdown with the run's
whole trace already gone. There is no unauthenticated remote export; the local
trace files are always written regardless. Why the check happens at construction
is in [the design notes](docs/design-notes.md#credentials-resolved-at-construction).

The OTLP exporter follows the same lifecycle as the file exporter: it **buffers
spans and POSTs the whole run once, on exit**, not streamed mid-run. Accepted
spans leave the buffer, so a mid-run flush never re-sends them. The trade-offs --
and why the remote sink mirrors the file sink rather than streaming -- are in
[the design notes](docs/design-notes.md#buffer-now-emit-once).

A backend that rejects or can't be reached never crashes the run; the failure
surfaces as a `RuntimeWarning` (which `python -W error` can promote to an
exception) and the local trace files are written regardless. The warning
**names the cause** rather than saying only "not delivered": a `401` reads as a
rejected API key and points at `AEGIS_API_KEY`, a `403` as a key without access,
a `404` as a likely-wrong endpoint, a `5xx` as a transient backend problem, and
a connection error as the failure it raised. The credential never appears in
the message. Why it is done this way is in [the design
notes](docs/design-notes.md#delivery-failures-name-their-cause).

A transient failure (a `5xx`, a connection error) keeps its spans buffered, so a
later flush can still deliver them. A permanent one -- a rejected key, a wrong
endpoint, a malformed batch -- does not: re-sending could only be rejected the
same way, so those spans are dropped after the one warning rather than re-POSTed
and re-warned on every subsequent flush ([why](docs/design-notes.md#permanent-rejections-are-not-retried)).

A customized remote sink stays a one-argument decision: `OtlpConfig` carries the
headers and timeout too, so an `OtlpConfig(headers=..., timeout=...)` still runs
alongside the trace files. Authentication comes from `api_key`/`AEGIS_API_KEY`
either way, and an `Authorization` entry in `headers` is rejected rather than
silently overwritten.

For anything beyond those four settings -- compression, a shared HTTP session --
construct the exporter yourself and pass it through `exporters=`. It is named
`BufferedOTLPExporter` rather than `OTLPSpanExporter` so it doesn't shadow
OpenTelemetry's streaming exporter of that name
([why](docs/design-notes.md#names-that-dont-shadow-opentelemetrys)); since
`exporters=` replaces the default sink, name the file one alongside it when you
want both. See
[docs/examples.md](docs/examples.md#trace-files-and-a-remote-backend-together)
for the runnable form.

## Custom exporters

`exporters=` accepts any OpenTelemetry `SpanExporter`, and how Argus drives it
depends on whether it opts into the deferred lifecycle:

- A **plain `SpanExporter`** needs nothing special. Spans reach its `export`
  method synchronously as they end; Argus calls `force_flush()` on each flush
  with new spans to drain and `shutdown()` once at process exit.
- One that also defines **`emit(failed: bool)`** satisfies Argus's
  `BufferedSpanExporter` protocol and opts into the buffer-now, write-once
  lifecycle Argus's own two sinks use. Argus then calls `emit` instead of
  `force_flush`, passing whether the run ended in an unhandled exception -- which
  is what lets the file exporter tag a failed run in its filename.

`BufferedSpanExporter` is a runtime-checkable `typing.Protocol`, so a sink needs
no inheritance to be recognized; import it from `argus.exporters` when you want a
type checker to hold your sink to the contract. `emit` can be called more than
once, so a buffered sink has to decide what a repeat call does -- rewrite (as the
file sink does) or clear once delivered (as the OTLP sink does). The reasoning is
in [the design notes](docs/design-notes.md#repeat-emits-rewrite-or-clear).

Because `exporters=` replaces the default sink, `output_dir` no longer applies
(Argus warns if you pass both). To keep the trace files while adding a sink of
your own, name `FileSpanExporter()` alongside it -- with no arguments it is
exactly the sink `init` would have built (`<cwd>/traces`, under the running
script's name); both arguments override that, e.g.
`FileSpanExporter("./my_traces", script_name="my_agent")`.

See [docs/examples.md](docs/examples.md#a-sink-of-your-own) for a complete
buffered-sink example and
[typing one against the protocol](docs/examples.md#typing-a-sink-against-the-protocol).

## Excluding code from tracing (`argus.blindspot`)

Argus records everything by default. When a particular workflow -- or a slice of
one -- should stay off the record (secrets, PII, or just noise), wrap it in a
`blindspot`. It works as a context manager (sync or async) and as a decorator on
either kind of function.

What the scope suppresses is instrumentor spans: the flag Argus sets is the one
every OpenInference instrumentor checks before recording, so the model calls,
tool calls and agent steps inside the block never exist -- nothing is buffered or
written for them. A span you start yourself with `tracer.start_as_current_span`
is not subject to it; skip those yourself.

The suppression rides on the active context, so it follows `await` points and
copies into tasks spawned inside the block. It does **not** reach threads you
start yourself (a raw `threading.Thread` or a `ThreadPoolExecutor`), which begin
from a fresh context unless you explicitly copy it.

The decorator refuses generator and async-generator functions, raising
`TypeError` at decoration time; wrap the loop that *consumes* the generator
instead. See [docs/examples.md](docs/examples.md#keeping-a-scope-off-the-record)
for the recipes ([generators included](docs/examples.md#blindspots-around-a-generator)),
and [the design notes](docs/design-notes.md#why-the-decorator-refuses-generators)
for why generators are refused.

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
