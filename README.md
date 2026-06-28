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

argus.init("openai")               # auto-detects framework, flushes on exit

# ... run your agent ...
```

On process exit, spans are written under `traces/` as one indented JSON file per
trace, named `DD-MM-YY_HH:MM:SS_<script>.json`. A run that ends in an unhandled
exception is still captured, tagged with a `.error.json` suffix.

## Installation

The package is named `argus-trace` and imported as `argus`. Install it with
`pip`, picking the extra that matches your agent framework:

```bash
pip install "argus-trace[openai-agents]"   # OpenAI Agents SDK
pip install "argus-trace[claude]"          # Claude Agent SDK
pip install "argus-trace[agno]"            # Agno
pip install "argus-trace[otlp]"            # remote OTLP/HTTP exporter
```

The bare `pip install argus-trace` pulls only the thin core (OpenTelemetry +
`python-dotenv`); instrumentors are optional so Argus stays lightweight.

For local development, install from a checkout in editable mode:

```bash
pip install -e .            # core
pip install -r requirements-dev.txt   # editable install + formatting tools
```

## `argus.init(...)`

| Argument      | Default          | Notes |
| ------------- | ---------------- | ----- |
| `project`     | (required)       | Traces sub-directory and `service.name` on every span. |
| `instrument`  | `None`           | `None` = curated auto-detection; `"auto"` = entry-point discovery; a key or list of keys (`"openai_agents"`, `["agno"]`). |
| `output_dir`  | `<cwd>/traces`   | Directory traces are written to. |
| `exporters`   | `[FileSpanExporter]` | Swap in your own OpenTelemetry exporters (e.g. OTLP). |
| `load_dotenv` | `True`           | Load a `.env` found from the working directory. |

`init` returns a `Session` that flushes automatically via `atexit`. It can also
be used as a context manager for deterministic, scoped flushing:

```python
with argus.init("openai"):
    run_my_agent()
```

## Instrumentor detection

By default Argus uses a curated registry, detecting the framework actually in
use (preferring already-imported modules) and avoiding double-instrumentation:

| Key             | Detected via       | Instrumentors |
| --------------- | ------------------ | ------------- |
| `openai_agents` | `agents`           | `OpenAIAgentsInstrumentor` |
| `claude`        | `claude_agent_sdk` | `ClaudeAgentSDKInstrumentor` |
| `agno`          | `agno`             | `AgnoInstrumentor` + `OpenAIInstrumentor` |
| `openai`        | `openai`           | `OpenAIInstrumentor` |

Pass `instrument="auto"` to instead load every instrumentor registered under
the `openinference_instrumentor` entry-point group.

## Roadmap

- Remote export over standard OTLP/HTTP (streamed via `BatchSpanProcessor`),
  usable alongside the on-disk JSON exporter.
- Span scrubbing/redaction hook before export.
