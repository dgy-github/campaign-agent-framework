# A2A Protocol Layer

`campaign` now routes worker task execution through a small Agent-to-Agent (A2A) protocol layer instead of calling `Agent.handle(task)` directly from the runtime. The default transport is still in-process, so the current demo and tests keep the same behavior while the orchestration code no longer depends on local function calls.

## Mapping

| Campaign model | A2A concept | Notes |
| --- | --- | --- |
| `AgentSpec` | `AgentCard` | Advertises `id`, `role`, `skills`, `model_tier`, optional endpoint, and transport kind. |
| `Task` | Task payload | Serialized into a `Message.parts[0].data` part for request messages. |
| Runtime lifecycle events | Task state | Runtime remains authoritative for `task.assigned`, `task.done`, `task.failed`, and `task.frozen`; `TaskState` names the A2A lifecycle values for remote implementations. |
| `run_id` / `task_id` | Message context | Carried on every request/response envelope for replay and audit. |
| `message_id` / `correlation_id` | Message correlation | Responses copy the request `message_id` into `correlation_id`. |
| `Part` | Message content part | Supports `text` and structured `data` parts. |

## Transports

`InProcessTransport` is implemented and used by `Runtime` when no transport is explicitly configured. It keeps an `agent_id -> Agent` registry, exposes `cards()` for capability discovery, sends request envelopes to `Agent.on_message`, and writes lightweight `a2a.message` audit events when an event log is provided.

`HttpJsonRpcTransport` is intentionally a stub. It defines the same `send(Message) -> Message` interface but raises `NotImplementedError`, leaving HTTP/JSON-RPC wiring for a distributed deployment pass. That future implementation can follow the scaling direction in `docs/SCALING.md` without changing runtime orchestration.

## Governance And Safety

The protocol layer only changes delivery. Runtime still performs agent selection, budget checks, breaker handling, timeout control, review, and lifecycle event emission. Cross-agent messages should continue to pass through `PolicyGate` before dispatch. Secrets and credentials should not be placed in message parts; use transport-specific authentication outside the message body.

`InProcessTransport` can be configured with `known_senders`. When configured, messages whose `from_agent` is not registered are rejected before routing and recorded as `a2a.rejected`. Runtime enables this for its default in-process transport and allows registered agent ids plus `coordinator` and `system`.

`Part.untrusted` marks content from outside the current trust boundary, including retrieval results, tool output, or cross-agent data that should be treated as data rather than instructions. When a transport has a `PolicyGate`, untrusted parts are scanned before delivery by the `no_prompt_injection` rule. Messages containing common prompt-injection phrases are rejected and are not delivered to the target agent.
