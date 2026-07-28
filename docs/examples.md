# Argus by example

Task-shaped recipes for Argus's public API: `init`, the `Session` it returns,
`blindspot`, `reset`, and the two exporters you can hand to `init`. Each example
shows everything Argus needs from you; what's elided is your own agent code, and
an `import argus` where an earlier example just made it obvious.

The [README](../README.md) is the reference (every argument, every environment
variable, every default) and [design-notes.md](design-notes.md) holds the
reasoning behind the behavior. This file is the middle ground: what to type for
the thing you're trying to do.

## Contents

- [The shortest thing that works](#the-shortest-thing-that-works)
- [OpenAI Agents SDK](#openai-agents-sdk)
- [Agno](#agno)
- [The OpenAI client, used directly](#the-openai-client-used-directly)
- [Choosing the instrumentors yourself](#choosing-the-instrumentors-yourself)
- [Every argument at once](#every-argument-at-once)
- [Where the trace files go](#where-the-trace-files-go)
- [Reproducing a trace's file name](#reproducing-a-traces-file-name)
- [Remote export over OTLP](#remote-export-over-otlp)
- [Trace files and a remote backend together](#trace-files-and-a-remote-backend-together)
- [A sink of your own](#a-sink-of-your-own)
- [Typing a sink against the protocol](#typing-a-sink-against-the-protocol)
- [Scoped flushing with the Session](#scoped-flushing-with-the-session)
- [Tagging a failure you handled yourself](#tagging-a-failure-you-handled-yourself)
- [Adding spans of your own](#adding-spans-of-your-own)
- [Keeping a scope off the record](#keeping-a-scope-off-the-record)
- [Blindspots around a generator](#blindspots-around-a-generator)
- [Notebooks and REPLs](#notebooks-and-repls)
- [A framework Argus doesn't know yet](#a-framework-argus-doesnt-know-yet)

## The shortest thing that works

One call, before the agent runs:

```python
import argus

argus.init("support-bot")

# ... run your agent ...
```

That detects the agent framework you have installed, turns on the matching
OpenInference instrumentor(s), and registers an `atexit` hook. When the process
ends you get two files under `./traces/` — canonical OTLP/JSON and a readable
rendering of the same trace. Nothing else is required, and nothing needs to be
torn down.

## OpenAI Agents SDK

`pip install "argus-trace[openai-agents]"`, then import the framework before
`init` so detection sees a module that is already loaded:

```python
import argus
from agents import Agent, Runner

argus.init("support-bot", service="triage-worker")

agent = Agent(name="Triage", instructions="Route the customer's request.")
result = Runner.run_sync(agent, "My order still hasn't arrived.")
print(result.final_output)
```

`project` groups runs the way you think about them; `service` is the observed
application's own identity, stamped as OpenTelemetry's `service.name`, and it
defaults to the script's name when you leave it out.

Importing first is a preference, not a requirement — Argus falls back to asking
whether the module is importable — but it is the reliable signal, and it costs
nothing to put your imports at the top.

## Agno

`pip install "argus-trace[agno]"`:

```python
import argus
from agno.agent import Agent
from agno.models.openai import OpenAIChat

argus.init("research-agent")

agent = Agent(model=OpenAIChat(id="gpt-4o-mini"))
agent.run("Summarize the last three papers on retrieval augmentation.")
```

Detecting Agno turns on two instrumentors: `AgnoInstrumentor` for the agent's
own steps and `OpenAIInstrumentor` for the model calls underneath them, since
Agno's instrumentor doesn't cover those itself. You get the whole stack from the
one `init`, without naming either.

## The OpenAI client, used directly

No agent framework needed — `pip install "argus-trace[openai]"` traces plain
client calls:

```python
import argus
from openai import OpenAI

argus.init("prompt-lab")

client = OpenAI()
client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "Two facts about Argus."}],
)
```

## Choosing the instrumentors yourself

Pass `instrument=` when detection would guess wrong, or when you want the
selection pinned in code rather than inferred from what happens to be installed:

```python
argus.init("support-bot", instrument="openai_agents")   # one curated key
argus.init("mixed", instrument=["agno", "claude"])      # several
argus.init("everything", instrument="all")              # entry-point discovery
argus.init("no-framework", instrument=[])               # instrument nothing
```

The curated keys are `openai_agents`, `claude`, `agno`, and `openai` (the README
has the table of what each detects and applies). A key that isn't one of them
raises `ValueError` listing the ones that are, so a typo fails at your `init`
line instead of producing a run with nothing instrumented.

`instrument="all"` loads every instrumentor advertised under the
`openinference_instrumentor` entry-point group, which is the escape hatch for a
framework the curated registry doesn't cover yet. `instrument=[]` sets up the
tracing pipeline and patches nothing, which is what you want when the only spans
you care about are [your own](#adding-spans-of-your-own).

## Every argument at once

Nothing here is required except the project name — this is a tour of the
keyword arguments rather than a template to copy:

```python
import argus
from argus import OtlpConfig

session = argus.init(
    "support-bot",                       # the run umbrella, stamped on every span
    service="triage-worker",             # the observed app's own identity
    instrument=["openai_agents"],        # skip detection, name the key
    output_dir="./artifacts/traces",     # where the default file sink writes
    otlp=OtlpConfig(                     # remote export, alongside the files
        "https://ingest.example.com/v1/traces",
        api_key="sk_live_…",             # or leave it to AEGIS_API_KEY
        headers={"x-tenant": "acme"},
        timeout=10,
    ),
)

print(session.project)      # 'support-bot'
print(session.instruments)  # ('OpenAIAgentsInstrumentor',)
```

The one argument missing from that list is `exporters=`, which *replaces* the
default file sink and therefore makes `output_dir` meaningless (Argus warns if
you pass both). It has [its own section](#a-sink-of-your-own).

## Where the trace files go

By default, `<cwd>/traces`, under the running script's name. Move the directory
with `output_dir`:

```python
argus.init("support-bot", output_dir="/var/log/argus")
```

Name the exporter yourself when you also want the file names to say something
other than the script's name — `FileSpanExporter()` with no arguments is exactly
the sink `init` would have built:

```python
from argus import FileSpanExporter

argus.init(
    "support-bot",
    exporters=[FileSpanExporter("/var/log/argus", script_name="triage")],
)
```

Either way you get two files per trace, sharing a base name and differing only
by a format marker: `..._triage.otlp.json` to replay to an OTLP backend, and
`..._triage.readable.json` to read, with prompts and tool arguments unescaped
into real structure. A run that dies on an unhandled exception is still written,
tagged `.error` before the marker.

## Reproducing a trace's file name

`trace_filename` is the naming scheme in one function, for code that builds or
parses the names rather than reading them:

```python
from datetime import datetime, timezone

from argus.exporters import trace_filename

moment = datetime(2026, 7, 27, 19, 30, 11, tzinfo=timezone.utc)

trace_filename("triage", "otlp", timestamp=moment)
# '2026-07-27_19-30-11_triage.otlp.json'

trace_filename("triage", "readable", timestamp=moment)
# '2026-07-27_19-30-11_triage.readable.json'

trace_filename("triage", "otlp", failed=True, timestamp=moment)
# '2026-07-27_19-30-11_triage.error.otlp.json'

trace_filename("triage", "otlp", failed=True, sequence=1, timestamp=moment)
# '2026-07-27_19-30-11_triage.error_1.otlp.json'
```

The timestamp is always rendered in UTC — an aware value is converted, a naive
one is taken as already UTC — so a listing sorts chronologically no matter where
it was captured. `sequence` is the tiebreaker for a second trace from the same
run in the same second.

## Remote export over OTLP

`pip install "argus-trace[otlp]"`, put the endpoint and key in the environment
(a `.env` at or above your working directory is enough — `init` loads the
nearest one):

```bash
AEGIS_API_KEY=sk_live_…
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

```python
argus.init("support-bot", otlp=True)
```

That POSTs the run to `http://localhost:4318/v1/traces` on exit, *and* keeps
writing the local trace files. Or configure it in code, which is the same
`OtlpConfig` shown in the tour above:

```python
from argus import OtlpConfig

argus.init(
    "support-bot",
    otlp=OtlpConfig("https://ingest.example.com/v1/traces", timeout=10),
)
```

There is no default endpoint and no unauthenticated export: with `otlp` on and
neither an endpoint nor a key resolvable, `init` raises `ValueError` right at
that line rather than warning during interpreter shutdown with the run already
over. A bare `OtlpConfig()` means the same as `otlp=True`.

## Trace files and a remote backend together

`otlp=` already runs alongside the files, so reach for this only when you need
something `OtlpConfig` doesn't carry — a compression setting, a shared HTTP
session. Build the exporter yourself and name the file sink too, since
`exporters=` replaces the default:

```python
from argus import FileSpanExporter
from argus.exporters import BufferedOTLPExporter

argus.init(
    "support-bot",
    exporters=[
        FileSpanExporter(),
        BufferedOTLPExporter(
            "https://ingest.example.com/v1/traces",
            timeout=10,
        ),
    ],
)
```

It is `BufferedOTLPExporter`, not `OTLPSpanExporter`, because OpenTelemetry
already has a class by that name that streams spans mid-run. This one buffers
and POSTs once.

## A sink of your own

`exporters=` takes any OpenTelemetry `SpanExporter`. A plain one needs nothing
special: spans arrive in `export` as they end, Argus calls `force_flush()` on
each flush and `shutdown()` at exit.

```python
from opentelemetry.sdk.trace.export import ConsoleSpanExporter

argus.init("support-bot", exporters=[ConsoleSpanExporter()])
```

Define `emit(failed: bool)` as well and your sink satisfies Argus's
`BufferedSpanExporter` protocol: Argus then drives it through `emit` and hands
over whether the run failed, which is how the built-in sinks tag a crashed run.
Buffer in `export`, act in `emit`:

```python
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from argus import FileSpanExporter


class FailureAlertExporter(SpanExporter):
    """Posts one alert per run, and only when the run ended badly."""

    def __init__(self, webhook: str) -> None:
        self._webhook = webhook
        self._spans = []

    def export(self, spans):
        self._spans.extend(spans)          # buffer only; nothing leaves yet
        return SpanExportResult.SUCCESS

    def emit(self, failed: bool = False) -> None:
        if failed and self._spans:
            post(self._webhook, f"run failed after {len(self._spans)} spans")
        self._spans = []                   # alerted once; don't repeat these

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True

    def shutdown(self) -> None:
        pass


argus.init(
    "support-bot",
    exporters=[FileSpanExporter(), FailureAlertExporter("https://hooks…")],
)
```

`emit` can run more than once — a scoped flush emits what has accumulated, and
later spans go out with the next one — so the last line of `emit` is a real
decision: clear the buffer when a repeat would duplicate something (an alert, a
POST), keep it when a repeat supersedes it (a file rewritten from scratch).

## Typing a sink against the protocol

`BufferedSpanExporter` is a runtime-checkable `Protocol`, so the class above is
recognized without inheriting anything. Import it when you want a type checker
holding your sink to the contract:

```python
from argus.exporters import BufferedSpanExporter


def build_sink() -> BufferedSpanExporter:
    return FailureAlertExporter("https://hooks…")
```

## Scoped flushing with the Session

The `atexit` hook is enough for a script. Use the session as a context manager
when you want the files written at a point you choose:

```python
import argus

with argus.init("nightly-batch"):
    run_batch()

# Traces for everything above are on disk here.
```

Leaving the block is not a point of no return: tracing stays on, and spans from
code that keeps running afterwards are emitted by the next flush — which the
`atexit` hook always performs. Flushing with nothing new buffered does no work,
so the context manager and the automatic flush cost nothing together.

## Tagging a failure you handled yourself

Argus notices an unhandled exception through its excepthook. If you catch the
failure, say so on the flush and the trace files get the `.error` marker:

```python
session = argus.init("nightly-batch")

try:
    run_batch()
except BatchError:
    session.flush(failed=True)   # you handled it; record it as a failed run
    raise SystemExit(1)
```

## Adding spans of your own

`session.provider` is the configured `TracerProvider`, so your own spans land in
the same trace files as the instrumented ones:

```python
session = argus.init("nightly-batch")
tracer = session.provider.get_tracer("nightly-batch")

with tracer.start_as_current_span("load-customers") as span:
    span.set_attribute("customer.count", len(customers))
    load(customers)
```

## Keeping a scope off the record

Wrap anything that should not be traced — secrets, PII, or noise you don't want
in the file — in a `blindspot`. It works as a sync or async context manager and
as a decorator on either kind of function:

```python
import argus

argus.init("support-bot")

with argus.blindspot():                     # synchronous block
    verify_customer_identity(ssn)


async def handle_request(ssn):
    async with argus.blindspot():           # asynchronous block
        await verify_customer_identity(ssn)


@argus.blindspot()                          # every call to this function
def rotate_credentials(vault):
    ...
```

Suppression rides on the active context, so it follows `await` points and copies
into tasks spawned inside the block. It does **not** reach threads you start
yourself, which begin from a fresh context — wrap the work inside the thread
instead. A single `blindspot()` instance is safe to reuse, nest, and share
across threads and tasks.

What the scope actually suppresses is instrumentor spans: the flag Argus sets is
the one every OpenInference instrumentor checks before recording anything, so
model calls, tool calls and agent steps inside the block never exist. A span you
start yourself with `tracer.start_as_current_span` is not subject to it — skip
those yourself.

## Blindspots around a generator

Decorating a generator function raises `TypeError` at decoration time, because a
generator runs in its consumer's context: suppression held across a `yield`
would leak out and silence the consumer, and a wrapper that avoided the leak
would leave the generator's body unprotected. Wrap the consumption instead,
which covers the generator's body *and* the code handling each item:

```python
def stream_answer(prompt):          # left undecorated
    yield from llm.stream(prompt)


with argus.blindspot():             # covers the generator and the loop body
    for chunk in stream_answer(sensitive_prompt):
        handle(chunk)
```

## Notebooks and REPLs

Argus is a per-process singleton: a second `init` warns and returns the first
session unchanged, which is right for a script and wrong for a notebook where
re-running the `init` cell is how you change a setting. `reset()` retires the
active session so the next `init` configures everything afresh:

```python
argus.reset()
argus.init("support-bot", otlp=True)
```

`reset` deliberately does not flush, and the `atexit` hook only flushes the
*active* session — so write out what the old one buffered first if you still
want it:

```python
session.flush()     # the Session that init returned
argus.reset()
```

## A framework Argus doesn't know yet

`instrument=` takes curated keys, so an instrumentor of your own goes on
afterwards, pointed at the session's provider. Argus needs only two methods from
it, expressed as the `Instrumentor` protocol — there is nothing to inherit:

```python
from argus.detection import Instrumentor


class MyFrameworkInstrumentor:
    def instrument(self, *, tracer_provider=None, **kwargs) -> None:
        my_framework.set_trace_hook(tracer_provider)

    def uninstrument(self, **kwargs) -> None:
        my_framework.clear_trace_hook()


def build() -> Instrumentor:      # the protocol is structural; this checks
    return MyFrameworkInstrumentor()


session = argus.init("my-app", instrument=[])   # detect nothing
build().instrument(tracer_provider=session.provider)
```

Its spans now flow to the same exporters as everything else. Argus doesn't own
this instrumentor, so `argus.reset()` won't uninstrument it — call
`uninstrument()` yourself if you need it undone.
