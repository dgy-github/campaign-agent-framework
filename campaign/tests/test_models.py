"""M0-M8 完整测试套件。

覆盖：数据模型、Event Log、State 派生、LLM Client、角色、路由、
熔断/制动、检查点、动员减员、预备队、治理层、可观测性、Config、Eval/Chaos。
"""
import asyncio
import os
import tempfile

import pytest

from campaign.core.events import Event, SqliteEventLog
from campaign.core.models import AgentSpec, ExecutionOrder, Task, Difficulty, Role
from campaign.core.state import derive_state, State

# ═══════════════════════════════════════════════════════════
# 测试工具
# ═══════════════════════════════════════════════════════════

def _event(seq: int, typ: str, actor: str = "system", payload: dict | None = None) -> Event:
    from datetime import datetime, timezone
    return Event(
        seq=seq,
        ts=datetime.now(timezone.utc),
        type=typ,
        actor=actor,
        payload=payload or {},
    )


@pytest.fixture
def event_log():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="campaign_test_")
    os.close(fd)
    log = SqliteEventLog(db_path=path)
    yield log
    log.close()
    try:
        os.unlink(path)
    except OSError:
        pass


# ═══════════════════════════════════════════════════════════
# M0: 数据模型 + Event Log + State
# ═══════════════════════════════════════════════════════════

def test_models_construct():
    spec = AgentSpec(id="exec-1", role="executor", model_tier="coder", skills=["写代码"])
    task = Task(id="t1", goal="写个函数", difficulty="simple", required_skills=["写代码"])
    order = ExecutionOrder(objective="demo", tasks=[task])
    assert order.tasks[0].id == "t1"
    assert "写代码" in spec.skills


def test_task_defaults():
    t = Task(id="t0", goal="test")
    assert t.difficulty == "medium"
    assert t.degradable is True


def test_agent_spec_proficiency():
    spec = AgentSpec(
        id="a1", role="executor", model_tier="coder",
        skills=["coding", "testing"],
        proficiency={"coding": 80, "testing": 50},
    )
    assert spec.proficiency["coding"] == 80


@pytest.mark.asyncio
async def test_event_log_append_and_replay(event_log):
    e1 = await event_log.append("task.assigned", "exec-1", {"task_id": "t1"})
    e2 = await event_log.append("task.done", "exec-1", {"task_id": "t1"})
    assert e1.seq == 1
    assert e2.seq == 2
    all_events = await event_log.replay(since=0)
    assert len(all_events) == 2
    partial = await event_log.replay(since=1)
    assert len(partial) == 1
    assert partial[0].seq == 2


@pytest.mark.asyncio
async def test_event_log_replay_empty(event_log):
    assert await event_log.replay() == []


@pytest.mark.asyncio
async def test_event_log_subscribe(event_log):
    received: list[Event] = []

    async def collector():
        async for event in event_log.subscribe():
            received.append(event)
            if len(received) >= 2:
                break

    task = asyncio.create_task(collector())
    await asyncio.sleep(0.05)
    await event_log.append("task.assigned", "a1", {"task_id": "x"})
    await event_log.append("task.done", "a1", {"task_id": "x"})
    await asyncio.wait_for(task, timeout=3.0)
    assert len(received) == 2


def test_derive_state_empty():
    s = derive_state([])
    assert s.tasks_pending == [] and s.tasks_done == [] and s.tasks_frozen == []


def test_derive_state_assigned_done():
    events = [
        _event(1, "task.assigned", payload={"task_id": "t1"}),
        _event(2, "task.done", payload={"task_id": "t1"}),
    ]
    s = derive_state(events)
    assert s.tasks_pending == []
    assert s.tasks_done == ["t1"]


def test_derive_state_frozen():
    events = [
        _event(1, "task.assigned", payload={"task_id": "t1"}),
        _event(2, "task.frozen", payload={"task_id": "t1", "reason": "no_agent"}),
    ]
    s = derive_state(events)
    assert s.tasks_frozen == ["t1"]


def test_derive_state_is_pure():
    events = [_event(1, "task.assigned", payload={"task_id": "t1"})]
    s1 = derive_state(events)
    s2 = derive_state(events)
    assert s1 == s2


def test_derive_state_complex():
    events = [
        _event(1, "task.assigned", payload={"task_id": "t1"}),
        _event(2, "task.done", payload={"task_id": "t1"}),
        _event(3, "task.frozen", payload={"task_id": "t2"}),
        _event(4, "incident", payload={"reason": "boom"}),
    ]
    s = derive_state(events)
    assert s.tasks_done == ["t1"]
    assert s.tasks_frozen == ["t2"]
    assert len(s.incidents) == 1


# ═══════════════════════════════════════════════════════════
# M1: LLM Client (mock 模式)
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_llm_client_mock_complete():
    from campaign.llm.client import LLMClient, TierConfig, extract_text

    mock_responses = {
        "coder": [{"choices": [{"message": {"content": "mock response"}}]}],
    }
    client = LLMClient(
        tiers={"coder": TierConfig(model="test", base_url="http://localhost")},
        mock_responses=mock_responses,
    )
    resp = await client.complete("coder", [{"role": "user", "content": "hi"}])
    assert extract_text(resp) == "mock response"
    await client.close()


@pytest.mark.asyncio
async def test_llm_client_mock_tool_call():
    from campaign.llm.client import LLMClient, TierConfig, extract_tool_calls

    mock_responses = {
        "flagship": [{"choices": [{"message": {"tool_calls": [
            {"id": "call_1", "function": {"name": "test_fn", "arguments": "{}"}}
        ]}}]}],
    }
    client = LLMClient(
        tiers={"flagship": TierConfig(model="test", base_url="http://localhost")},
        mock_responses=mock_responses,
    )
    resp = await client.tool_call("flagship", [{"role": "user", "content": "call"}], [])
    calls = extract_tool_calls(resp)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "test_fn"
    await client.close()


@pytest.mark.asyncio
async def test_llm_client_missing_tier():
    from campaign.llm.client import LLMClient, LLMError
    client = LLMClient(tiers={})
    with pytest.raises(LLMError, match="未配置 tier"):
        await client.complete("flagship", [])
    await client.close()


# ═══════════════════════════════════════════════════════════
# M2: 角色 + Runtime 最小闭环
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_executor_stub(event_log):
    from campaign.roles.executor import Executor
    spec = AgentSpec(id="exec-1", role="executor", model_tier="coder", skills=["coding"])
    task = Task(id="t1", goal="test", required_skills=["coding"])
    exec = Executor(spec, event_log)
    result = await exec.handle(task)
    assert result["task_id"] == "t1"
    assert "output" in result


@pytest.mark.asyncio
async def test_reviewer_stub(event_log):
    from campaign.roles.reviewer import Reviewer
    spec = AgentSpec(id="rev-1", role="reviewer", model_tier="flagship", skills=[])
    task = Task(id="t1", goal="test", acceptance="code must compile")
    reviewer = Reviewer(spec, event_log)
    result = await reviewer.handle(task, {"output": "test output"})
    assert "passed" in result


@pytest.mark.asyncio
async def test_reviewer_strictness(event_log):
    from campaign.roles.reviewer import Reviewer
    spec = AgentSpec(id="rev-1", role="reviewer", model_tier="flagship", skills=[])
    task = Task(id="t1", goal="test")

    # 默认 strictness
    r1 = Reviewer(spec, event_log)
    default = await r1.handle(task, {"output": "test"})

    # 高 strictness（减员时使用）
    r2 = Reviewer(spec, event_log, strictness=0.9)
    strict = await r2.handle(task, {"output": "test"})

    # strictness 越高分数越低
    assert strict["score"] <= default["score"]


@pytest.mark.asyncio
async def test_retriever_stub(event_log):
    from campaign.roles.retriever import Retriever
    spec = AgentSpec(id="ret-1", role="retriever", model_tier="value", skills=["search"])
    task = Task(id="t1", goal="find docs", required_skills=["search"])
    retriever = Retriever(spec, event_log)
    result = await retriever.handle(task)
    assert "summary" in result


@pytest.mark.asyncio
async def test_coordinator_dispatch(event_log):
    from campaign.roles.coordinator import Coordinator
    from campaign.roles.executor import Executor

    exec_spec = AgentSpec(id="exec-1", role="executor", model_tier="coder", skills=["coding"])
    coord_spec = AgentSpec(id="coord-1", role="coordinator", model_tier="flagship", skills=[])
    executor = Executor(exec_spec, event_log)
    coordinator = Coordinator(coord_spec, event_log)

    task = Task(id="t1", goal="test", required_skills=["coding"])
    result = await coordinator.dispatch(task, [executor])
    assert result["status"] == "done"


@pytest.mark.asyncio
async def test_coordinator_dispatch_no_agent(event_log):
    from campaign.roles.coordinator import Coordinator
    coord_spec = AgentSpec(id="coord-1", role="coordinator", model_tier="flagship", skills=[])
    coordinator = Coordinator(coord_spec, event_log)

    task = Task(id="t1", goal="test", required_skills=["nonexistent_skill"])
    result = await coordinator.dispatch(task, [])
    assert result["status"] == "frozen"


@pytest.mark.asyncio
async def test_coordinator_decompose_passthrough(event_log):
    from campaign.roles.coordinator import Coordinator
    coord_spec = AgentSpec(id="coord-1", role="coordinator", model_tier="flagship", skills=[])
    coordinator = Coordinator(coord_spec, event_log)

    order = ExecutionOrder(
        objective="test",
        tasks=[Task(id="t1", goal="do sth", difficulty="simple")],
    )
    tasks = await coordinator.decompose(order)
    assert len(tasks) == 1
    assert tasks[0].id == "t1"


@pytest.mark.asyncio
async def test_runtime_minimal_loop(event_log):
    from campaign.app.config import Config
    from campaign.app.runtime import Runtime
    from campaign.roles.executor import Executor
    from campaign.roles.reviewer import Reviewer

    exec_spec = AgentSpec(id="exec-1", role="executor", model_tier="coder", skills=["coding"])
    rev_spec = AgentSpec(id="rev-1", role="reviewer", model_tier="flagship", skills=[])

    config = Config()
    runtime = Runtime(event_log, config)
    runtime.register_agent(Executor(exec_spec, event_log))
    runtime.register_agent(Reviewer(rev_spec, event_log))

    order = ExecutionOrder(
        objective="demo",
        tasks=[Task(id="t1", goal="execute test", difficulty="simple", required_skills=["coding"])],
    )
    result = await runtime.run(order)
    assert result["tasks_total"] == 1
    assert len(result["results"]) == 1


# ═══════════════════════════════════════════════════════════
# M3: 能力注册表 + ROI 路由
# ═══════════════════════════════════════════════════════════

def test_skill_registry_candidates():
    from campaign.routing.skill_registry import SkillRegistry

    r = SkillRegistry()
    r.register(AgentSpec(id="a1", role="executor", model_tier="coder", skills=["coding"]))
    r.register(AgentSpec(id="a2", role="executor", model_tier="coder", skills=["testing"]))
    r.register(AgentSpec(id="a3", role="executor", model_tier="coder", skills=["coding", "testing"]))

    task = Task(id="t1", goal="test", required_skills=["coding", "testing"])
    candidates = r.candidates(task)
    assert len(candidates) == 1
    assert candidates[0].id == "a3"


def test_skill_registry_candidates_by_proficiency():
    from campaign.routing.skill_registry import SkillRegistry

    r = SkillRegistry()
    r.register(AgentSpec(id="a1", role="executor", model_tier="coder",
                          skills=["coding"], proficiency={"coding": 50}))
    r.register(AgentSpec(id="a2", role="executor", model_tier="coder",
                          skills=["coding"], proficiency={"coding": 90}))

    task = Task(id="t1", goal="test", required_skills=["coding"])
    candidates = r.candidates(task)
    # 熟练度高的排前面
    assert candidates[0].id == "a2"


def test_skill_registry_no_candidates():
    from campaign.routing.skill_registry import SkillRegistry

    r = SkillRegistry()
    r.register(AgentSpec(id="a1", role="executor", model_tier="coder", skills=["coding"]))
    task = Task(id="t1", goal="test", required_skills=["rocket_science"])
    assert r.candidates(task) == []


def test_skill_registry_useful():
    from campaign.routing.skill_registry import SkillRegistry

    r = SkillRegistry()
    r.register(AgentSpec(id="a1", role="executor", model_tier="coder", skills=["coding"]))
    task = Task(id="t1", goal="test", required_skills=["coding"])
    candidates = r.candidates(task)
    assert len(candidates) > 0


@pytest.mark.asyncio
async def test_router_skips_unskilled_agent(event_log):
    from campaign.routing.router import Router
    from campaign.routing.skill_registry import SkillRegistry

    r = SkillRegistry()
    r.register(AgentSpec(id="a1", role="executor", model_tier="coder", skills=["testing"]))
    router = Router(r, event_log)

    task = Task(id="t1", goal="code", required_skills=["coding"])
    # Nobody has "coding" → None
    result = await router.pick(task)
    assert result is None


@pytest.mark.asyncio
async def test_router_picks_best(event_log):
    from campaign.routing.router import Router
    from campaign.routing.skill_registry import SkillRegistry

    r = SkillRegistry()
    # 两个 candidate，a2 有更高熟练度 + 更便宜 tier
    r.register(AgentSpec(id="a1", role="executor", model_tier="flagship",
                          skills=["coding"], proficiency={"coding": 50}))
    r.register(AgentSpec(id="a2", role="executor", model_tier="value",
                          skills=["coding"], proficiency={"coding": 90}))

    router = Router(r, event_log)
    task = Task(id="t1", goal="code", difficulty="simple", required_skills=["coding"])
    picked = await router.pick(task)
    assert picked is not None
    # value tier (< 100 cost) beats flagship (500 cost) + lower proficiency
    assert picked.id == "a2"


# ═══════════════════════════════════════════════════════════
# M4: 熔断 + 协同制动 + 检查点
# ═══════════════════════════════════════════════════════════

def test_circuit_breaker_trips(event_log):
    from campaign.resilience.breaker import CircuitBreaker
    cb = CircuitBreaker(event_log, fail_threshold=3)
    assert not cb.record_failure("a1")
    assert not cb.record_failure("a1")
    assert cb.record_failure("a1")  # 第 3 次触发
    assert cb.is_tripped("a1")


def test_circuit_breaker_success_resets(event_log):
    from campaign.resilience.breaker import CircuitBreaker
    cb = CircuitBreaker(event_log, fail_threshold=3)
    cb.record_failure("a1")
    cb.record_failure("a1")
    cb.record_success("a1")
    # 重置后计数归零
    assert not cb.is_tripped("a1")


def test_circuit_breaker_trips_after_threshold(event_log):
    from campaign.resilience.breaker import CircuitBreaker
    cb = CircuitBreaker(event_log, fail_threshold=2)
    assert not cb.record_failure("x")
    assert cb.record_failure("x")
    assert cb.is_tripped("x")


@pytest.mark.asyncio
async def test_coordinated_brake(event_log):
    from campaign.resilience.breaker import CircuitBreaker
    cb = CircuitBreaker(event_log)
    await cb.broadcast_brake("execution", "cascade risk")
    assert await cb.is_braking("execution")
    await cb.release_brake("execution")
    assert not await cb.is_braking("execution")


@pytest.mark.asyncio
async def test_checkpoint_snapshot_rollback(event_log):
    from campaign.resilience.checkpoint import Checkpointer

    await event_log.append("task.assigned", "a1", {"task_id": "t1"})
    await event_log.append("task.done", "a1", {"task_id": "t1"})

    cp = Checkpointer(event_log)
    seq = await cp.snapshot("pre_t2")
    assert seq == 2

    await event_log.append("task.assigned", "a1", {"task_id": "t2"})
    await event_log.append("task.frozen", "a1", {"task_id": "t2"})

    state = await cp.rollback("pre_t2")
    assert state.tasks_done == ["t1"]
    assert state.tasks_frozen == []


# ═══════════════════════════════════════════════════════════
# M5: 动员减员 + 预备队
# ═══════════════════════════════════════════════════════════

def test_capacity_ledger_assess():
    from campaign.resilience.mobilization import CapacityLedger, AttritionLevel

    ledger = CapacityLedger()
    # 空 active → NONE
    assert ledger.assess() == AttritionLevel.NONE

    # 添加 5 个 active
    for i in range(5):
        ledger.register_active(AgentSpec(id=f"a{i}", role="executor", model_tier="coder", skills=[]))

    # 全部健康 → NONE
    assert ledger.assess() == AttritionLevel.NONE

    # 1/5 不健康 → LIGHT
    ledger.mark_unhealthy("a0", 0.1)
    assert ledger.assess() == AttritionLevel.LIGHT

    # 2/5 不健康 → MEDIUM (ratio = 0.4)
    ledger.mark_unhealthy("a1", 0.1)
    assert ledger.assess() == AttritionLevel.MEDIUM

    # 4/5 不健康 → SEVERE (ratio = 0.8)
    ledger.mark_unhealthy("a2", 0.1)
    ledger.mark_unhealthy("a3", 0.1)
    assert ledger.assess() == AttritionLevel.SEVERE


@pytest.mark.asyncio
async def test_mobilizer_light_response(event_log):
    from campaign.resilience.mobilization import CapacityLedger, Mobilizer, AttritionLevel

    ledger = CapacityLedger()
    ledger.register_active(AgentSpec(id="a0", role="executor", model_tier="coder", skills=[]))
    ledger.register_reserve(AgentSpec(id="r0", role="reserve", model_tier="small", skills=["coding"]))

    m = Mobilizer(event_log, ledger)
    resp = await m.respond(AttritionLevel.LIGHT)
    assert resp["level"] == "light"
    assert len(resp["actions"]) > 0


@pytest.mark.asyncio
async def test_mobilizer_severe_escalation(event_log):
    from campaign.resilience.mobilization import CapacityLedger, Mobilizer, AttritionLevel

    ledger = CapacityLedger()
    ledger.register_reserve(AgentSpec(id="r0", role="reserve", model_tier="small", skills=["coding"]))

    m = Mobilizer(event_log, ledger)
    resp = await m.respond(AttritionLevel.SEVERE)
    assert any("human-in-the-loop" in a for a in resp["actions"])


@pytest.mark.asyncio
async def test_reserve_refuses_hard_task(event_log):
    from campaign.roles.reserve import Reserve

    spec = AgentSpec(id="r0", role="reserve", model_tier="small", skills=["coding"])
    reserve = Reserve(spec, event_log)

    task = Task(id="t1", goal="hard problem", difficulty="hard", required_skills=["coding"])
    result = await reserve.handle(task)
    assert result["accepted"] is False


@pytest.mark.asyncio
async def test_reserve_accepts_simple(event_log):
    from campaign.roles.reserve import Reserve

    spec = AgentSpec(id="r0", role="reserve", model_tier="small", skills=["coding"])
    reserve = Reserve(spec, event_log)

    task = Task(id="t1", goal="simple task", difficulty="simple", required_skills=["coding"])
    result = await reserve.handle(task)
    assert result["accepted"] is True


@pytest.mark.asyncio
async def test_reserve_work_stealing(event_log):
    from campaign.roles.reserve import Reserve

    spec = AgentSpec(id="r0", role="reserve", model_tier="small", skills=["coding"])
    reserve = Reserve(spec, event_log)

    frozen = [
        Task(id="t_hard", goal="hard", difficulty="hard", required_skills=["coding"]),
        Task(id="t_simple", goal="simple", difficulty="simple", required_skills=["coding"]),
    ]
    stolen = await reserve.steal_work(frozen)
    assert stolen is not None
    assert stolen.id == "t_simple"  # 只抢 simple


# ═══════════════════════════════════════════════════════════
# M6: 治理层（督军）
# ═══════════════════════════════════════════════════════════

def test_budget_rule():
    from campaign.governance.policy import BudgetRule, Action
    rule = BudgetRule()

    # 初始花费在预算内
    ctx: dict = {"cumulative_spend": 0.0}
    assert rule.check(Action(actor="a1", kind="spend", payload={}, cost=30), ctx) is None
    # 超预算（30+80=110 > 100 default）
    ctx["cumulative_spend"] = 80.0
    violation = rule.check(Action(actor="a1", kind="spend", payload={}, cost=30), ctx)
    assert violation is not None
    assert "exceeded" in violation


def test_authority_rule_blocks_escalation():
    from campaign.governance.policy import AuthorityRule, Action
    rule = AuthorityRule()

    # executor 试图 dispatch → 越权
    violation = rule.check(
        Action(actor="exec-1", kind="dispatch", payload={}),
        {"actor_role": "executor"},
    )
    assert violation is not None
    assert "privilege escalation" in violation

    # executor 做 tool_call → 正常
    assert rule.check(
        Action(actor="exec-1", kind="tool_call", payload={}),
        {"actor_role": "executor"},
    ) is None


def test_data_egress_rule():
    from campaign.governance.policy import DataEgressRule, Action
    rule = DataEgressRule()

    # 非 sensitive → 放行
    assert rule.check(
        Action(actor="a1", kind="data_egress", payload={}, sensitive=False),
        {"privacy_strict": True, "deploy_mode": "hybrid", "is_local": False},
    ) is None

    # sensitive + non-local → 拦截
    violation = rule.check(
        Action(actor="a1", kind="data_egress", payload={}, sensitive=True),
        {"privacy_strict": True, "deploy_mode": "hybrid", "is_local": False},
    )
    assert violation is not None

    # sensitive + local → 放行
    assert rule.check(
        Action(actor="a1", kind="data_egress", payload={}, sensitive=True),
        {"privacy_strict": True, "deploy_mode": "hybrid", "is_local": True},
    ) is None


@pytest.mark.asyncio
async def test_governor_blocks_violation(event_log):
    from campaign.governance.governor import Governor
    from campaign.governance.policy import BudgetRule, AuthorityRule, Action

    governor = Governor(event_log, rules=[BudgetRule(), AuthorityRule()])
    # executor dispatch → 越权
    ok = await governor.vet(
        Action(actor="exec-1", kind="dispatch", payload={}),
        {"actor_role": "executor"},
    )
    assert not ok

    # 确认事件已写入
    events = await event_log.replay()
    violations = [e for e in events if e.type == "governance.violation"]
    assert len(violations) == 1


@pytest.mark.asyncio
async def test_governor_allows_valid_action(event_log):
    from campaign.governance.governor import Governor
    from campaign.governance.policy import AuthorityRule, Action

    governor = Governor(event_log, rules=[AuthorityRule()])
    ok = await governor.vet(
        Action(actor="exec-1", kind="tool_call", payload={}),
        {"actor_role": "executor"},
    )
    assert ok


# ═══════════════════════════════════════════════════════════
# M7: Tracer + Config
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_tracer_span():
    from campaign.observability.tracer import InMemoryTracer
    tracer = InMemoryTracer()

    async with tracer.span("test_op", "agent-1"):
        pass  # success

    assert len(tracer.traces) == 1
    assert tracer.traces[0]["status"] == "ok"
    assert "duration_ms" in tracer.traces[0]


@pytest.mark.asyncio
async def test_tracer_span_error():
    from campaign.observability.tracer import InMemoryTracer
    tracer = InMemoryTracer()

    with pytest.raises(ValueError):
        async with tracer.span("test_op", "agent-1"):
            raise ValueError("boom")

    assert tracer.traces[0]["status"] == "error"


def test_tracer_metering():
    from campaign.observability.tracer import InMemoryTracer
    tracer = InMemoryTracer()

    tracer.meter("agent-1", 100, 0.05, 250.0)
    tracer.meter("agent-1", 200, 0.10, 300.0)

    assert tracer.recent_cost("agent-1") == pytest.approx(0.15)
    assert tracer.recent_latency("agent-1") == pytest.approx(275.0)


def test_config_tier_for_sensitive_hybrid():
    from campaign.app.config import Config
    from campaign.llm.client import TierConfig

    cfg = Config(
        deploy_mode="hybrid",
        tiers={
            "value": TierConfig(model="cloud-model", base_url="https://api.openai.com", local=False),
            "small": TierConfig(model="local-model", base_url="http://localhost:11434", local=True),
        },
    )
    tier = cfg.tier_for_sensitive()
    assert tier == "small"  # local tier


def test_config_tier_for_sensitive_cloud_strict():
    from campaign.app.config import Config
    from campaign.llm.client import TierConfig

    cfg = Config(
        deploy_mode="cloud",
        privacy_strict=True,
        tiers={
            "coder": TierConfig(model="cloud-model", base_url="https://api.openai.com", local=False),
        },
    )
    with pytest.raises(ValueError, match="Cannot process sensitive data"):
        cfg.tier_for_sensitive()


# ═══════════════════════════════════════════════════════════
# M8: Eval + Chaos
# ═══════════════════════════════════════════════════════════

def test_chaos_drill_kill_random():
    from campaign.eval.chaos import ChaosDrill

    agents = [
        AgentSpec(id="a1", role="executor", model_tier="coder", skills=[]),
        AgentSpec(id="a2", role="executor", model_tier="coder", skills=[]),
    ]
    drill = ChaosDrill(seed=42)
    victim = drill.kill_random(agents)
    assert drill.is_victim(victim.id)


def test_chaos_drill_kill_by_role():
    from campaign.eval.chaos import ChaosDrill

    agents = [
        AgentSpec(id="a1", role="executor", model_tier="coder", skills=[]),
        AgentSpec(id="a2", role="reviewer", model_tier="flagship", skills=[]),
        AgentSpec(id="a3", role="executor", model_tier="coder", skills=[]),
    ]
    drill = ChaosDrill(seed=42)
    victims = drill.kill_by_role(agents, "executor")
    assert len(victims) == 2
    assert all(drill.is_victim(v.id) for v in victims)


def test_chaos_drill_error_injection():
    from campaign.eval.chaos import ChaosDrill

    drill = ChaosDrill(seed=42)
    drill.inject_error("a1", 0.5)
    # 多次采样，应有接近一半为 True
    failures = sum(1 for _ in range(100) if drill.should_fail("a1"))
    assert 30 < failures < 70  # 统计范围，seed=42 保证可重复


def test_chaos_drill_latency():
    from campaign.eval.chaos import ChaosDrill

    drill = ChaosDrill()
    drill.inject_latency("a1", 500.0)
    assert drill.get_latency("a1") == 500.0


@pytest.mark.asyncio
async def test_eval_harness_gate_above_threshold(event_log):
    from campaign.app.config import Config
    from campaign.app.runtime import Runtime
    from campaign.eval.harness import EvalCase, EvalHarness
    from campaign.roles.executor import Executor

    exec_spec = AgentSpec(id="exec-1", role="executor", model_tier="coder", skills=["coding"])
    config = Config()
    runtime = Runtime(event_log, config)
    runtime.register_agent(Executor(exec_spec, event_log))

    harness = EvalHarness(threshold=0.5)
    cases = [
        EvalCase(
            id="case_1",
            order={"objective": "test", "tasks": [
                {"id": "t1", "goal": "do", "difficulty": "simple", "required_skills": ["coding"]}
            ]},
            expected={"tasks_done": 1, "min_score": 0.5},
        ),
    ]
    passed = await harness.gate(runtime, cases)
    assert passed is True  # stub executor always succeeds


@pytest.mark.asyncio
async def test_eval_harness_export_trainset(event_log):
    from campaign.eval.harness import EvalHarness

    await event_log.append("task.assigned", "a1", {"task_id": "t1"})
    await event_log.append("task.done", "a1", {"task_id": "t1"})
    await event_log.append("task.assigned", "a1", {"task_id": "t2"})
    await event_log.append("task.failed", "a1", {"task_id": "t2"})

    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)

    harness = EvalHarness()
    count = await harness.export_trainset_async(event_log, path)
    assert count == 2

    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    assert len(lines) == 2
    os.unlink(path)
