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

The bare `pip install argus-trace` pulls only the thin core (OpenTelemetry +
`python-dotenv`); instrumentors are optional so Argus stays lightweight.

The `[otlp]` extra installs the OpenTelemetry OTLP/HTTP exporter package that
backs the built-in remote export (see [Remote export over OTLP](#remote-export-over-otlp)).

## Local development

To work on Argus itself, install from a checkout in editable mode together with
the `dev` dependency group -- black, isort, pytest, pytest-cov, and commitizen:

```bash
pip install -e . --group dev
```

`--group` reads the group from `pyproject.toml` and needs pip 25.1 or newer
(`python -m pip install -U pip`). There is no `requirements.txt`:
`pyproject.toml` is the single source of truth for every dependency Argus has --
runtime, extras, and dev.

Add the relevant `[…]` extra from above if you want to exercise a particular
instrumentor locally, e.g. `pip install -e ".[otlp]" --group dev`.

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
| `instrument`  | `None`               | `None`/`"curated"` = curated auto-detection; `"all"` = entry-point discovery; a key or list of keys (`"openai_agents"`, `["agno"]`). |
| `output_dir`  | `<cwd>/traces`       | Directory the default file exporter writes to. Configures that exporter only, so it has no effect (and warns) alongside `exporters`.  |
| `exporters`   | `[FileSpanExporter]` | Replace the default sink with your own OpenTelemetry exporters. See [Custom exporters](#custom-exporters).                            |
| `otlp`        | `None`               | Enable remote OTLP/HTTP export alongside the others. `True` reads the endpoint from the standard OTel env vars (raises if unset -- no default); a string sets the endpoint URL. See below. |
| `api_key`     | `AEGIS_API_KEY`      | Key authenticating the OTLP export, sent as `Authorization: Bearer <key>`. Required whenever `otlp` is on; defaults to the `AEGIS_API_KEY` env var. See below. |

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

# Or point it at your own ingest route explicitly:
argus.init("my_project_name", otlp="http://localhost:9000/api/v1/trace/ingest")
```

Argus ships **no default endpoint**: it's a library anyone can install, so
rather than guess a target (and risk quietly shipping traces to the wrong
backend) it requires one to be set. Resolution follows OpenTelemetry's own
precedence, first match winning:

| Source                                | Treated as                                        |
| ------------------------------------- | ------------------------------------------------- |
| `otlp="https://..."` argument         | the complete POST URL, used verbatim              |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`  | the complete POST URL, used verbatim              |
| `OTEL_EXPORTER_OTLP_ENDPOINT`         | a base for all signals, with `v1/traces` appended |

So a collector reachable at `http://localhost:4318` can be configured the
ordinary OpenTelemetry way, and Argus will POST to
`http://localhost:4318/v1/traces`. The first two sources get no path appended --
they are already the full route. Where OpenTelemetry would fall back to
`http://localhost:4318` with nothing set at all, Argus raises `ValueError`
instead. Timeouts come from the usual `OTEL_EXPORTER_OTLP_TRACES_TIMEOUT` var.

An `otlp=""` raises as well, rather than being read as "off". A blank string is
what `os.getenv("MY_ENDPOINT", "")` hands you when the variable is missing, and
treating it as falsy would disable export over what is almost always a
configuration slip. Pass `None`, or omit `otlp`, when you do mean to keep remote
export off.

The headers variables -- `OTEL_EXPORTER_OTLP_TRACES_HEADERS` and
`OTEL_EXPORTER_OTLP_HEADERS` -- **never take effect**. They apply only when no
headers are passed to the exporter in code, and Argus always passes them, since
that is how the API key below travels. Add extra headers through
`make_otlp_exporter(..., headers={...})` instead.

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
argus.init("my_project_name", otlp=True, api_key=fetch_secret("aegis"))
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

For full control (extra headers, timeout, compression, a shared HTTP session),
build the exporter yourself and pass it via `exporters=`. Authentication still
comes from `api_key`/`AEGIS_API_KEY` -- an `Authorization` entry in `headers`
is rejected rather than silently overwritten:

```python
from argus.exporters import make_otlp_exporter

argus.init(
    "my_project_name",
    exporters=[
        make_otlp_exporter(
            "http://localhost:9000/api/v1/trace/ingest",
            api_key="sk_…",       # or leave it to AEGIS_API_KEY
            timeout=10,
        ),
    ],
)
```

Note that passing `exporters=` replaces the default file exporter, so a
hand-built OTLP exporter means naming the file one too when you want both. Only
a *customized* remote sink needs this -- plain `otlp=True` already runs alongside
the files:

```python
from argus.exporters import FileSpanExporter, make_otlp_exporter

argus.init(
    "my_project_name",
    exporters=[
        FileSpanExporter("./traces", script_name="my_agent"),
        make_otlp_exporter(
            "http://localhost:9000/api/v1/trace/ingest",
            timeout=10,
        ),
    ],
)
```

`FileSpanExporter`'s two arguments are the values `init` would otherwise derive:
the directory to write into (`<cwd>/traces`, or whatever `output_dir` said) and
the name stamped into each filename (the running script's).

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

An exporter that also defines `emit(failed: bool)` opts into the buffer-now,
write-once lifecycle that Argus's own two sinks use. Argus then calls `emit`
instead of `force_flush`, passing whether the run ended in an unhandled
exception -- which is what lets the file exporter tag a failed run in its
filename. Buffer in `export`, do the real work in `emit`:

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

`emit` can be called more than once: a scoped flush emits what has accumulated,
and spans produced afterwards are emitted by the next flush. So decide what a
repeat call should do — Argus's own two sinks answer that differently, each way
suited to its destination. The file exporter keeps its buffer and rewrites the
trace's files, so each file is always the complete trace; the OTLP exporter
clears its buffer once the backend confirms the batch, so no span is POSTed
twice. The example above follows the OTLP shape, reporting only what is new.

Because `exporters=` replaces the default sink, `output_dir` no longer applies
(Argus warns if you pass both). To keep files while adding a sink of your own,
construct the file exporter yourself:

```python
from argus.exporters import FileSpanExporter

argus.init(
    "my_project_name",
    exporters=[
        FileSpanExporter("./my_traces", script_name="my_agent"),
        SummaryExporter(),
    ],
)
```

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

## Roadmap

- Span scrubbing/redaction hook before export.
