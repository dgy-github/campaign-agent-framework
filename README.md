# campaign — Multi-Agent Command / Runtime / Rehearsal Framework

`campaign` is a runnable reference implementation for engineering-grade multi-agent systems.
It is built around one idea: **an agent team should be commanded, governed, rehearsed, observed, and recovered like a production system, not treated as a loose chat room of personas.**

The framework turns multi-agent orchestration into a concrete system:

- **Command layer**: `ExecutionOrder` and `Task` define objective, constraints, skills, budget, and acceptance criteria.
- **Runtime layer**: Coordinator / Executor / Retriever / Reviewer / Reserve agents collaborate through explicit A2A messages.
- **Governance layer**: every task and LLM call can pass through `PolicyGate` for budget, authority, data-egress, and injection checks.
- **Event-sourced state**: `EventLog` is append-only; runtime state is derived by replaying events with `run_id` isolation.
- **Rehearsal layer**: `EvalHarness` and `ChaosDrill` make release readiness measurable instead of vibe-based.
- **A2A transport**: in-process, HTTP JSON-RPC, SSE streaming, capability discovery, idempotent retry, and remote agent proxy.

This is intentionally small enough to read, but complete enough to discuss serious agent engineering topics: trust boundaries, HITL, distributed coordination, cost control, RAG memory, tracing, circuit breaking, and testable failure modes.

## Architecture Page

Open the live HTML architecture view:

- https://dgy-github.github.io/campaign-agent-framework/

Or open the local file:

```text
campaign-architecture.html
```

It explains the framework as a "campaign command center": command -> runtime -> A2A protocol -> governance -> rehearsal.

## What Makes It Different

Most multi-agent demos stop at role prompts. `campaign` focuses on the parts that make agent systems survivable:

- **State is derived, not guessed**: task status comes from replaying events, not from mutable global state.
- **Judge and worker are separated**: `Reviewer` validates results independently from executors.
- **Remote agents share one protocol**: local and remote workers both receive `Message` envelopes.
- **Human intervention is durable**: input-required and approval states can be resumed across processes.
- **The system can fail loudly**: chaos drills, circuit breakers, brakes, and explicit governance violations are first-class.
- **Tests are offline**: the suite uses mocked providers and local stores, so no API key or network is required.

## Quick Start

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q
python -m campaign
python -m campaign.examples.capstone_demo
python -m campaign.examples.distributed_demo
```

Expected test result:

```text
172 passed
```

## Repository Map

| Path | Responsibility |
| --- | --- |
| `campaign/core/events.py` | Append-only event log, replay, subscription, rollback/truncation. |
| `campaign/core/state.py` | Derive runtime state from events, scoped by `run_id`. |
| `campaign/core/models.py` | `ExecutionOrder`, `Task`, `AgentSpec`, validation contracts. |
| `campaign/protocol.py` | A2A `Message`, `Part`, `AgentCard`, task states. |
| `campaign/transport.py` | In-process and HTTP JSON-RPC transport, SSE, discovery, remote proxy. |
| `campaign/roles/` | Coordinator, Executor, Retriever, Reviewer, Reserve agents. |
| `campaign/routing/` | Skill registry and ROI router. |
| `campaign/governance/` | Policy-as-code, Governor, PolicyGate. |
| `campaign/resilience/` | Circuit breaker, checkpoint rollback, mobilization. |
| `campaign/observability/` | Tracer spans, meters, scores. |
| `campaign/llm/` | OpenAI-compatible chat / embedding adapter with mock support. |
| `campaign/memory.py` | Scratchpad and session memory. |
| `campaign/knowledge.py` | SQLite TF-IDF knowledge store with neural embedding seam. |
| `campaign/eval/` | Eval harness and chaos drill. |
| `campaign/app/runtime.py` | Runtime assembly, run/resume/discover APIs. |
| `docs/` | Methodology, A2A, scaling, RAG, runbook, pitfalls, worklog. |

## Design Boundaries

- The distributed path is a working MVP: HTTP transport + shared SQLite event store + remote proxy. Production-grade distributed consistency still needs shared budget, health, breaker, and leader coordination.
- Long-term knowledge is lexical TF-IDF by default, not neural embedding. The adapter seam is kept open deliberately.
- External content is treated as untrusted input. Retrieved data can be marked `untrusted` before entering agent context.

## Why This Project Exists

`campaign` is designed for people who want to understand how agent applications become real systems:

- How do agents coordinate without hidden shared mutable state?
- How do you recover after a task pauses for human approval?
- How do you keep budget and authority checks from becoming decorative?
- How do you compare local and remote workers under one protocol?
- How do you turn failures into rehearsal data instead of production surprises?

That is the point of the framework: **not more agents, better command.**
