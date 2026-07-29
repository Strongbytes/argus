# Design notes

Why Argus works the way it does. This is the home for rationale: the trade-offs
behind each decision, the failure modes they avoid, and the alternatives that
were rejected.

The other two places prose lives have narrower jobs, so each fact has exactly
one home:

- The [README](../README.md) is the user-facing guide — what the API is and how
  to use it.
- Docstrings state the contract — what a function does, its arguments, what it
  returns and raises — and link here for the why.

## Contents

- [Zero-ceremony capture](#zero-ceremony-capture)
- [One session per process](#one-session-per-process)
- [The session reports, it does not rewire](#the-session-reports-it-does-not-rewire)
- [A flush is not terminal](#a-flush-is-not-terminal)
- [Buffer now, emit once](#buffer-now-emit-once)
- [Repeat emits: rewrite or clear](#repeat-emits-rewrite-or-clear)
- [Exporters Argus does not own](#exporters-argus-does-not-own)
- [Extension points are protocols](#extension-points-are-protocols)
- [Opting out of the provider's atexit shutdown](#opting-out-of-the-providers-atexit-shutdown)
- [Two files per trace](#two-files-per-trace)
- [Trace filenames](#trace-filenames)
- [Hex ids in OTLP/JSON](#hex-ids-in-otlpjson)
- [Generation order](#generation-order)
- [The raised span-attribute ceiling](#the-raised-span-attribute-ceiling)
- [Remote export is one argument](#remote-export-is-one-argument)
- [Names that don't shadow OpenTelemetry's](#names-that-dont-shadow-opentelemetrys)
- [No default OTLP endpoint](#no-default-otlp-endpoint)
- [Credentials resolved at construction](#credentials-resolved-at-construction)
- [Delivery failures warn, never raise](#delivery-failures-warn-never-raise)
- [Delivery failures name their cause](#delivery-failures-name-their-cause)
- [Permanent rejections are not retried](#permanent-rejections-are-not-retried)
- [Swallowed errors are still audible](#swallowed-errors-are-still-audible)
- [Loading `.env` unconditionally](#loading-env-unconditionally)
- [Suppression at the source](#suppression-at-the-source)
- [Why the decorator refuses generators](#why-the-decorator-refuses-generators)
- [Curated detection over entry points](#curated-detection-over-entry-points)

## Zero-ceremony capture

The central goal: adding a single `argus.init(...)` line should produce a
complete, correctly-labelled trace on disk even if the user calls nothing else
and even if the script crashes. Two module-level mechanisms in
`argus.session` buy that:

- An `atexit` hook (`_flush_on_exit`) flushes the active session on process
  exit, so the common case needs no context manager and no explicit flush.
- An `excepthook` wrapper (`_install_excepthook`) records whether the run died
  from an unhandled exception. That flag is what lets the on-exit flush tag a
  crashed run as failed without the user opting in.

`Session` also doubles as a context manager for callers who want deterministic,
scoped flushing. Using both together is harmless because a flush with nothing
new to emit is a no-op.

## One session per process

Instrumentors are global singletons, so a second `TracerProvider` could never
reliably receive spans for an already-instrumented framework. `init` therefore
enforces a single session per process: a second call warns and returns the
first session unchanged.

That is emitted as a `RuntimeWarning` rather than raised, which is the same
forgiving stance OpenTelemetry's own `set_tracer_provider` takes — a stray
second `init` should not crash the host program. Callers who want fail-fast
behavior can promote it with `python -W error`.

The right default for a script is the wrong one for a notebook, where
re-running the `init` cell is the ordinary way to change a setting. `reset()`
is the escape hatch: it uninstruments the live instrumentors (so the next
`init` can re-wire them to its own provider), drops the session, and clears the
failure flag. It deliberately does *not* flush — retiring a session and
emitting its traces are separate decisions — and because the `atexit` hook only
ever flushes the *active* session, whatever the dropped one had buffered is
gone unless the caller flushed first.

## The session reports, it does not rewire

What a caller may do with the `Session` `init` returns had one answer nobody
could see: a sentence in the class docstring calling mutation of its five public
attributes unsupported. Nothing enforced that, the README never mentioned it,
and the attributes were plain lists — so `session.exporters.append(my_sink)` was
the obvious thing to reach for.

It is also the worst thing to reach for. `init` attaches one span processor per
sink to the provider, so a sink appended afterwards receives no spans at all,
while `flush` and the exit shutdown still drive it: it gets `emit` or
`force_flush` and then `shutdown`, and writes an empty trace file. Clearing the
list instead disables flushing. Neither raises, and neither is discoverable.

The surface is therefore decided rather than inherited. `provider`, `project`,
`instruments` and `exporters` are read-only properties, and the live
instrumentors are private — `instruments` already answers the only question a
caller had for them. `exporters` returns a tuple rather than the list Argus
drives, and `instruments` is derived on access rather than stored at
construction, which also retires a copy that could disagree with the
instrumentors `reset` tears down. `provider` stays as useful as it ever was:
emitting your own spans through it and pointing an unknown instrumentor at it
are both documented, and read-only stops it only from being *replaced*, which
would leave the session flushing sinks that no longer see the spans.

The constructor is internal for a related reason. It takes the `_SpanCounter`
`init` registered on the provider, so a session built by hand cannot be wired
correctly anyway; it used to default that argument, which implied otherwise. The
class stays exported, because a caller still needs the name to annotate what
`init` returned.

None of this is a cage — `session._exporters` is one underscore away, and a
tuple protects the container rather than the exporters inside it. The point is
that the supported path is now visible in the README and pinned by a test, so it
can be relied on, while everything behind it can still change before the 1.0
that `major_version_zero` is holding open.

## A flush is not terminal

`Session.flush` must be safe to call repeatedly, since the context manager and
the `atexit` hook routinely both fire. But "already flushed once" must not mean
"finished forever", or spans produced after an explicit flush would be buffered
and never emitted — silently buffering into the void being the worst of the
available behaviors.

A `_SpanCounter` span processor tallies ended spans, and comparing that tally
against the one recorded at the last flush distinguishes the two cases: an
unchanged count means there is genuinely nothing new to emit, while a higher
one means more spans arrived and deserve a second emit. The counter mirrors
`SimpleSpanProcessor`'s sampling check, since counting spans that no exporter
received would signal work that does not exist.

## Buffer now, emit once

OpenTelemetry hands spans to an exporter's `export` as they end —
incrementally, and out of order — but at that moment the run's outcome is not
yet known. Both of Argus's sinks therefore buffer spans in memory and defer the
real work (writing a file, POSTing a batch) to an `emit(failed=...)` hook that
Argus drives on exit, when the outcome is known.

For the file sink, knowing the outcome up front is what lets the *filename*
carry it (see [Trace filenames](#trace-filenames)). The remote sink mirrors the
same model rather than adopting OpenTelemetry's usual streaming
`BatchSpanProcessor`, which buys:

- **Symmetry.** Both sinks are plain buffered exporters driven by the same
  `emit` hook and the same `SimpleSpanProcessor`, so there is no second
  delivery model to special-case in `init`.
- **One request per run.** The backend is hit once, at the end, instead of
  absorbing a stream of mid-run batches — fewer connections and less
  per-request overhead, at the cost of a single larger payload.
- **Outcome at emit time.** The send is driven by the known final outcome
  rather than issued blind mid-run.

The trade-off both sinks accept: a hard kill (`SIGKILL`, power loss) before
`emit` loses the trace, since nothing was written or sent yet. An ordinary
exception is fine — the `atexit`/excepthook path still flushes and tags the run.

`SimpleSpanProcessor` is the right processor for this because it is
synchronous: it has no background queue that could drop spans under load, and
the actual write or send is deferred to `emit` anyway, so there is nothing to
gain from batching in front of it.

## Repeat emits: rewrite or clear

Because [a flush is not terminal](#a-flush-is-not-terminal), `emit` can be
called more than once, and each sink has to stay correct when it is. They
answer differently, and the destination is why:

- The **file sink keeps its buffer** and rewrites each trace's files from
  scratch, so what lands on disk is always the complete trace rather than the
  fragment that arrived since the last write. The base name is allocated once
  per trace, so the rewrite lands on the same files instead of scattering
  partial ones through the directory.
- The **remote sink clears its buffer** once the backend confirms the batch, so
  a later emit sends only what has not been ingested, and nothing at all when
  everything already has.

The asymmetry is not an oversight: re-POSTing spans duplicates them at the
backend, whereas rewriting a file simply supersedes it. Since the buffer is
cleared only on a *confirmed* success, a failed remote attempt leaves the spans
intact for the next emit to retry.

`_DeferredExporter` encodes this as the one decision a subclass makes — its
`_deliver` returns a `Delivery` outcome — so neither sink can get the buffer
bookkeeping wrong, and a third-party sink cannot get the polarity backwards at
the return statement. The decision is really binary (keep the spans or let them
go), but there are two reasons to let them go: `CONSUMED`, delivered and
accepted, and `DISCARDED`, [undeliverable and not worth
retrying](#permanent-rejections-are-not-retried). `RETAINED` is the only outcome
that keeps them, so `emit` clears the buffer on anything else.

## Exporters Argus does not own

`exporters=` accepts any OpenTelemetry `SpanExporter`, not just Argus's own, so
`Session.flush` has to drive two kinds of sink:

- One that implements `BufferedSpanExporter` (i.e. defines `emit(failed=...)`)
  has opted into the deferred lifecycle above and is handed the run's outcome.
- A stock exporter has no such hook; it received its spans synchronously as
  they ended. Because Argus [opts out of the provider's own `atexit`
  shutdown](#opting-out-of-the-providers-atexit-shutdown), nothing else in the
  pipeline would ever drain it, so an exporter that batches internally would
  silently lose the run. Argus therefore calls `force_flush()` on each flush
  and `shutdown()` once at exit.

`emit` takes precedence for an exporter offering both, because only `emit`
carries the run's outcome.

Failures in either path are swallowed per exporter: a sink that cannot flush or
close must not take the host program down with it, nor prevent the other sinks
from being driven. Swallowed is not silent, though — each failure is
[reported once](#swallowed-errors-are-still-audible).

## Extension points are protocols

Argus has two seams a caller can substitute their own object into: the sinks
(`exporters=`) and the instrumentors (`instrument=`, or an instance Argus resolves
for a framework it knows). Both are `typing.Protocol`s — `BufferedSpanExporter`
and `Instrumentor` — rather than base classes to inherit or bare `object`
annotations.

Structural typing is the only option that fits: an OpenInference instrumentor
already derives from OpenTelemetry's `BaseInstrumentor`, and a third-party
exporter from `SpanExporter`, so neither can be asked to inherit from Argus as
well. A protocol describes the two or three methods Argus actually calls and
leaves the class hierarchy alone — including for the test suite's stand-ins,
which need nothing but the right methods.

The annotations they replace (`Sequence[object]` for the instrumentors,
`resolve_instrumentors(...) -> list`) described the *most* extensible part of the
system the *least*: to learn what an instrumentor had to provide you had to find
the `instrumentor.instrument(tracer_provider=...)` call in `init` and the
`uninstrument()` in `reset`. The protocol says it in one place, and a type checker
can now hold a substitute to it.

Both are `runtime_checkable`, but for different reasons. `Session.flush` genuinely
branches on `isinstance(exporter, BufferedSpanExporter)` to decide whether to
drive a sink through `emit` or `force_flush`. Nothing branches on `Instrumentor` —
Argus just calls the methods — so there it only lets a test assert conformance
instead of discovering a mismatch when a call fails.

Two smaller pieces of the same effort: `__exit__` is annotated `Literal[False]`
rather than `bool`, which tells a checker that neither `Session` nor `blindspot`
ever swallows an exception (a `bool` return leaves that ambiguous, and code after
a `with` block is analyzed differently depending on the answer); and the package
ships a PEP 561 `py.typed` marker, without which none of these annotations are
visible to anyone who installs Argus rather than reading it.

## Opting out of the provider's atexit shutdown

`TracerProvider(shutdown_on_exit=False)` is load-bearing, not a tidiness
choice. A provider otherwise registers its own `atexit` handler in its
constructor, and `atexit` runs handlers LIFO. Argus's `_flush_on_exit` is
registered at *import* time — before any provider exists — so the provider's
shutdown, registered later at `init` time, would run *first* on exit. That
shutdown cascades into each exporter's `shutdown()`, tearing down the OTLP
transport's HTTP session, so Argus's later flush would call `emit()` on an
already-dead transport: OpenTelemetry logs "Exporter already shutdown, ignoring
batch" and returns `FAILURE`, and the backend is never even contacted.

Argus drives the whole buffer-now/emit-once lifecycle itself, so it opts out of
the competing handler and closes the exporters itself — as the last step of
`_flush_on_exit`, after everything has been emitted. Shutdown lives there and
never in `Session.flush`, because a flush from a context manager mid-program
must leave every exporter usable for the spans still to come.

## Two files per trace

Every trace is written twice, to two files that share a base name and differ
only by a format marker:

- `.otlp.json` is canonical OTLP/JSON — a single `ExportTraceServiceRequest`,
  the same protobuf message the remote sink POSTs, rendered to JSON via
  `google.protobuf.json_format.MessageToDict`. It can be replayed to any OTLP
  backend. Argus reuses OpenTelemetry's own `encode_spans` rather than
  hand-rolling the schema, so the file sink and the remote sink cannot drift
  apart. That is why `opentelemetry-exporter-otlp-proto-common` is a core
  dependency rather than part of the `otlp` extra: it is the encoder only, not
  a network transport, and the default sink needs it.
- `.readable.json` trades wire fidelity for legibility: a plain JSON array of
  spans rendered with OpenTelemetry's own `ReadableSpan.to_json` (snake_case
  fields, hex ids, ISO timestamps), then passed through `expand_embedded_json`
  so an attribute value that is itself a JSON string — a model's `output.value`,
  a tool call's arguments — reads as real structure instead of an escaped blob.

## Trace filenames

A trace's base name is `<timestamp>_<script>`, where the timestamp is a UTC
`YYYY-MM-DD_HH-MM-SS` stamp. Date-first means a directory listing sorts
chronologically; UTC rather than local time keeps that ordering stable across
machines and DST.

A run that died on an unhandled exception is tagged with an `.error` marker
before the format suffix, so failed runs are obvious at a glance in a listing
and never silently discarded. This is what [buffering until
`emit`](#buffer-now-emit-once) buys: the outcome is known before the name is
chosen.

The format marker is a suffix rather than a prefix so the date-first sort
survives it. A numeric suffix is appended if a sibling trace from the same
run and second already claimed the name, so concurrent traces never overwrite
one another.

All five of those parts are assembled in one function, `trace_filename`, which
takes them as arguments and formats nothing else. It was previously spread over
two module constants and a method that also allocated names and memoized them,
which meant the scheme the README documents in detail had no single place to read
or test: a change to the timestamp format touched one line, a change to the format
marker another, and neither could be exercised without an exporter and a temporary
directory. The exporter now decides *which* name a trace gets (`_TraceNaming`,
allocated once per trace and kept, so both of a trace's files — and any rewrite
from a [repeat emit](#repeat-emits-rewrite-or-clear) — agree) while the function
decides what a name *looks like*.

The outcome is captured when a trace is first written rather than at every write.
A run that flushes mid-way and then crashes therefore keeps the name that first
flush chose: the rewrite supersedes the same pair of files, instead of leaving a
stale success-named pair beside a new `.error` one.

## Hex ids in OTLP/JSON

OTLP/JSON has a single documented departure from the proto3 JSON mapping:
`traceId` and `spanId` (and `parentSpanId`, including the ids nested under span
links) are **hex** strings, not the base64 that `MessageToDict` emits for
`bytes` fields by default. Argus re-encodes exactly those fields, which is what
makes the file drop-in valid for POSTing straight back as OTLP/JSON — the
common replay path.

The rarer path — rebuilding the protobuf message from the file to send as
OTLP/protobuf — must therefore hex-decode those id fields rather than lean on
stock proto3 JSON parsing, which would base64-decode them. Every other field
follows the standard camelCase OTLP layout.

Attribute payloads are safe from accidental rewriting: their values live under
`key`/`value` entries, never under these reserved keys.

## Generation order

Spans arrive from `export` in *end-time* order — a leaf finishes before the
parent that wraps it — so the buffer reads roughly backwards, with the
earliest-started root span landing last. Both files restore the run's
chronology by sorting on `start_time`, the epoch-nanosecond stamp
OpenTelemetry records, so they read top-to-bottom as the run unfolded.

Sorting on the real timestamp rather than blindly reversing keeps siblings and
concurrent work correctly ordered. The sort is stable and tolerates spans
without a `start_time`, which keep their relative arrival order.

## The raised span-attribute ceiling

OpenTelemetry caps span attributes at 128 by default. OpenInference flattens
every chat message into several attributes (role, content, each tool call's
id/name/arguments, ...), so a long agent conversation blows past 128 and the
SDK silently evicts the oldest attributes — which, given the order OpenInference
writes them, includes the model's final output message.

Argus raises the ceiling to 50,000: far past any realistic run, while keeping a
rail against a pathological one. At roughly three attributes per chat message
that holds on the order of ten thousand messages in a single span.

The ceiling is fixed, and deliberately not configurable — neither an `init`
argument nor read from any environment variable. Three things make an adjustable
ceiling all cost and no benefit here:

- **A too-low value fails silently.** Recording less than a run produced is the
  exact loss the raised ceiling exists to prevent, and it surfaces as a missing
  final message rather than an error — so a knob whose main effect is "quietly
  capture less" undercuts the whole point of the tool.
- **Raising it higher rescues no real run.** The limit is a `maxlen` on the
  span's attribute container, not a preallocation, so it costs no memory until
  that many attributes actually exist. 50,000 already sits far past any realistic
  run while still bounding a pathological one, so no larger value buys anything.
- **Choosing a value well needs internals.** It requires knowing OpenInference's
  per-message flattening — Argus's job to settle once, not the caller's to
  rediscover, and a too-low guess fails silently as above.

This is the one place Argus overrides a standard OpenTelemetry setting rather
than deferring to it: it does not honor OpenTelemetry's own attribute-count
limits, because an externally lowered ceiling would reintroduce exactly the
silent truncation the fixed value removes. The endpoint and headers variables,
which carry deployment wiring rather than a capture policy, still apply to remote
export. Only the attribute *count* is fixed; events, links, and attribute value
lengths keep whatever OpenTelemetry resolves for them.

## Remote export is one argument

Everything about remote export is reached through a single `otlp` argument to
`init`, which takes `True`, an `OtlpConfig`, or `None`. The alternative — an
`otlp` switch beside a separate `api_key`, which is what Argus had — lets the
signature spell combinations that cannot mean anything:

- A key with no endpoint to send it to. That needed a runtime warning to explain
  itself, which is a signature apologizing for its own shape.
- An `otlp=""` that was falsy, so it silently turned export *off* — while the
  caller believed they had supplied an endpoint. Blank strings are exactly what
  `os.getenv("MY_ENDPOINT", "")` yields, so this was reachable by accident.

Folding the endpoint, key, headers and timeout into one frozen `OtlpConfig`
makes both unrepresentable: there is nowhere to put a credential except beside
the endpoint it authenticates. `otlp=True` survives as shorthand for
`OtlpConfig()` — "on, everything from the environment" — because that is the
documented one-liner and it cannot be ambiguous.

The argument is normalized before `init` creates anything, so a wrong type costs
no side effects: no provider, no exporters, no trace directory, and the session
singleton is left unclaimed for a corrected call. A string endpoint (the old
spelling) is rejected with a message naming its replacement rather than being
read as a truthy "on", which would have ignored the endpoint given.

Carrying headers and timeout on the config also removes a detour. Customizing
those used to mean building the exporter by hand and passing it through
`exporters=`, which replaces the default file sink — so "add a timeout" quietly
turned into "also remember to re-add the file exporter".

## Names that don't shadow OpenTelemetry's

OpenTelemetry ships an `OTLPSpanExporter`, and Argus's remote sink used to have
the same name. The collision was visible in Argus's own source, which had to
alias the real one on import to talk about both in one module.

`BufferedOTLPExporter` names what actually distinguishes it: OpenTelemetry's
streams spans out mid-run, Argus's buffers them and POSTs once (see [Buffer now,
emit once](#buffer-now-emit-once)). It also matches the vocabulary of
`BufferedSpanExporter`, the protocol it satisfies. One importable name should
mean one thing, so an import line can be read without checking which package it
came from.

For the same reason there is one way to construct it. A `make_otlp_exporter`
factory alongside the constructor gave callers a coin flip between two entry
points to the same object, and it did nothing the constructor did not.

## No default OTLP endpoint

Where OpenTelemetry falls back to `http://localhost:4318`, Argus raises.
It is a library anyone may install, so a hardcoded endpoint would either
silently ship a stranger's traces to someone else's backend or point at a
machine-local address meaningless to the caller.

Resolution otherwise follows OpenTelemetry's own precedence: an explicit
endpoint argument, then `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, then the generic
`OTEL_EXPORTER_OTLP_ENDPOINT`. The first two are complete POST URLs used
verbatim; the generic one is a base shared by every signal — what a collector
deployment usually sets — so `v1/traces` is appended to it, which is what the
standard says and what OpenTelemetry's own exporter does. Honoring it means a
collector configured the ordinary way just works, instead of Argus refusing
over a variable it could plainly have read.

Endpoint resolution is kept separate from the exporter class so the rules can
be exercised without importing the optional OTLP dependency.

An explicitly configured endpoint is stripped of surrounding whitespace, since
one read out of a file or a shell often arrives with a trailing newline that
would make every POST fail. If stripping leaves it empty, that raises rather than
falling through to the environment: a blank endpoint reads as one that failed to
resolve, and quietly substituting a variable the caller never mentioned would
send traces somewhere they never chose.

Argus does not reimplement the wire protocol. OpenTelemetry's own OTLP/HTTP
exporter — the "transport" — already handles protobuf encoding, gzip, retries,
and the standard `OTEL_EXPORTER_OTLP_*` variables, and it lives in the optional
`argus-trace[otlp]` extra. It is imported when an exporter is constructed, so a
missing extra fails loudly at `init` time rather than silently at exit. Two
settings Argus does not leave to it: the endpoint pair above, and the headers
pair — `OTEL_EXPORTER_OTLP_TRACES_HEADERS` and `OTEL_EXPORTER_OTLP_HEADERS`
never take effect, because the transport reads them only when no `headers`
argument was passed and Argus always passes one to carry the credential.
Everything else the transport reads (timeout, compression, TLS material)
reaches it untouched.

## Credentials resolved at construction

Aegis ingest is authenticated, so an API key is part of the remote sink's
configuration alongside the endpoint. It is resolved and validated when the
exporter is *constructed*, not when spans are sent: with an emit-once-on-exit
model, a credential problem discovered at send time would surface as a rejected
batch during interpreter shutdown, after the run's whole trace had been
buffered and with nothing left to retry.

Precedence mirrors the endpoint's and the major LLM SDKs': an explicit
`OtlpConfig(api_key=...)`, then the `AEGIS_API_KEY` environment variable. The env
fallback is what keeps single-line initialization possible, and keeps callers
from pasting a live secret into committed source just to satisfy a required
argument. The variable is named for Aegis rather than Argus because the
credential is issued and validated by the Aegis backend that receives the
traces; Argus is only the client presenting it.

A key is mandatory — there is no unauthenticated configuration worth
supporting — and any caller-supplied headers are merged *around* the resolved
`Authorization: Bearer <key>`, never in place of it. Two values are rejected
outright rather than sent: a key containing whitespace (Aegis splits the header
on whitespace, so it could only ever come back as an opaque 401) and a
caller-supplied `Authorization` header (the key would silently overwrite it).

The resolved key is held only inside the transport's headers, never on the
exporter, so it cannot leak through a warning or a repr.

## Delivery failures warn, never raise

Argus is a side-channel: a backend that is down, slow, or rejecting batches
must not crash the run, nor — via the context-manager path — mask the user's own
exception. But a silent drop is just as bad, because with an emit-once model
there is no later batch to reveal the gap.

So a failed delivery surfaces as a `RuntimeWarning`, which `python -W error`
can promote to an exception for callers who want strictness. Both failure
shapes are handled: the transport reports a rejected batch by *return value*
(it retries retryable statuses, then gives up) rather than by raising, and a
connection-level error can still escape as an exception.

## Delivery failures name their cause

A warning that says only "the batch was not delivered" leaves the caller to
guess between the handful of things that actually go wrong — a wrong API key, a
key without access, a mistyped endpoint, an unreachable host. The distinctions
are exactly the ones a person needs to act, so the warning names the cause: a
`401` reads as a rejected key (and points at `AEGIS_API_KEY` / `api_key`), a
`403` as a key that lacks access, a `404` as a likely-wrong endpoint, a `4xx`
body error as a malformed batch, a `5xx` as a transient backend problem, and a
connection-level failure as the exception it raised.

The obstacle is that OpenTelemetry's exporter throws that information away.
`export()` returns only success or failure; the HTTP status and any connection
error are logged and then dropped, and the retry bookkeeping lives on an
internal object Argus cannot reach. Rather than reimplement the wire protocol
(see [above](#no-default-otlp-endpoint)) or scrape the transport's log lines —
whose text is not an API — Argus subclasses the transport and overrides the
one private seam that still has the answer: the per-attempt `_export`, which
returns the raw HTTP response or raises the connection error. The override
records the last attempt's status code (or exception) and delegates everything
else untouched, so all of OpenTelemetry's retry, backoff, and encoding still
run. `_deliver` reads what was recorded and hands it to `_describe_failure`,
which is a module function — like the endpoint and header rules — so the whole
status-to-sentence mapping is testable without the network or the `otlp` extra.

The coupling to a private method is deliberately made cheap to lose. `_export`
is delegated to through `*args`, so a change to its signature passes straight
through; and if a release renames or drops it, the override simply never runs,
the recorded status stays absent, and the warning falls back to the same
generic "rejected the batch" line it used before — degraded, never broken. The
status buckets are the same principle: an unrecognised code still reports its
number, so a backend Argus has no dedicated wording for is never *less* legible
than it was. Throughout, the credential is never part of the message — the
mapping only ever echoes a status code or an exception type, neither of which
can carry the key.

## Permanent rejections are not retried

A failed delivery [keeps its spans buffered](#repeat-emits-rewrite-or-clear) so
a later emit can retry — the right move when the cause is transient, since a
[flush is not terminal](#a-flush-is-not-terminal) and a backend that was down or
throttling may have recovered by the next one. But not every failure is
transient. A `401`, a `403`, a `404`, a malformed-batch `400`/`422` — these are
verdicts on the request itself, so re-POSTing the same spans to the same
endpoint with the same key can only earn the same rejection. Retaining them
would buy nothing and cost twice: a run that flushes repeatedly (a notebook, a
long service) would re-send the doomed batch and re-warn on *every* flush, and
the buffer would grow without bound holding spans that can never leave it.

So `_deliver` splits the failure return. Once the [status code is
known](#delivery-failures-name-their-cause), a client error (`4xx`) that a retry
cannot fix returns `DISCARDED` — the spans are dropped, the single warning
already stands, and later flushes are silent — while everything transient
(`5xx`, a connection error, a failure with no status to judge) returns
`RETAINED` and is kept for another attempt. The split is conservative in the
uncertain direction: an unattributed failure is retried, not discarded, so a
gap only ever comes from a status Argus is confident is permanent.

Two `4xx` codes are deliberately treated as transient: `408 Request Timeout` and
`429 Too Many Requests`, both of which can clear between flushes on their own.
The rule is one module function (`_is_permanent_status`) beside the message
mapping, testable without the network, and it never drops silently — the run
still has its local trace files, and the warning that preceded the drop named
exactly why the backend refused the spans.

## Swallowed errors are still audible

Four places catch `Exception` and carry on. Three of them warn once they have:
draining a stock exporter with `force_flush`, closing the exporters at exit, and
uninstrumenting on `reset`. The reason is the same each time, and it is the right
one — an exporter or an instrumentor Argus does not own must not take the host
program down, nor stop the ones after it from being driven.

Saying nothing about it is not. A bare `pass` means a custom exporter that
raises on every call produces no trace, no error and no clue, which leaves
`exporters=` the least debuggable part of the API at exactly the moment it is
broken. Each of those three therefore warns, the same way a [failed remote
delivery](#delivery-failures-warn-never-raise) does: a `RuntimeWarning` naming
the object, the call, and what it raised.

The fourth is a deliberate exception. The on-exit flush itself is wrapped in a
bare `pass`, because it runs from the `atexit` hook while the interpreter is
already tearing down — a warning there is not something a caller can count on
seeing, and letting the exception escape would crash shutdown. The exporters are
still closed afterwards regardless.

Two details keep that from becoming noise of its own. The report is deduped per
object and call, because `flush` may run many times over a session and a sink
that fails once usually fails every time — the first report is the useful one.
And it is attributed to the caller's own `flush()` or `reset()` line, so `python
-W error`, which promotes these as it does every other Argus warning, points at
code the caller can act on. On the exit path no such frame exists and the
`atexit` hook is the best available answer.

## Loading `.env` unconditionally

`init` loads the nearest `.env` at or above the working directory before
resolving anything, which is what lets a key kept in `.env` satisfy
`AEGIS_API_KEY` with no argument passed.

There is no opt-out because there is nothing to opt out of: python-dotenv only
ever *fills in* variables, leaving anything already in the environment
untouched, so a deployment's real configuration always wins — and a missing file
is a no-op. The search walks up from the working directory (where the script
runs) rather than from inside the installed package, which does mean a script
in a monorepo can reach a `.env` in a shared parent. That is harmless given the
no-override rule, but it is worth knowing about.

python-dotenv is a *core* dependency, not an extra: `.env` loading is part of how
the documented one-line initialization gets its API key, so an installation where
it is absent is one where that promise quietly stops holding. The import is
guarded all the same, because an environment can end up without it despite the
metadata — a vendored or trimmed deployment, a partially-resolved image — and the
right behavior there is "no `.env` was loaded", not an `init` that raises over a
file the caller may not even have. So the guard is a fallback for those installs,
not an invitation to drop the dependency.

## Suppression at the source

`blindspot` attaches OpenTelemetry's own `_SUPPRESS_INSTRUMENTATION_KEY` to the
active context. OpenInference's instrumentors — and OTel-aware libraries in
general — check that flag and skip span creation entirely while it is set, so
an instrumentor span is never created, buffered, or written. Suppressing at the
source rather than dropping spans after the fact means sensitive payloads never
enter the pipeline at all.

The flag reaches only code that consults it. The SDK's own `Tracer.start_span`
does not, so a span a caller starts by hand through `provider.get_tracer(...)`
(a [documented path](../README.md#the-session-object)) is recorded even inside a
blindspot — the scope covers the instrumentors, not manual spans. Keep manual
spans out of a sensitive region yourself.

Because the flag rides on a `contextvars`-backed context, the suppression
follows `await` points and copies into tasks spawned inside the block. It does
*not* reach threads started by the caller (a raw `threading.Thread`, a
`ThreadPoolExecutor`), since those begin from a fresh context unless the
context is explicitly copied.

Suppression is keyed to the context active when the scope is entered, and each
entry stacks its own token that exit pops last-in/first-out, so one instance can
be entered repeatedly and nested safely. That stack is itself a `ContextVar`
(`_open_tokens`, shared by every instance) rather than an attribute of the
instance, so it is per thread and per task just like the suppression it undoes.
Instance state would have made a shared `blindspot()` unsafe to enter from two
threads at once: each exit pops whichever token is on top, and `detach` on a
token attached in another context fails — which would leave suppression stuck on
in the thread whose token was taken. The decorator establishes suppression per
call, so recursive and concurrent invocations never interfere.

## Why the decorator refuses generators

A generator does not get a context of its own the way a coroutine does: it runs
in whatever context its consumer is in. So a wrapper holding the suppression
across a `yield` cannot confine it to the generator's body — the flag stays
attached while the consumer runs too, quietly suppressing code the caller never
meant to hide. Nor can the wrapper simply not hold it, because the body would
then run unsuppressed, which is worse: the decorator would look like it was
protecting a sensitive stream while doing nothing.

Both failure modes are silent, and for a feature whose whole job is keeping
payloads off the record, silence is the one thing Argus cannot ship. So the
decorator refuses at decoration time, when the mistake is cheap to fix, and
names the pattern that does work: wrap the *consumption* of the generator,
which covers the body and the consumer alike, deliberately.

## Curated detection over entry points

The default is a curated registry mapping each agent framework to the
instrumentor(s) it needs, detected by whether the framework is actually in use.
It is predictable and avoids double-instrumenting — it won't add the standalone
OpenAI instrumentor on top of the OpenAI Agents one.

That rule lives on the registry entry itself, as a `supersedes` tuple naming the
keys detection drops when this framework is present, so adding a framework is
one self-contained entry rather than an entry plus an edit to a table elsewhere
in the module. It also generalizes past the one case that motivated it: any
framework can declare the narrower keys its own coverage makes redundant.

Detection prefers `sys.modules` over importability, so in a shared environment
with several SDKs installed Argus instruments only the framework the current
script actually imported, rather than everything present. Instrumentor classes
are imported lazily, once a framework is selected, so an unused optional
dependency never has to be importable.

`instrument="all"` opts into entry-point discovery instead: every instrumentor
registered under the `openinference_instrumentor` group lights up with no code
change here, at the cost of possibly instrumenting more than intended in a
multi-framework environment. A broken or incompatible instrumentor is skipped
rather than allowed to abort the run.

The keys are a type, not just documentation. `instrument=` is annotated with
literals — `InstrumentKey` for the four framework keys, `InstrumentStrategy` for
`curated` and `all` — so an editor completes them and a type checker rejects
`instrument="openai_agent"` at the call site instead of leaving it to the
`ValueError` at run time. The registry is checked against that type rather than
the other way round: `_Framework.key` is annotated with `InstrumentKey`, so an
entry naming something the type does not offer fails to typecheck, and a test
pins the reverse direction.

The runtime check stays regardless, because a key can arrive from a config file
or a command line, where no checker ever saw it. That is also the Literal's
cost: a plain `list[str]` is now a type error even when every string in it is
valid, so code assembling a selection dynamically annotates it as
`list[InstrumentKey]` (or casts).

When curated auto-detection (or `"all"`) resolves zero instrumentors, `init`
emits a `RuntimeWarning` naming the known keys and the `instrument=` argument.
A bare `argus.init(project)` with no recognized framework otherwise installs
nothing, produces no spans, and writes no files — silence of the kind the rest
of `init` refuses. Pass `instrument=[]` to opt out of instrumentation
deliberately without the warning.
