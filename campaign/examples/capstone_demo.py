"""Capstone end-to-end demo wiring ALL major implemented capabilities of the
campaign multi-agent framework in a single self-contained, deterministic run.

Stub mode only — no real LLM / no API keys / no network.  ASCII-safe output.
Zero new dependencies.  No circular imports.

Run:
    python -m campaign.examples.capstone_demo

Capabilities exercised:
  - M0: SqliteEventLog (temp file), derive_state, ScratchpadMemory, SessionMemory
  - M2: Executor / Retriever / Reviewer role registration + stub execution
  - M4: CircuitBreaker (fail_threshold / cooldown / brake broadcast)
  - M5: CapacityLedger + Mobilizer (health tracking / attrition assessment)
  - M6: Governor + Policy-as-Code (BudgetRule / AuthorityRule / DataEgressRule / InjectionScanRule)
  - M7: InMemoryTracer (span tracing / metering / online scoring), Config
  - M8: EvalHarness + EvalCase gate (threshold-based pass/fail)
  - M-G: SqliteKnowledgeStore (in-memory TF-IDF lexical RAG) + knowledge injection
  - M-F: Session memory recall + scratchpad snapshot injection
  - Deep#1: set_concurrency(2) + PolicyGate (serialised budget/state under lock)
  - Deep#3: PolicyGate shared between Runtime and potential LLMClient (wired)
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from collections import Counter

from campaign.app.config import Config
from campaign.app.runtime import Runtime
from campaign.core.events import SqliteEventLog
from campaign.core.models import AgentSpec, ExecutionOrder, Task
from campaign.eval.harness import EvalCase, EvalHarness
from campaign.governance.governor import Governor
from campaign.governance.policy import (
    AuthorityRule,
    BudgetRule,
    DataEgressRule,
    InjectionScanRule,
)
from campaign.knowledge import SqliteKnowledgeStore
from campaign.observability.tracer import InMemoryTracer
from campaign.resilience.breaker import CircuitBreaker
from campaign.resilience.mobilization import CapacityLedger, Mobilizer
from campaign.roles.executor import Executor
from campaign.roles.retriever import Retriever
from campaign.roles.reviewer import Reviewer

SEP = "=" * 60


class _DemoBudgetRule(BudgetRule):
    """BudgetRule variant with a permissive default so the transport-layer
    untrusted-content scan (which passes no budget context) does not
    falsely block knowledge injection.

    Real spend enforcement is driven by ExecutionOrder.budget (100k).
    """

    DEFAULT_TOKEN_LIMIT = 1_000_000.0

    def check(self, action, context):
        # Inject a high default budget when none is present, so
        # _scan_untrusted in InProcessTransport does not trip BudgetRule.
        # Mutate in-place so BudgetRule.check can write _new_spend back.
        if "budget" not in context:
            context["budget"] = {"token_limit": self.DEFAULT_TOKEN_LIMIT}
        return super().check(action, context)


async def main() -> dict:
    """Run the full capstone demo and return a structured summary dict."""
    # ── 1. Temp event log + default config ─────────────────────────────
    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="capstone_")
    os.close(fd)

    log = SqliteEventLog(db_path)
    config = Config()  # hybrid deploy, privacy_strict=True, no LLM tiers

    print(SEP)
    print("  Campaign Multi-Agent Framework -- Capstone Demo (STUB)")
    print(f"  Event Log : {db_path}")
    print(SEP)

    # ── 2. Session context from prior runs (empty for fresh log) ──────
    # The SessionMemory recall path is exercised even though it returns []
    # for a first-ever run.  The code path in Runtime._execute_one_task
    # still loads the SessionMemory instance and injects the (empty)
    # session context into the message.

    # ── 3. Seeded knowledge store (in-memory TF-IDF) ──────────────────
    store = SqliteKnowledgeStore(":memory:")
    store.add(
        "doc-hash-1",
        "Password hashing should use bcrypt with a cost factor of at least 12. "
        "Salt must be generated per-password using a CSPRNG. "
        "Never use MD5 or SHA1 for password storage.",
        {"topic": "security", "level": "advanced"},
    )
    store.add(
        "doc-test-2",
        "Unit testing best practices: follow the AAA pattern Arrange-Act-Assert. "
        "Use parametrised tests for edge cases. "
        "Mock external dependencies but never mock the unit under test.",
        {"topic": "testing", "level": "intermediate"},
    )
    store.add(
        "doc-async-3",
        "Python asyncio patterns: use asyncio.gather for concurrent IO-bound work. "
        "Avoid blocking the event loop with CPU-intensive tasks. "
        "Use asyncio.Semaphore for concurrency limiting.",
        {"topic": "async"},
    )

    # ── 4. Agent specs ────────────────────────────────────────────────
    exec_spec = AgentSpec(
        id="executor-1",
        role="executor",
        model_tier="value",
        skills=["code", "test", "refactor"],
    )
    retriever_spec = AgentSpec(
        id="retriever-1",
        role="retriever",
        model_tier="value",
        skills=["retrieve"],
    )
    reviewer_spec = AgentSpec(
        id="reviewer-1",
        role="reviewer",
        model_tier="flagship",
        skills=["review"],
    )

    executor = Executor(exec_spec, log)       # no llm → stub output
    retriever = Retriever(retriever_spec, log)  # no llm, no store → stub summary
    reviewer = Reviewer(reviewer_spec, log)     # no llm → rule-based verdict

    tracer = InMemoryTracer()

    # ── 5. Assemble Runtime ───────────────────────────────────────────
    runtime = Runtime(log, config)
    runtime.register_agent(executor)
    runtime.register_agent(retriever)
    runtime.register_agent(reviewer)

    # Governance (M6): Policy-as-Code with all 4 rule types wired.
    # _DemoBudgetRule uses a permissive default so the _scan_untrusted
    # path in InProcessTransport (which passes no budget context) does not
    # falsely block legitimate knowledge injection.
    governor = Governor(
        log,
        rules=[
            AuthorityRule(),
            DataEgressRule(),
            InjectionScanRule(),
            _DemoBudgetRule(),
        ],
    )
    runtime.set_governor(governor)

    # Resilience (M4): CircuitBreaker
    breaker = CircuitBreaker(log, fail_threshold=3, cooldown_sec=30.0)
    runtime.set_breaker(breaker)

    # Resilience (M5): Mobilizer + CapacityLedger
    ledger = CapacityLedger()
    mobilizer = Mobilizer(log, ledger)
    runtime.set_mobilizer(mobilizer)

    # Observability (M7): InMemoryTracer
    runtime.set_tracer(tracer)

    # Concurrency (Deep#1): allow up to 2 tasks in parallel
    runtime.set_concurrency(2)

    # Memory (M-F): opt-in session + scratchpad injection
    runtime.enable_memory(session=True, scratch=True)

    # Knowledge (M-G): opt-in long-term RAG injection
    runtime.set_knowledge_store(store, k=2)

    print("\n[Registered Agents]")
    print(f"  Executor   : {exec_spec.id}  skills={exec_spec.skills}  tier={exec_spec.model_tier}")
    print(f"  Retriever  : {retriever_spec.id}  skills={retriever_spec.skills}  tier={retriever_spec.model_tier}")
    print(f"  Reviewer   : {reviewer_spec.id}  skills={reviewer_spec.skills}  tier={reviewer_spec.model_tier}")
    print(f"  Concurrency: {runtime._concurrency}")
    print(f"  Governor   : {len(governor.rules)} rules (budget/egress/authority/injection)")
    _doc_count = len(store.search("password hashing testing", k=10))
    print(f"  Knowledge  : {_doc_count} docs reachable via TF-IDF (3 total seeded)")
    print(f"  Memory     : session=True  scratch=True")

    # ── 6. Build ExecutionOrder with dependency chain ──────────────────
    # t0 (retrieve) and t1 (implement) run in parallel with concurrency=2.
    # t2 (test) depends on t1, so it only starts after t1 completes.
    # Budget is intentionally large (100k tokens) so Governance does NOT block.
    order = ExecutionOrder(
        objective="Capstone: build a password-hashing middleware with tests",
        constraints=["no real API keys", "all stub mode", "ASCII output"],
        budget={"token_limit": 100_000},
        tasks=[
            Task(
                id="t0-retrieve",
                goal="Retrieve security best practices for password hashing",
                difficulty="simple",
                required_skills=["retrieve"],
                acceptance="Relevant security context returned",
            ),
            Task(
                id="t1-implement",
                goal="Implement password hashing middleware using bcrypt",
                difficulty="medium",
                required_skills=["code"],
                acceptance="Middleware accepts password, returns hash; thread-safe",
            ),
            Task(
                id="t2-test",
                goal="Write unit tests for the password hashing middleware",
                difficulty="medium",
                required_skills=["test"],
                depends_on=["t1-implement"],
                acceptance="Tests cover normal path + edge cases; all pass",
            ),
        ],
    )

    print("\n[Execution Order]")
    print(f"  Objective  : {order.objective}")
    print(f"  Budget     : {order.budget}")
    print(f"  Constraints: {order.constraints}")
    for t in order.tasks:
        deps = f"  (depends_on={t.depends_on})" if t.depends_on else ""
        skills = ",".join(t.required_skills)
        print(f"  {t.id:<14} [{t.difficulty:6}] skills=[{skills}]{deps}")

    # ── 7. Run ────────────────────────────────────────────────────────
    print("\n[Running...]")
    result = await runtime.run(order)
    run_id = result["run_id"]

    # ── 8. Per-task results + review ──────────────────────────────────
    print(f"\n[Result]  run_id={run_id}")
    print(f"  Objective   : {result['objective']}")
    print(f"  Tasks total : {result['tasks_total']}")
    for r in result["results"]:
        tid = r.get("task_id", "?")
        status = r.get("status", "?")
        review = r.get("review", {})
        passed = review.get("passed")
        score = review.get("score", "N/A")
        reasons = review.get("reasons", [])
        flag = "[PASS]" if passed else "[FAIL]" if passed is False else "[N/A]"
        print(f"  {tid:<14} status={status:<6}  review={flag}  score={score}  {reasons}")
        scratch = r.get("scratchpad")
        if scratch:
            print(f"           scratchpad={scratch}")

    # ── 9. Derived state (run_id-scoped) ─────────────────────────────
    state = await runtime.state(run_id)
    print(f"\n[State]  run_id={run_id}")
    print(f"  done              : {state.tasks_done}")
    print(f"  failed            : {state.tasks_failed}")
    print(f"  frozen            : {state.tasks_frozen}")
    print(f"  skipped           : {state.tasks_skipped}")
    print(f"  awaiting_input    : {state.tasks_awaiting_input}")
    print(f"  pending           : {state.tasks_pending}")
    print(f"  incidents         : {len(state.incidents)}")
    for inc in state.incidents:
        print(f"    - task={inc.get('task_id')}  reason={inc.get('reason', '?')}")

    # ── 10. Event stream counts ──────────────────────────────────────
    events = await log.replay()
    type_counts = Counter(e.type for e in events)
    print(f"\n[Events]  {len(events)} total events")
    for typ in sorted(type_counts):
        print(f"  {typ:<28}  {type_counts[typ]}")

    # Highlight specific injection events
    for e in events:
        if e.type == "knowledge.injected":
            print(f"  -> knowledge.injected  task={e.payload.get('task_id')}  "
                  f"docs={e.payload.get('doc_ids')}")
        if e.type == "task.assigned":
            print(f"  -> task.assigned       task={e.payload.get('task_id')}  "
                  f"agent={e.payload.get('agent')}")

    # ── 11. Observability snapshot ───────────────────────────────────
    print(f"\n[Observability]")
    print(f"  tracer spans  : {len(tracer.traces)}")
    for tr in tracer.traces:
        print(f"    span={tr['name']:<14} actor={tr['actor']:<14} "
              f"status={tr['status']:<6} {tr['duration_ms']:.2f}ms")
    print(f"  online scores : {tracer.scores}")

    # ── 12. Eval gate (M8) ───────────────────────────────────────────
    print(f"\n[Eval Gate]")
    eval_harness = EvalHarness(threshold=0.5)

    # Build an eval case that re-runs a variant of the same order.
    eval_case_1 = EvalCase(
        id="capstone-mini",
        order={
            "objective": "Eval: build hashing middleware (mini)",
            "budget": {"token_limit": 100_000},
            "tasks": [
                {
                    "id": "e0",
                    "goal": "Implement password hashing middleware",
                    "difficulty": "medium",
                    "required_skills": ["code"],
                    "acceptance": "Middleware returns valid hash",
                },
            ],
        },
        expected={"tasks_done": 1, "min_score": 0.5},
        weight=1.0,
    )

    passed = await eval_harness.gate(runtime, [eval_case_1])
    status = "PASS" if passed else "FAIL"
    print(f"  Gate threshold : {eval_harness.threshold}")
    print(f"  Case           : {eval_case_1.id}  weight={eval_case_1.weight}")
    print(f"  Result         : {status}")
    summary_scores = eval_harness.results_summary()
    for cid, data in summary_scores.items():
        print(f"    {cid} -> score={data['score']:.3f}")

    # ── 13. Cleanup ──────────────────────────────────────────────────
    store.close()
    log.close()
    os.unlink(db_path)
    print(f"\n[Cleanup]  removed {db_path}  |  knowledge store closed")
    print(SEP)
    print("  Capstone demo complete -- all agents in stub mode, zero API calls.")
    print(SEP)

    return {
        "run_id": run_id,
        "tasks_total": result["tasks_total"],
        "tasks_done": len(state.tasks_done),
        "tasks_failed": len(state.tasks_failed),
        "events_total": len(events),
        "event_counts": dict(type_counts),
        "tracer_spans": len(tracer.traces),
        "eval_passed": passed,
    }


if __name__ == "__main__":
    summary = asyncio.run(main())
    print(f"\n[Summary dict]  {summary}")
