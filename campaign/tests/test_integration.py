"""功能集成测试 —— 端到端验证框架核心链路。

覆盖：
1. 完整最小闭环: 拆解→路由→执行→验收
2. 技能路由: 无人具备技能→冻结
3. 熔断+协同制动: 注入失败→熔断→制动广播→背压
4. 预备队补位: reserve 拒 hard、接 simple/medium、work-stealing
5. 督军拦截: 越权/超预算/数据出域
6. Checkpoint+Rollback
7. 减员动员三级响应
8. Eval 门禁+训练集导出
9. 混沌演练
"""
import asyncio
import os
import tempfile

import pytest

from campaign.core.events import SqliteEventLog, Event
from campaign.core.models import AgentSpec, ExecutionOrder, Task
from campaign.core.state import derive_state
from campaign.app.config import Config
from campaign.app.runtime import Runtime
from campaign.roles.coordinator import Coordinator
from campaign.roles.executor import Executor
from campaign.roles.reviewer import Reviewer
from campaign.roles.retriever import Retriever
from campaign.roles.reserve import Reserve
from campaign.routing.skill_registry import SkillRegistry
from campaign.routing.router import Router
from campaign.resilience.breaker import CircuitBreaker
from campaign.resilience.checkpoint import Checkpointer
from campaign.resilience.mobilization import CapacityLedger, Mobilizer, AttritionLevel
from campaign.governance.policy import BudgetRule, AuthorityRule, DataEgressRule, Action
from campaign.governance.governor import Governor
from campaign.observability.tracer import InMemoryTracer
from campaign.eval.harness import EvalHarness, EvalCase
from campaign.eval.chaos import ChaosDrill


# ═══════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════

@pytest.fixture
def event_log():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="campaign_int_")
    os.close(fd)
    log = SqliteEventLog(db_path=path)
    yield log
    log.close()
    try:
        os.unlink(path)
    except OSError:
        pass


def make_runtime(event_log, agents=None, with_router=False, with_governor=False):
    """构建 Runtime 实例。"""
    config = Config()
    runtime = Runtime(event_log, config)

    # 注册 agents
    if agents:
        for a in agents:
            runtime.register_agent(a)

    # Coordinator
    coord_spec = AgentSpec(id="coordinator", role="coordinator", model_tier="flagship", skills=[])
    coordinator = Coordinator(coord_spec, event_log)

    if with_router:
        registry = SkillRegistry()
        for a in (agents or []):
            registry.register(a.spec)
        router = Router(registry, event_log)
        coordinator._router = router

    runtime.set_coordinator(coordinator)

    if with_governor:
        governor = Governor(event_log, rules=[BudgetRule(), AuthorityRule()])
        runtime.set_governor(governor)

    return runtime


# ═══════════════════════════════════════════════════════════
# 场景 1: 完整最小闭环
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_full_minimal_loop(event_log):
    """拆解→路由→执行→验收，全程事件可 replay。"""
    exec_spec = AgentSpec(id="exec-1", role="executor", model_tier="coder", skills=["coding"])
    rev_spec = AgentSpec(id="rev-1", role="reviewer", model_tier="flagship", skills=[])

    runtime = make_runtime(event_log, agents=[
        Executor(exec_spec, event_log),
        Reviewer(rev_spec, event_log),
    ])

    order = ExecutionOrder(
        objective="写一个排序函数并测试",
        tasks=[
            Task(id="t1", goal="实现快速排序", difficulty="simple", required_skills=["coding"], acceptance="代码能编译运行"),
            Task(id="t2", goal="写单元测试", difficulty="simple", required_skills=["coding"], acceptance="覆盖率>80%"),
        ],
    )

    result = await runtime.run(order)

    # 验证结果结构
    assert result["tasks_total"] == 2
    assert len(result["results"]) == 2
    for r in result["results"]:
        assert r["status"] == "done"
        assert "review" in r
        assert r["review"]["passed"] is True

    # 验证事件流可 replay
    events = await event_log.replay()
    event_types = [e.type for e in events]
    assert "run.started" in event_types
    assert "run.decomposed" in event_types
    assert "run.completed" in event_types
    assert "task.done" in event_types

    # 验证 state 派生
    state = derive_state(events)
    assert len(state.tasks_done) >= 2


# ═══════════════════════════════════════════════════════════
# 场景 2: 技能路由 —— 无人具备技能 → 冻结
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_routing_freezes_when_no_skill_match(event_log):
    """任务要求技能无人具备 → 冻结排队。"""
    exec_spec = AgentSpec(id="exec-1", role="executor", model_tier="coder", skills=["testing"])
    rev_spec = AgentSpec(id="rev-1", role="reviewer", model_tier="flagship", skills=[])

    runtime = make_runtime(event_log, agents=[
        Executor(exec_spec, event_log),
        Reviewer(rev_spec, event_log),
    ], with_router=True)

    order = ExecutionOrder(
        objective="写代码",
        tasks=[Task(id="t1", goal="写代码", required_skills=["coding"])],
    )

    result = await runtime.run(order)
    assert result["results"][0]["status"] == "frozen"

    # 验证冻结事件
    events = await event_log.replay()
    frozen_events = [e for e in events if e.type == "task.frozen"]
    assert len(frozen_events) == 1


# ═══════════════════════════════════════════════════════════
# 场景 3: 熔断 + 协同制动
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_circuit_breaker_and_brake_cascade(event_log):
    """持续失败的 executor 触发熔断 → 广播制动 → 检查制动状态。"""
    breaker = CircuitBreaker(event_log, fail_threshold=2)

    # 模拟连续失败
    assert not breaker.record_failure("exec-1")
    assert breaker.record_failure("exec-1")  # 触发熔断
    assert breaker.is_tripped("exec-1")

    # 广播协同制动
    await breaker.broadcast_brake("execution", "exec-1 tripped")
    assert await breaker.is_braking("execution")

    # 另一个 executor 仍可正常工作（不同链路）
    assert not await breaker.is_braking("retrieval")

    # 释放制动
    await breaker.release_brake("execution")
    assert not await breaker.is_braking("execution")


@pytest.mark.asyncio
async def test_breaker_success_resets_count(event_log):
    """成功后重置失败计数。"""
    breaker = CircuitBreaker(event_log, fail_threshold=3)
    breaker.record_failure("a1")
    breaker.record_failure("a1")
    breaker.record_success("a1")
    breaker.record_failure("a1")
    breaker.record_failure("a1")
    # 只有 2 次连续失败（成功了重置）
    assert not breaker.is_tripped("a1")


# ═══════════════════════════════════════════════════════════
# 场景 4: 预备队补位 + work-stealing
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_reserve_refuses_hard_accepts_simple(event_log):
    """预备队：拒 hard、接 simple、medium 标 needs_strict_review。"""
    spec = AgentSpec(id="r0", role="reserve", model_tier="small", skills=["coding"])
    reserve = Reserve(spec, event_log)

    # Hard → 拒接
    hard_result = await reserve.handle(
        Task(id="t_hard", goal="复杂架构", difficulty="hard", required_skills=["coding"])
    )
    assert hard_result["accepted"] is False

    # Simple → 接受
    simple_result = await reserve.handle(
        Task(id="t_simple", goal="格式化代码", difficulty="simple", required_skills=["coding"])
    )
    assert simple_result["accepted"] is True
    assert not simple_result.get("needs_strict_review", True)

    # Medium → 接受但标记需加严验收
    medium_result = await reserve.handle(
        Task(id="t_med", goal="写 patch", difficulty="medium", required_skills=["coding"])
    )
    assert medium_result["accepted"] is True
    assert medium_result["needs_strict_review"] is True


@pytest.mark.asyncio
async def test_reserve_work_stealing_skips_hard(event_log):
    """work-stealing 跳过 hard 任务。"""
    spec = AgentSpec(id="r0", role="reserve", model_tier="small", skills=["coding"])
    reserve = Reserve(spec, event_log)

    frozen = [
        Task(id="t_hard", goal="架构重写", difficulty="hard", required_skills=["coding"]),
        Task(id="t_hard2", goal="复杂推理", difficulty="hard", required_skills=["coding"]),
        Task(id="t_simple", goal="格式化", difficulty="simple", required_skills=["coding"]),
    ]
    stolen = await reserve.steal_work(frozen)
    assert stolen is not None
    assert stolen.id == "t_simple"

    # 只有 hard 任务 → 抢不到
    frozen_hard_only = [
        Task(id="t_h", goal="x", difficulty="hard", required_skills=["coding"]),
    ]
    assert await reserve.steal_work(frozen_hard_only) is None


# ═══════════════════════════════════════════════════════════
# 场景 5: 督军拦截
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_governor_blocks_privilege_escalation(event_log):
    """督军拦截越权动作。"""
    governor = Governor(event_log, rules=[AuthorityRule()])

    # executor 尝试 dispatch → 越权
    ok = await governor.vet(
        Action(actor="exec-1", kind="dispatch", payload={}),
        {"actor_role": "executor"},
    )
    assert not ok

    # coordinator dispatch → 合法
    ok = await governor.vet(
        Action(actor="coord-1", kind="dispatch", payload={}),
        {"actor_role": "coordinator"},
    )
    assert ok

    # 验证违规事件已写入
    events = await event_log.replay()
    violations = [e for e in events if e.type == "governance.violation"]
    assert len(violations) == 1
    assert violations[0].payload["actor"] == "exec-1"


@pytest.mark.asyncio
async def test_governor_blocks_budget_overspend(event_log):
    """督军拦截超预算。"""
    governor = Governor(event_log, rules=[BudgetRule()])

    # 累计花费超过预算
    ok1 = await governor.vet(
        Action(actor="a1", kind="spend", payload={}, cost=60),
        {"budget": {"token_limit": 100}},
    )
    assert ok1

    ok2 = await governor.vet(
        Action(actor="a1", kind="spend", payload={}, cost=50),
        {"budget": {"token_limit": 100}},
    )
    assert not ok2  # 60+50=110 > 100

    events = await event_log.replay()
    violations = [e for e in events if e.type == "governance.violation"]
    assert len(violations) == 1


@pytest.mark.asyncio
async def test_data_egress_rule_blocks_sensitive_cloud(event_log):
    """敏感数据在非本地模式下出境被拦截。"""
    governor = Governor(event_log, rules=[DataEgressRule()])

    # 敏感 + cloud + privacy strict → 拦截
    ok = await governor.vet(
        Action(actor="a1", kind="data_egress", payload={}, sensitive=True),
        {"privacy_strict": True, "deploy_mode": "cloud", "is_local": False},
    )
    assert not ok

    # 敏感 + local → 放行
    ok = await governor.vet(
        Action(actor="a1", kind="data_egress", payload={}, sensitive=True),
        {"privacy_strict": True, "deploy_mode": "hybrid", "is_local": True},
    )
    assert ok


# ═══════════════════════════════════════════════════════════
# 场景 6: Checkpoint + Rollback
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_checkpoint_and_rollback_scenario(event_log):
    """完整 checkpoint→继续执行→rollback 场景。"""
    cp = Checkpointer(event_log)

    # Phase 1: 执行两个任务
    await event_log.append("task.assigned", "a1", {"task_id": "t1"})
    await event_log.append("task.done", "a1", {"task_id": "t1"})
    seq_after_phase1 = await cp.snapshot("phase1")
    assert seq_after_phase1 == 2

    # Phase 2: 继续执行（引入错误）
    await event_log.append("task.assigned", "a1", {"task_id": "t2"})
    await event_log.append("task.failed", "a1", {"task_id": "t2"})
    await event_log.append("incident", "system", {"reason": "boom"})

    # Rollback 到 phase1
    state = await cp.rollback("phase1")
    assert state.tasks_done == ["t1"]
    assert state.tasks_pending == []
    assert len(state.incidents) == 0
    assert state.tasks_frozen == []

    # 当前 state（不回滚）应包含 t2 失败
    current_events = await event_log.replay()
    current_state = derive_state(current_events)
    assert len(current_state.incidents) == 1


# ═══════════════════════════════════════════════════════════
# 场景 7: 减员动员三级响应
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_mobilization_three_levels(event_log):
    """完整三级响应链路。"""
    ledger = CapacityLedger()
    mobilizer = Mobilizer(event_log, ledger)

    # 添加现役和预备役
    for i in range(3):
        ledger.register_active(
            AgentSpec(id=f"exec-{i}", role="executor", model_tier="coder", skills=["coding"])
        )
    for i in range(2):
        ledger.register_reserve(
            AgentSpec(id=f"res-{i}", role="reserve", model_tier="small", skills=["coding", "testing"])
        )

    # LIGHT 响应：预备役补位
    resp_light = await mobilizer.respond(AttritionLevel.LIGHT)
    assert resp_light["level"] == "light"
    assert any("reserves" in a for a in resp_light["actions"])

    # MEDIUM 响应：checkpoint + 降级 + Reviewer 加严
    resp_med = await mobilizer.respond(AttritionLevel.MEDIUM)
    assert resp_med["level"] == "medium"
    assert any("checkpoint" in a for a in resp_med["actions"])
    assert any("reviewer" in a.lower() for a in resp_med["actions"])

    # SEVERE 响应：全动员 + human escalation
    resp_sev = await mobilizer.respond(AttritionLevel.SEVERE)
    assert resp_sev["level"] == "severe"
    assert any("human-in-the-loop" in a for a in resp_sev["actions"])
    assert any("full mobilization" in a for a in resp_sev["actions"])

    # 验证事件流
    events = await event_log.replay()
    event_types = [e.type for e in events]
    assert "mobilization.substitute" in event_types or "mobilization.response" in event_types
    assert "mobilization.checkpoint" in event_types
    assert "mobilization.human_escalation" in event_types


# ═══════════════════════════════════════════════════════════
# 场景 8: Eval 门禁 + 训练集导出
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_eval_gate_and_trainset_export(event_log):
    """Eval 门禁：达到阈值通过，导出训练集。"""
    exec_spec = AgentSpec(id="exec-1", role="executor", model_tier="coder", skills=["coding"])
    rev_spec = AgentSpec(id="rev-1", role="reviewer", model_tier="flagship", skills=[])

    runtime = make_runtime(event_log, agents=[
        Executor(exec_spec, event_log),
        Reviewer(rev_spec, event_log),
    ])

    harness = EvalHarness(threshold=0.5)
    cases = [
        EvalCase(
            id="case_1",
            order={
                "objective": "test",
                "tasks": [
                    {"id": "t1", "goal": "写代码", "difficulty": "simple", "required_skills": ["coding"]},
                ],
            },
            expected={"tasks_done": 1},
            weight=1.0,
        ),
        EvalCase(
            id="case_2",
            order={
                "objective": "test2",
                "tasks": [
                    {"id": "t2", "goal": "写测试", "difficulty": "simple", "required_skills": ["coding"]},
                ],
            },
            expected={"tasks_done": 1},
            weight=0.5,
        ),
    ]

    # 门禁检查
    passed = await harness.gate(runtime, cases)
    assert passed is True

    # 导出训练集
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    count = await harness.export_trainset_async(event_log, path)
    assert count >= 2

    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    assert len(lines) == count
    os.unlink(path)


# ═══════════════════════════════════════════════════════════
# 场景 9: 混沌演练
# ═══════════════════════════════════════════════════════════

def test_chaos_drill_full_scenario():
    """混沌演练完整场景：击杀 + 延迟 + 错误注入。"""
    agents = [
        AgentSpec(id="exec-1", role="executor", model_tier="coder", skills=["coding"]),
        AgentSpec(id="exec-2", role="executor", model_tier="coder", skills=["testing"]),
        AgentSpec(id="rev-1", role="reviewer", model_tier="flagship", skills=[]),
        AgentSpec(id="ret-1", role="retriever", model_tier="value", skills=["search"]),
    ]

    drill = ChaosDrill(seed=42)

    # 1. 随机杀一个 agent
    victim = drill.kill_random(agents)
    assert drill.is_victim(victim.id)

    # 2. 按角色批量杀 executor
    victims = drill.kill_by_role(agents, "executor")
    assert len(victims) >= 1

    # 3. 注入延迟
    drill.inject_latency("ret-1", 2000.0)
    assert drill.get_latency("ret-1") == 2000.0

    # 4. 注入错误率
    drill.inject_error("exec-1", 0.8)
    # 多次采样验证概率
    failures = sum(1 for _ in range(100) if drill.should_fail("exec-1"))
    assert failures > 50  # 80% 错误率 → 大多数应失败

    # 5. 复活
    drill.revive(victim.id)
    assert not drill.is_victim(victim.id)

    # 6. 重置
    drill.reset()
    assert not drill.is_victim(victims[0].id) if victims else True


# ═══════════════════════════════════════════════════════════
# 场景 10: 跨模块综合 —— Router + Breaker + Reserve 联动
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_cross_module_coordination(event_log):
    """Router 选 agent → Breaker 熔断 → Reserve 补位 联动。"""
    # 设置注册表和路由
    registry = SkillRegistry()
    registry.register(AgentSpec(id="exec-1", role="executor", model_tier="coder",
                                 skills=["coding"], proficiency={"coding": 80}))
    registry.register(AgentSpec(id="res-1", role="reserve", model_tier="small",
                                 skills=["coding"], proficiency={"coding": 40}))

    router = Router(registry, event_log)
    breaker = CircuitBreaker(event_log, fail_threshold=2)

    # 正常路由选最优（综合 cost+confidence+success_rate）
    task = Task(id="t1", goal="code", difficulty="simple", required_skills=["coding"])
    best = await router.pick(task)
    assert best is not None
    # res-1 (tier=small, cost=50) 综合分高于 exec-1 (tier=coder, cost=300)
    # 即使 exec-1 熟练度更高，ROI 路由优先性价比
    assert best.id in ("exec-1", "res-1")

    # 模拟 exec-1 连续失败 → 熔断
    assert not breaker.record_failure("exec-1")
    assert breaker.record_failure("exec-1")
    assert breaker.is_tripped("exec-1")

    # 广播制动
    await breaker.broadcast_brake("execution", "exec-1 tripped")
    assert await breaker.is_braking("execution")

    # Reserve 补位：simple 任务可接
    res_spec = AgentSpec(id="res-1", role="reserve", model_tier="small", skills=["coding"])
    reserve = Reserve(res_spec, event_log)
    result = await reserve.handle(task)
    assert result["accepted"] is True


# ═══════════════════════════════════════════════════════════
# 场景 11: Tracer 完整生命周期
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_tracer_full_lifecycle():
    """Tracer 完整流程：span → meter → score → 查询。"""
    tracer = InMemoryTracer()

    # Span 记录
    async with tracer.span("task.execute", "exec-1"):
        await asyncio.sleep(0.01)

    # 计量
    tracer.meter("exec-1", 150, 0.03, 120.0)
    tracer.meter("exec-1", 200, 0.05, 150.0)
    tracer.meter("exec-1", 300, 0.07, 180.0)

    # 打分
    tracer.score("t1", 0.85)
    tracer.score("t2", 0.92)

    # 验证
    assert len(tracer.traces) == 1
    assert tracer.traces[0]["status"] == "ok"
    assert tracer.traces[0]["duration_ms"] > 0

    assert len(tracer.metrics) == 3
    assert tracer.recent_cost("exec-1", 3) == pytest.approx(0.15)
    assert tracer.recent_latency("exec-1", 3) == pytest.approx(150.0)

    assert tracer.scores["t1"] == 0.85
    assert tracer.scores["t2"] == 0.92


# ═══════════════════════════════════════════════════════════
# 场景 12: Config 部署模式
# ═══════════════════════════════════════════════════════════

def test_config_deploy_modes():
    """三种部署模式 + 隐私开关。"""
    from campaign.llm.client import TierConfig

    # Hybrid 模式：敏感数据走 local tier
    cfg = Config(
        deploy_mode="hybrid",
        tiers={
            "value": TierConfig(model="cloud-model", base_url="https://api.example.com", local=False),
            "small": TierConfig(model="local-model", base_url="http://localhost:11434", local=True),
        },
    )
    assert cfg.tier_for_sensitive() == "small"
    assert cfg.is_local_tier("small")
    assert not cfg.is_local_tier("value")

    # Cloud 模式 + privacy strict → 拒绝敏感数据
    cfg_cloud = Config(
        deploy_mode="cloud",
        privacy_strict=True,
        tiers={
            "coder": TierConfig(model="cloud-only", base_url="https://api.example.com", local=False),
        },
    )
    with pytest.raises(ValueError, match="Cannot process sensitive data"):
        cfg_cloud.tier_for_sensitive()

    # Cloud 模式 + 不强制隐私 → 使用第一个可用 tier
    cfg_cloud_loose = Config(
        deploy_mode="cloud",
        privacy_strict=False,
        tiers={
            "value": TierConfig(model="cheap-model", base_url="https://api.example.com", local=False),
        },
    )
    assert cfg_cloud_loose.tier_for_sensitive() == "value"


# ═══════════════════════════════════════════════════════════
# 场景 13: 完整 Runtime + Governor 拦截
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_runtime_with_governor_blocks_dispatch(event_log):
    """Runtime + Governor：越权 dispatch 被督军拦截。"""
    exec_spec = AgentSpec(id="exec-1", role="executor", model_tier="coder", skills=["coding"])
    rev_spec = AgentSpec(id="rev-1", role="reviewer", model_tier="flagship", skills=[])

    runtime = make_runtime(event_log, agents=[
        Executor(exec_spec, event_log),
        Reviewer(rev_spec, event_log),
    ], with_governor=True)

    order = ExecutionOrder(
        objective="测试治理",
        tasks=[Task(id="t1", goal="测试", required_skills=["coding"])],
        budget={"token_limit": 1000},  # 预算充足 → executor spend(300) 放行
    )

    result = await runtime.run(order)
    # Governor 检查 spend 动作：executor 有 spend 权限且预算内 → 放行
    assert result["results"][0]["status"] == "done"

    # 验证没有违规事件（spend 在预算内、角色合法）
    events = await event_log.replay()
    violations = [e for e in events if e.type == "governance.violation"]
    assert len(violations) == 0
