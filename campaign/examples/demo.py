"""End-to-end demo for the campaign multi-agent framework.

Runs a minimal pipeline in STUB mode (no real LLM / no API keys) to verify
the full event-sourcing + role coordination + review loop in one shot.

Usage:
    python -m campaign          # via __main__.py
    python examples/demo.py     # directly (when campaign is on PYTHONPATH)
"""
from __future__ import annotations

import asyncio
import os
import tempfile

from campaign.app.config import Config
from campaign.app.runtime import Runtime
from campaign.core.events import SqliteEventLog
from campaign.core.models import AgentSpec, ExecutionOrder, Task
from campaign.core.state import derive_state
from campaign.observability.tracer import InMemoryTracer
from campaign.roles.executor import Executor
from campaign.roles.retriever import Retriever
from campaign.roles.reviewer import Reviewer

SEP = "=" * 60


async def main() -> None:
    # -- 1. Setup: temp event log + default config ----------
    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="campaign_demo_")
    os.close(fd)

    log = SqliteEventLog(db_path)
    config = Config()  # stubs everywhere -- no LLM, no API keys

    print(SEP)
    print("  Campaign Multi-Agent Framework -- End-to-End Demo (STUB)")
    print(f"  Event Log : {db_path}")
    print(SEP)

    # -- 2. Define & register agents ------------------------
    exec_spec = AgentSpec(
        id="executor-1",
        role="executor",
        model_tier="value",
        skills=["code", "test", "refactor"],
    )
    review_spec = AgentSpec(
        id="reviewer-1",
        role="reviewer",
        model_tier="flagship",
        skills=["review"],
    )

    retriever_spec = AgentSpec(
        id="retriever-1",
        role="retriever",
        model_tier="value",
        skills=["retrieve"],
    )

    executor = Executor(exec_spec, log)          # no llm -> stub output
    reviewer = Reviewer(review_spec, log)        # no llm -> rule-based verdict
    retriever = Retriever(retriever_spec, log)   # no llm -> stub summary

    tracer = InMemoryTracer()                    # 可观测性(M7)：内存 tracer

    runtime = Runtime(log, config)
    runtime.set_tracer(tracer)                   # 接入可观测性
    runtime.register_agent(executor)
    runtime.register_agent(reviewer)
    runtime.register_agent(retriever)

    print("\n[Agents Registered]")
    print(f"  Executor  : {exec_spec.id}  skills={exec_spec.skills}")
    print(f"  Reviewer  : {review_spec.id}  strictness={reviewer.strictness}")
    print(f"  Retriever : {retriever_spec.id}  skills={retriever_spec.skills}")

    # -- 3. Build execution order --------------------------
    order = ExecutionOrder(
        objective="Demo: build and test a small feature",
        constraints=["no real API keys", "all agents in stub mode"],
        budget={"max_tokens": 1000},
        tasks=[
            Task(
                id="t0",
                goal="Retrieve prior login-module incidents for context",
                difficulty="simple",
                required_skills=["retrieve"],
                acceptance="Relevant summary returned",
            ),
            Task(
                id="t1",
                goal="Write unit tests for the login module",
                difficulty="simple",
                required_skills=["code"],
                acceptance="Tests pass with >80% coverage",
            ),
            Task(
                id="t2",
                goal="Implement password hashing middleware",
                difficulty="medium",
                required_skills=["refactor"],
                acceptance="All existing tests still pass; new middleware is thread-safe",
            ),
            Task(
                id="t3",
                goal="Run integration test suite and fix regressions",
                difficulty="hard",
                required_skills=["test"],
                acceptance="Zero regression failures; report generated",
            ),
        ],
    )

    print("\n[Execution Order]")
    print(f"  Objective : {order.objective}")
    for t in order.tasks:
        print(f"  - {t.id}  [{t.difficulty:6}]  {t.goal}")

    # -- 4. Run --------------------------------------------
    print("\n[Running...]")
    result = await runtime.run(order)

    print("\n[Result Summary]")
    print(f"  Objective   : {result['objective']}")
    print(f"  Tasks total : {result['tasks_total']}")
    for r in result["results"]:
        tid = r.get("task_id", "?")
        status = r.get("status", "?")
        review = r.get("review", {})
        passed = "[PASS]" if review.get("passed") else "[FAIL]"
        score = review.get("score", "N/A")
        reasons = review.get("reasons", [])
        print(f"  - {tid}: {status:<6}  review={passed}  score={score}  {reasons}")

    # -- 5. Replay events -> derive state -----------------
    events = await log.replay()
    state = derive_state(events)

    print(f"\n[Derived State]")
    print(f"  Pending   : {state.tasks_pending}")
    print(f"  Done      : {state.tasks_done}")
    print(f"  Frozen    : {state.tasks_frozen}")
    print(f"  Incidents : {len(state.incidents)}")
    for inc in state.incidents:
        print(f"    - task={inc.get('task_id')}  reason={inc.get('reason', '?')}")

    print(f"\n[Event Stream]  ({len(events)} events)")
    print(f"  {'seq':>4}  {'type':<26} {'actor':<16}  payload")
    print(f"  {'-'*4}  {'-'*26} {'-'*16}  {'-'*40}")
    for e in events:
        print(f"  {e.seq:>4}  {e.type:<26} {e.actor:<16}  {_brief(e.payload)}")

    # -- 6. Observability (M7) snapshot --------------------
    print(f"\n[Observability]  ({len(tracer.traces)} spans traced)")
    for tr in tracer.traces:
        print(f"  span={tr['name']:<14} actor={tr['actor']:<12} status={tr['status']:<6} {tr['duration_ms']:.1f}ms")
    print(f"  online scores : {tracer.scores}")

    # -- 7. Cleanup ----------------------------------------
    log.close()
    os.unlink(db_path)
    print(f"\n[Cleanup]  removed {db_path}")
    print(SEP)
    print("  Demo complete -- all agents ran in stub mode, zero API calls.")
    print(SEP)


def _brief(payload: dict) -> str:
    """Compact one-line payload for the event-stream table."""
    tid = payload.get("task_id", "")
    agent = payload.get("agent", "")
    objective = payload.get("objective", "")
    count = payload.get("task_count") or payload.get("results_count")

    if tid and agent:
        return f"task={tid} -> agent={agent}"
    if tid:
        return f"task={tid}"
    if objective:
        return f"objective={objective[:50]}"
    if count is not None:
        return f"count={count}"
    return str(payload)[:55]


if __name__ == "__main__":
    asyncio.run(main())
