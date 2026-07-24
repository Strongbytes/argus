# Argus

A thin wrapper over [OpenInference](https://github.com/Arize-ai/openinference)
and [OpenTelemetry](https://opentelemetry.io/) that captures LLM agent traces.

Argus is the all-seeing companion to Aegis: it watches what your agents do and
records it. One call detects the agent framework in use, turns on the matching
OpenInference instrumentor(s), and persists each run's spans to disk as readable
JSON.

```python
import argus
from agents import Agent, Runner   # OpenAI Agents SDK

argus.init("my_project_name")      # "my_project_name" is the project name; the
                                   # framework is auto-detected and traces
                                   # flush on exit

# ... run your agent ...
```

On process exit, spans are written under `traces/` as one indented JSON file per
trace, named `YYYY-MM-DD_HH-MM-SS_<script>.json`. A run that ends in an unhandled
exception is still captured, tagged with a `.error.json` suffix.

## Installation

The package is named `argus-trace` and imported as `argus`. Install it with
`pip`, picking the extra that matches your agent framework:

```bash
pip install "argus-trace[openai-agents]"   # OpenAI Agents SDK
pip install "argus-trace[claude]"          # Claude Agent SDK
pip install "argus-trace[agno]"            # Agno
pip install "argus-trace[otlp]"            # remote OTLP/HTTP export
```

The bare `pip install argus-trace` pulls only the thin core (OpenTelemetry +
`python-dotenv`); instrumentors are optional so Argus stays lightweight.

The `[otlp]` extra installs the OpenTelemetry OTLP/HTTP exporter package that
backs the built-in remote export (see [Remote export over OTLP](#remote-export-over-otlp)).

## Local development

To work on Argus itself, install from a checkout in editable mode. The dev
requirements pull in an editable install of the package plus the formatting
tools:

```bash
pip install -r requirements-dev.txt   # editable install (-e .) + black + isort + commitizen + pytest + pytest-cov
```

Install the relevant `[…]` extra from above as well if you want to exercise a
particular instrumentor locally.

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
| `service`     | script name          | Observed app identity; stamped as OpenTelemetry `service.name`. Defaults to the running script's name.                               |
| `instrument`  | `None`               | `None`/`"curated"` = curated auto-detection; `"all"` = entry-point discovery; a key or list of keys (`"openai_agents"`, `["agno"]`). |
| `output_dir`  | `<cwd>/traces`       | Directory traces are written to.                                                                                                     |
| `exporters`   | `[FileSpanExporter]` | Swap in your own OpenTelemetry exporters (e.g. OTLP).                                                                                |
| `otlp`        | `None`               | Enable remote OTLP/HTTP export alongside the others. `True` reads the endpoint from `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` (raises if unset -- no default); a string sets the endpoint URL. See below. |
| `load_dotenv` | `True`               | Load a `.env` found from the working directory.                                                                                      |

`init` returns a `Session` that flushes automatically via `atexit`. It can also
be used as a context manager for deterministic, scoped flushing:

```python
with argus.init("my_project_name"):
    run_my_agent()
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

# otlp=True reads the endpoint from OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
# (and raises if it is unset -- there is no default endpoint):
argus.init("my_project_name", otlp=True)

# Or point it at your own ingest route explicitly:
argus.init("my_project_name", otlp="http://localhost:9000/api/v1/trace/ingest")
```

Argus ships **no default endpoint**: it's a library anyone can install, so
rather than guess a target (and risk quietly shipping traces to the wrong
backend) it requires one to be set. The endpoint is used verbatim as the POST
URL (no `/v1/traces` path is appended for you). It can come from the `otlp=`
argument or, when you pass `otlp=True`, from the standard
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` environment variable; auth headers /
timeouts come from the usual `OTEL_EXPORTER_OTLP_TRACES_HEADERS` and
`OTEL_EXPORTER_OTLP_TRACES_TIMEOUT` vars. If neither the argument nor the env
var supplies an endpoint, `init` raises `ValueError`.

The OTLP exporter follows the same lifecycle as the file exporter: it **buffers
spans in memory and POSTs the whole run once, on exit** (not streamed mid-run),
so the backend is hit a single time per run instead of absorbing a trickle of
batches. The trade-off is identical to the file sink's -- a hard kill before exit
loses the trace, since nothing was sent yet -- and a run's failure is carried on
each span's own status rather than in a filename.

For full control (custom headers, timeout, compression, a shared HTTP session),
build the exporter yourself and pass it via `exporters=`:

```python
from argus.exporters import make_otlp_exporter

argus.init(
    "my_project_name",
    exporters=[
        make_otlp_exporter(
            "http://localhost:9000/api/v1/trace/ingest",
            headers={"authorization": "Bearer …"},
            timeout=10,
        ),
    ],
)
```

Note that passing `exporters=` replaces the default file exporter; combine
`make_otlp_exporter(...)` with a `FileSpanExporter` in the list if you want both.

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
