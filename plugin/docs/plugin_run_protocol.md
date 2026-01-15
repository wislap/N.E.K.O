# Plugin Run Protocol: Run/State Machine + runs/export Channels

## 1. Scope

This document specifies a new plugin invocation architecture based on **Run** (execution instance) and a **server-owned state machine**, with two observable channels:

- `runs` channel: lightweight state/progress notifications.
- `export` channel: user-facing outputs (text/url/binary_url) associated with a run.

This design is intended to **replace** the current “server receives a trigger and directly executes entry” path for external callers, improving:

- Traceability (end-to-end correlation)
- Stability under slow tasks / retries
- Progress reporting
- Cancellation
- Clear atomicity semantics
- Plugin developer experience

Non-goals:

- Distributed transactions across external side effects.
- Guaranteeing rollback of arbitrary plugin side effects.


## 2. Terminology

- **Entry**: a plugin entry point identified by `entry_id`.
- **Run**: a single execution attempt of an entry. Identified by `run_id`.
- **RunStore**: authoritative server-side storage of Run state.
- **runs channel**: a notification stream for Run state changes.
- **export item**: a user-facing output payload associated with a run.
- **Task**: an upstream orchestration unit (e.g., agent task). Identified by `task_id`.
- **Correlation**:
  - `run_id`: unique invocation identifier (mandatory).
  - `trace_id`: optional cross-service trace identifier.
  - `task_id`: optional business identifier.

Additional identifiers (recommended):

- `idempotency_key`: caller-provided key to deduplicate network retries / duplicate triggers.
- `root_run_id`: stable identifier for a logical invocation across retries (attempts).
- `parent_run_id`: the previous attempt run that the new run retries from (if any).
- `attempt`: integer attempt index within a `root_run_id` sequence (starting at 1).
- `export_item_id`: unique identifier for a single exported output item.


## 3. Design Summary

### 3.1 Layering

This architecture separates responsibilities:

- **Authoritative state**: stored in RunStore (keyed by `run_id`).
- **Notifications**: delivered via `runs` channel (bus-style delta updates).
- **Outputs**: delivered via `export` channel as append-only items; the Run record references final output items by ID.

### 3.2 Why this replaces the current trigger flow

The existing synchronous trigger style:

- Couples “trigger” and “result retrieval”.
- Makes retries ambiguous (duplicate execution risk).
- Makes long-running tasks unstable (timeouts / blocked workers).
- Makes tracing difficult (no stable run identity).

The Run protocol decouples trigger and observation while keeping a clear, queryable source of truth.


## 4. Run State Machine

### 4.1 RunStatus

A Run MUST have exactly one `status` at any moment.

Allowed statuses:

- `queued`: accepted by server; not yet started.
- `running`: started execution.
- `succeeded`: finished successfully.
- `failed`: finished with an error.
- `canceled`: finished due to cancellation.
- `timeout`: finished due to timeout.

Optional intermediate statuses (implementation-dependent):

- `cancel_requested`: cancellation requested but not yet observed/terminated.

Recommendation:

- Implementations SHOULD support `cancel_requested` to represent two-phase cancellation (§7.3, §8.2).

### 4.2 State transitions

Allowed transitions:

- `queued -> running`
- `queued -> canceled` (if canceled before start)
- `running -> succeeded | failed | canceled | timeout`
- `running -> cancel_requested -> canceled` (if using the optional state)

Terminal states:

- `succeeded`, `failed`, `canceled`, `timeout` are terminal.
- Terminal states MUST be immutable once committed.

Partial results under cancellation:

- A Run that ends in `canceled` MAY still have exported items.
- If partial results are intended to be user-visible and treated as “final”, they MUST be referenced by `result_refs` when committing the terminal state (see §5.2).

### 4.3 Progress semantics

- A Run MAY expose `progress` in `[0.0, 1.0]`.
- Progress updates MUST NOT change the Run’s terminal status.
- Progress updates SHOULD be rate-limited by the server (see §10).


## 5. RunStore (Authoritative Storage)

### 5.1 RunRecord schema (normative)

A RunStore record MUST contain:

- `run_id: string` (uuid)
- `plugin_id: string`
- `entry_id: string`
- `status: RunStatus`
- `created_at: float` (epoch seconds)
- `updated_at: float`

A RunStore record SHOULD contain:

- `task_id: string | null`
- `trace_id: string | null`
- `idempotency_key: string | null`
- `root_run_id: string | null` (defaults to `run_id` if not set)
- `parent_run_id: string | null` (set when created via retry)
- `attempt: int | null` (starting at 1)
- `started_at: float | null`
- `finished_at: float | null`
- `progress: float | null`
- `cancel_requested: bool`
- `cancel_reason: string | null`
- `cancel_requested_at: float | null`
- `error: object | null` (see §9)
- `result_refs: list[ExportRef]` (only final/committed refs)

### 5.2 Result visibility atomicity

- `result_refs` MUST represent the “final result set” for the run.
- `result_refs` MUST only be committed when the Run reaches a terminal state.
- During `running`, intermediate outputs MAY exist in the export channel, but they MUST NOT be treated as the final result set unless referenced by `result_refs`.

Export replay requirement:

- The system SHOULD provide a way to replay/export items for a run by querying storage (see §7.5), because the export channel itself may be lossy or best-effort.


## 6. Channels

### 6.1 runs channel

Purpose:

- Notify observers (UI/agent/external callers) that a Run changed.

Properties:

- Payloads are lightweight and structured.
- The channel is not authoritative; RunStore is.
- Observers MUST be able to recover by calling `GET /runs/{run_id}`.

Filtering requirement:

- Implementations SHOULD support filtered subscription (at least by `run_id` and optionally by `task_id`, `plugin_id`, `status`) to avoid forcing observers to consume all runs.

Recommended delta payload:

```json
{
  "op": "add|change",
  "run_id": "...",
  "status": "running",
  "progress": 0.4,
  "updated_at": 1730000000.0
}
```

### 6.2 export channel

Purpose:

- Deliver user-facing outputs.

Properties:

- Append-only items.
- Each item MUST include `run_id`.
- Large payloads SHOULD be referenced by URL.

Reliability requirement:

- Export items SHOULD be persisted before notification is emitted, so observers can recover by replay (see §7.5).

Recommended ExportItem schema (informative):

```json
{
  "export_item_id": "...",
  "run_id": "...",
  "type": "text|url|binary_url|binary",
  "created_at": 1730000000.0,
  "description": "optional",
  "text": "...",
  "url": "...",
  "binary_url": "...",
  "binary": "base64...",
  "mime": "optional",
  "metadata": {}
}
```

Filtering requirement:

- Implementations SHOULD support filtered subscription at least by `run_id`.

ExportItem types:

- `text`
- `url`
- `binary_url`
- `binary` (allowed only for small payloads; size limit required)

### 6.3 Implementation note: mapping channels onto an existing bus/watch system (informative)

If the system already has a bus-style pub/sub with:

- per-bus subscriptions
- delta delivery (`add|change|del`)
- optional client-side watchers (reload+diff)

then implementations MAY map:

- `runs` channel -> a `runs` bus that emits lightweight delta payloads (see §6.1).
- `export` channel -> an `export` bus that emits append-only notifications (preferably only IDs + metadata; see §6.2).

To reduce fanout and client load, the bus subscription layer SHOULD support filtering at least by `run_id`.
If the bus system supports query plans (e.g., filter/where/limit) or debounce/coalesce, it SHOULD be used to:

- coalesce high-frequency progress updates on `runs`
- avoid dropping `export` result items by relying on replay via storage (§7.5)


## 7. HTTP API (External-facing)

### 7.1 Create a run (Trigger)

`POST /runs`

Request body (minimum):

- `plugin_id`
- `entry_id`
- `args` (object)

Optional:

- `task_id`
- `trace_id`
- `idempotency_key`
- `mode`: `"async" | "sync"` (default: `async`)

Idempotency semantics:

- If `idempotency_key` is provided, the server SHOULD deduplicate requests and return the same `run_id` for repeated submissions with the same key (within an implementation-defined window).
- The server SHOULD validate that the deduplicated run matches `plugin_id`/`entry_id` to avoid accidental key collisions.

Response:

- MUST include `run_id`
- MUST include initial `status` (usually `queued`)

### 7.2 Get run status

`GET /runs/{run_id}`

Response:

- MUST return the authoritative RunRecord.

### 7.3 Cancel run

`POST /runs/{run_id}/cancel`

Behavior:

- Server sets cancellation flag.
- If `queued`, server MAY directly transition to `canceled`.
- If `running`, plugin is expected to cooperate (§8). Server MAY enforce a timeout and mark as `timeout`/`canceled`.

Two-phase cancellation (recommended):

- On cancel request for a `running` run, the server SHOULD transition `running -> cancel_requested` immediately.
- The server SHOULD keep `cancel_requested=true`, set `cancel_requested_at`, and record `cancel_reason` when provided.
- When the plugin acknowledges cancellation (e.g., raises `RunCancelled`) or the host terminates execution, the server commits terminal `canceled`.

### 7.4 List runs (optional)

`GET /runs?task_id=...&plugin_id=...&status=...`

This endpoint is optional and intended for UI/operations.

### 7.5 List/export replay (recommended)

`GET /runs/{run_id}/export`

Purpose:

- Replay export items for reliable clients (UI/agent/external callers).

Query parameters (recommended):

- `after`: opaque cursor or `export_item_id` / monotonic offset (implementation-defined)
- `limit`: max items

Response (recommended):

- `items: list[ExportItem]`
- `next_after: string | null`

### 7.6 Retry a run (recommended)

`POST /runs/{run_id}/retry`

Behavior:

- Creates a new Run attempt with a new `run_id`.
- The new run SHOULD inherit `root_run_id` from the original run (or set it to the original `run_id` if missing).
- The new run MUST set `parent_run_id` to the retried run.
- The new run SHOULD set `attempt = parent.attempt + 1` when available.

Notes:

- This endpoint is for intentional re-execution after a terminal state.
- Network retries should use `idempotency_key` instead of creating new attempts.


## 8. Plugin-side Contract (Developer Experience)

### 8.1 RunContext

During entry execution, the plugin runtime MUST provide a `RunContext` (or equivalent) to the entry handler.

The plugin SDK SHOULD expose:

- `ctx.run.id` (run_id)
- `ctx.run.progress(p: float, message: str | None = None)`
- `ctx.run.export_text(text: str, description: str | None = None)`
- `ctx.run.export_url(url: str, description: str | None = None)`
- `ctx.run.export_binary_url(url: str, description: str | None = None)`
- `ctx.run.check_cancelled()`

Additional recommended APIs:

- `ctx.run.status(status: str, message: str | None = None)` (optional structured stage updates; mapped to `runs` channel)
- `ctx.run.export_id(ref: str)` (optional: let plugins reference existing stored artifacts by ID)

### 8.2 Cooperative cancellation

- The plugin runtime MUST make cancellation status observable to the running entry.
- `check_cancelled()` SHOULD raise a well-known exception (e.g., `RunCancelled`) or return a boolean.
- Long loops SHOULD call `check_cancelled()` periodically.

Cancellation contract:

- If cancellation is requested, the runtime SHOULD make it visible quickly enough that cooperative code can respond within a bounded time.
- When `RunCancelled` is raised/observed, the host SHOULD commit terminal `canceled` (not `failed`).

### 8.3 Default behavior

- If the plugin does not report progress, progress is `null`.
- If the plugin does not export items, `result_refs` may be empty.


## 9. Error Model

### 9.1 Error object

RunStore `error` SHOULD follow a structured model:

- `code: string` (e.g., `TIMEOUT`, `CANCELED`, `PLUGIN_ERROR`, `VALIDATION_ERROR`)
- `message: string`
- `details: object | null`

### 9.2 Mapping rules

- Plugin envelope errors MUST be mapped into RunStore error.
- Transport/IPC errors MUST be mapped and marked retriable when appropriate.


## 10. Performance Requirements

### 10.1 Rate limiting

- Server MUST rate-limit progress updates per `run_id`.
- Recommended defaults:
  - `runs` updates: at most once per 100ms per run.
  - `export` progress items: disabled by default; if enabled, at most once per 500ms per run.

Coalescing recommendation:

- For `runs` updates, coalescing by `run_id` is preferred (keep only the latest state/progress in a short window).

### 10.2 Large payload handling

- `binary` inline export MUST have a strict max size (e.g., 64KB).
- Larger binary outputs MUST use `binary_url`.

### 10.3 Backpressure

If channels are implemented on top of bus queues:

- The system MUST define behavior on overflow (drop, coalesce, or block).
- For `runs` updates, coalescing is preferred.
- For `export` items, dropping should be avoided for `result` items; dropping may be allowed for verbose logs.


## 11. Observability & Correlation

- All logs related to a run SHOULD include `run_id`.
- If present, `trace_id` SHOULD be propagated into plugin logs and exported items.
- UI SHOULD group items by `run_id`.

Attempt correlation:

- UIs/agents SHOULD group attempts by `root_run_id` and show `attempt` to present retry history.


## 12. Compatibility & Migration Plan

### 12.1 Current state

Today’s invocation flow primarily uses a server-side trigger endpoint which directly executes the plugin entry via host IPC.

Pain points:

- Hard to trace (no stable invocation identity)
- Coupled trigger/result
- Poor behavior under slow tasks

### 12.2 Migration principles

- Introduce the Run protocol without breaking existing APIs.
- Gradually switch callers (agent/task executor/UI) to `POST /runs`.
- Keep `/plugin/trigger` as a compatibility wrapper initially.

### 12.3 Phase plan (recommended)

Phase 0: Specification & scaffolding

- Add RunStore data model and minimal endpoints:
  - `POST /runs` (async)
  - `GET /runs/{run_id}`
  - `POST /runs/{run_id}/cancel`

Phase 1: Server-side execution integration

- Internally route `POST /runs` to existing `host.trigger(...)` implementation.
- On execution:
  - Create Run (queued)
  - Transition to running
  - Commit terminal state + error/result_refs

Phase 2: Add runs/export channels

- Implement `runs` notifications and `export` item append.
- Update UI/clients to subscribe or poll.

Phase 3: Plugin SDK ergonomics

- Expose `ctx.run.*` APIs.
- Provide cooperative cancellation helpers.

Phase 4: Deprecate direct trigger usage

- Modify `/plugin/trigger` to become a wrapper:
  - create a run
  - if sync behavior is required, wait for terminal state and return the final envelope

Phase 5: Remove old path (optional)

- After all call sites migrated and validated, remove or restrict old entry trigger endpoints.

### 12.4 Rollback strategy

- Keep old `/plugin/trigger` path functional during migration.
- Feature-flag the new /runs routing for selected callers.

### 12.5 Test plan

- Unit tests:
  - state transitions
  - cancel semantics
  - result_refs atomicity
- Integration tests:
  - long-running plugin with progress
  - cancellation mid-run
  - timeout path
- Load tests:
  - high-QPS short runs
  - mixed workload: runs + export items + message bus
