"""边界处理测试（加固 1-10）。

覆盖：decompose 校验错容错、State.failed、rollback 真分叉、EventLog 丢弃计数、
ExecutionOrder 重复 id、mock 耗尽、no-reviewer fail-closed、strictness 钳制、
artifact 非 dict、Router 同分确定性。
"""
import os
import tempfile

import pytest

from campaign.core.events import SqliteEventLog
from campaign.core.models import AgentSpec, ExecutionOrder, Task
from campaign.core.state import derive_state


@pytest.fixture
def log():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="bnd_")
    os.close(fd)
    lg = SqliteEventLog(db_path=path)
    yield lg
    lg.close()
    try:
        os.unlink(path)
    except OSError:
        pass


def _ev(seq, typ, payload):
    from datetime import datetime, timezone
    from campaign.core.events import Event
    return Event(seq=seq, ts=datetime.now(timezone.utc), type=typ, actor="c", payload=payload)


# 7. ExecutionOrder 重复 task id → 拒绝
def test_duplicate_task_ids_rejected():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ExecutionOrder(objective="x", tasks=[Task(id="t1", goal="a"), Task(id="t1", goal="b")])


def test_unique_task_ids_ok():
    o = ExecutionOrder(objective="x", tasks=[Task(id="t1", goal="a"), Task(id="t2", goal="b")])
    assert len(o.tasks) == 2


# 2. State.failed：失败任务不再凭空消失
def test_state_tracks_failed():
    state = derive_state([
        _ev(1, "task.assigned", {"task_id": "t1"}),
        _ev(2, "task.failed", {"task_id": "t1"}),
    ])
    assert state.tasks_failed == ["t1"]
    assert "t1" not in state.tasks_pending
    assert "t1" not in state.tasks_done


def test_state_failed_then_reassigned():
    state = derive_state([
        _ev(1, "task.assigned", {"task_id": "t1"}),
        _ev(2, "task.failed", {"task_id": "t1"}),
        _ev(3, "task.assigned", {"task_id": "t1"}),
        _ev(4, "task.done", {"task_id": "t1"}),
    ])
    assert state.tasks_done == ["t1"]
    assert state.tasks_failed == []


# 3. rollback 真分叉（hard=True 物理截断）
@pytest.mark.asyncio
async def test_hard_rollback_truncates(log):
    from campaign.resilience.checkpoint import Checkpointer
    await log.append("task.assigned", "c", {"task_id": "t1"})
    cp = Checkpointer(log)
    seq = await cp.snapshot("p")
    await log.append("task.done", "c", {"task_id": "t1"})
    assert len(await log.replay()) == 2
    state = await cp.rollback("p", hard=True)
    assert len(await log.replay()) == 1           # 物理截断生效
    assert "t1" not in state.tasks_done


@pytest.mark.asyncio
async def test_soft_rollback_keeps_log(log):
    from campaign.resilience.checkpoint import Checkpointer
    await log.append("task.assigned", "c", {"task_id": "t1"})
    cp = Checkpointer(log)
    await cp.snapshot("p")
    await log.append("task.done", "c", {"task_id": "t1"})
    await cp.rollback("p", hard=False)
    assert len(await log.replay()) == 2           # 软回滚不删日志


# 4. EventLog 丢弃计数存在且初始为 0
@pytest.mark.asyncio
async def test_dropped_events_counter(log):
    assert log.dropped_events == 0
    await log.append("x", "c", {})
    assert log.dropped_events == 0                 # 无慢订阅者，不丢


# 6. mock 耗尽 → 报错，不偷打真实 API
@pytest.mark.asyncio
async def test_mock_exhaustion_raises():
    from campaign.llm.client import LLMClient, TierConfig, LLMError
    client = LLMClient(
        tiers={"coder": TierConfig(model="m", base_url="http://localhost")},
        mock_responses={"coder": [{"choices": [{"message": {"content": "ok"}}]}]},
    )
    await client.complete("coder", [{"role": "user", "content": "1"}])  # 用掉唯一一条
    with pytest.raises(LLMError, match="exhausted"):
        await client.complete("coder", [{"role": "user", "content": "2"}])  # 不 fallthrough
    await client.close()


# 1. decompose：坏 task dict 被跳过，不炸整个 run
@pytest.mark.asyncio
async def test_decompose_skips_invalid_task(log):
    from campaign.llm.client import LLMClient, TierConfig
    from campaign.roles.coordinator import Coordinator
    bad_then_good = '[{"id":"a","goal":"good","difficulty":"simple"},{"id":"b"}]'  # 第二条缺 goal
    llm = LLMClient(
        tiers={"flagship": TierConfig(model="m", base_url="http://localhost")},
        mock_responses={"flagship": [{"choices": [{"message": {"content": bad_then_good}}]}]},
    )
    coord = Coordinator(AgentSpec(id="coordinator", role="coordinator", model_tier="flagship"), log, llm=llm)
    tasks = await coord.decompose(ExecutionOrder(objective="x", tasks=[]))
    assert [t.id for t in tasks] == ["a"]          # 坏条跳过，好条保留
    await llm.close()


# 8/9. reviewer：strictness 越界 + artifact 非 dict 不崩
@pytest.mark.asyncio
async def test_reviewer_strictness_clamp_and_bad_artifact(log):
    from campaign.roles.reviewer import Reviewer
    spec = AgentSpec(id="rev", role="reviewer", model_tier="flagship", skills=["review"])
    rev = Reviewer(spec, log, strictness=5.0)       # 越界
    out = await rev.handle(Task(id="t1", goal="g", acceptance="ok"), artifact="not-a-dict")
    assert out["score"] >= 0.0                       # 不为负
    assert "passed" in out


# 5. no-reviewer fail-closed vs fail-open
@pytest.mark.asyncio
async def test_no_reviewer_fail_modes(log):
    from campaign.app.config import Config
    from campaign.app.runtime import Runtime
    from campaign.roles.executor import Executor

    def build():
        lg_fd, lg_path = tempfile.mkstemp(suffix=".db", prefix="nr_")
        os.close(lg_fd)
        lg = SqliteEventLog(db_path=lg_path)
        rt = Runtime(lg, Config())
        rt.register_agent(Executor(AgentSpec(id="e", role="executor", model_tier="value", skills=["code"]), lg))
        return rt, lg, lg_path

    order = ExecutionOrder(objective="x", tasks=[Task(id="t1", goal="g", required_skills=["code"])])

    rt, lg, p = build()
    res = await rt.run(order)                        # 默认 fail-open
    assert res["results"][0]["review"]["passed"] is True
    lg.close(); os.unlink(p)

    rt, lg, p = build()
    rt.set_require_reviewer(True)                    # fail-closed
    res = await rt.run(order)
    assert res["results"][0]["review"]["passed"] is False
    lg.close(); os.unlink(p)


# 10. Router 同分确定性：返回 agent_id 最小者，且可复现
@pytest.mark.asyncio
async def test_router_tie_deterministic(log):
    from campaign.routing.router import Router
    from campaign.routing.skill_registry import SkillRegistry
    reg = SkillRegistry()
    reg.register(AgentSpec(id="z-agent", role="executor", model_tier="coder", skills=["code"]))
    reg.register(AgentSpec(id="a-agent", role="executor", model_tier="coder", skills=["code"]))
    router = Router(reg, log)
    task = Task(id="t1", goal="g", required_skills=["code"])
    pick1 = await router.pick(task)
    pick2 = await router.pick(task)
    assert pick1.id == pick2.id == "a-agent"        # 同分取最小 id，可复现


# ── 结构性边界（#1-4 真实驱动） ──────────────────────────

def _exec(skills=("code",), tier="value", aid="e"):
    from campaign.roles.executor import Executor
    return Executor, AgentSpec(id=aid, role="executor", model_tier=tier, skills=list(skills))


# #1 run 隔离：多次 run 在同一 log 上互不混流
@pytest.mark.asyncio
async def test_run_isolation_by_run_id(log):
    from campaign.app.config import Config
    from campaign.app.runtime import Runtime
    from campaign.roles.executor import Executor
    rt = Runtime(log, Config())
    rt.register_agent(Executor(AgentSpec(id="e", role="executor", model_tier="value", skills=["code"]), log))

    r1 = await rt.run(ExecutionOrder(objective="o1", tasks=[Task(id="t1", goal="g", required_skills=["code"])]))
    r2 = await rt.run(ExecutionOrder(objective="o2", tasks=[Task(id="tA", goal="g", required_skills=["code"])]))

    s1 = await rt.state(r1["run_id"])
    s2 = await rt.state(r2["run_id"])
    assert s1.tasks_done == ["t1"]          # 各自只见自己的任务
    assert s2.tasks_done == ["tA"]
    all_state = derive_state(await log.replay())  # 不传 run_id → 全量
    assert {"t1", "tA"} <= set(all_state.tasks_done)


# #2 预算真触发：超预算 → 被督军拦
@pytest.mark.asyncio
async def test_budget_actually_enforced_in_run(log):
    from campaign.app.config import Config
    from campaign.app.runtime import Runtime
    from campaign.roles.executor import Executor
    from campaign.governance.governor import Governor
    from campaign.governance.policy import BudgetRule, AuthorityRule
    rt = Runtime(log, Config())
    rt.register_agent(Executor(AgentSpec(id="e", role="executor", model_tier="coder", skills=["code"]), log))
    rt.set_governor(Governor(log, rules=[BudgetRule(), AuthorityRule()]))

    # coder 估价 300 > 预算 50 → 拦截
    r = await rt.run(ExecutionOrder(
        objective="o", budget={"token_limit": 50},
        tasks=[Task(id="t1", goal="g", required_skills=["code"])],
    ))
    assert r["results"][0]["status"] == "blocked_by_governor"
    events = await log.replay()
    assert any(e.type == "governance.violation" for e in events)


# #3 减员按真实 health：失败任务会拉低 health
@pytest.mark.asyncio
async def test_mobilization_health_driven_by_outcome(log):
    from campaign.app.config import Config
    from campaign.app.runtime import Runtime
    from campaign.roles.base import Agent
    from campaign.resilience.mobilization import Mobilizer, CapacityLedger

    class Failing(Agent):
        async def handle(self, task):
            raise RuntimeError("boom")

    rt = Runtime(log, Config())
    spec = AgentSpec(id="failer", role="executor", model_tier="value", skills=["code"])
    rt.register_agent(Failing(spec, log))
    ledger = CapacityLedger()
    rt.set_mobilizer(Mobilizer(log, ledger))

    r = await rt.run(ExecutionOrder(objective="o", tasks=[Task(id="t1", goal="g", required_skills=["code"])]))
    assert r["results"][0]["status"] == "failed"
    assert ledger.health["failer"] < 1.0      # 真实失败拉低了 health（之前恒为 1.0）


# #4 执行超时：挂住的 agent 不卡死整个 run
@pytest.mark.asyncio
async def test_task_timeout(log):
    import asyncio
    from campaign.app.config import Config
    from campaign.app.runtime import Runtime
    from campaign.roles.base import Agent

    class Slow(Agent):
        async def handle(self, task):
            await asyncio.sleep(2.0)
            return {"output": "late"}

    rt = Runtime(log, Config())
    rt.register_agent(Slow(AgentSpec(id="slow", role="executor", model_tier="value", skills=["code"]), log))
    rt.set_task_timeout(0.05)

    r = await rt.run(ExecutionOrder(objective="o", tasks=[Task(id="t1", goal="g", required_skills=["code"])]))
    assert r["results"][0]["status"] == "failed"
    assert r["results"][0]["error"] == "timeout"


# ── 深层 #1 真并行 + #3 全链路治理 ──────────────────────

# #1 真并行 + 并发安全：并发下预算账目不被竞争破坏
@pytest.mark.asyncio
async def test_concurrent_budget_is_race_safe(log):
    from campaign.app.config import Config
    from campaign.app.runtime import Runtime
    from campaign.roles.executor import Executor
    from campaign.governance.governor import Governor
    from campaign.governance.policy import BudgetRule, AuthorityRule
    rt = Runtime(log, Config())
    rt.register_agent(Executor(AgentSpec(id="e", role="executor", model_tier="coder", skills=["code"]), log))
    rt.set_governor(Governor(log, rules=[BudgetRule(), AuthorityRule()]))
    rt.set_concurrency(5)  # 并发跑

    # coder=300/个，预算 1000 → 最多 3 个过（300/600/900≤1000，1200/1500 超）
    tasks = [Task(id=f"t{i}", goal="g", required_skills=["code"]) for i in range(5)]
    r = await rt.run(ExecutionOrder(objective="o", budget={"token_limit": 1000}, tasks=tasks))
    done = [x for x in r["results"] if x["status"] == "done"]
    blocked = [x for x in r["results"] if x["status"] == "blocked_by_governor"]
    assert len(done) == 3 and len(blocked) == 2   # gate 锁保证账目串行、结果确定


# #1 并行正确性：独立任务并发全部完成
@pytest.mark.asyncio
async def test_parallel_all_independent_done(log):
    from campaign.app.config import Config
    from campaign.app.runtime import Runtime
    from campaign.roles.executor import Executor
    rt = Runtime(log, Config())
    rt.register_agent(Executor(AgentSpec(id="e", role="executor", model_tier="value", skills=["code"]), log))
    rt.set_concurrency(4)
    tasks = [Task(id=f"t{i}", goal="g", required_skills=["code"]) for i in range(4)]
    r = await rt.run(ExecutionOrder(objective="o", tasks=tasks))
    assert all(x["status"] == "done" for x in r["results"])
    s = await rt.state(r["run_id"])
    assert len(s.tasks_done) == 4


# #3 全链路治理：LLMClient 每次调用都过执行闸，超预算即拦
@pytest.mark.asyncio
async def test_llm_call_gated_by_governor(log):
    from campaign.llm.client import LLMClient, TierConfig, LLMError
    from campaign.governance.governor import Governor
    from campaign.governance.policy import BudgetRule, AuthorityRule
    from campaign.governance.gate import PolicyGate

    gate = PolicyGate(Governor(log, rules=[BudgetRule(), AuthorityRule()]))
    client = LLMClient(
        tiers={"coder": TierConfig(model="m", base_url="http://localhost")},
        mock_responses={"coder": [{"choices": [{"message": {"content": "1"}}]},
                                  {"choices": [{"message": {"content": "2"}}]}]},
        gate=gate,
        gate_ctx={"actor_role": "executor", "budget": {"token_limit": 100}},
    )
    await client.complete("coder", [{"role": "user", "content": "a"}], est_cost=60)  # 60≤100 ok
    with pytest.raises(LLMError, match="blocked by governor"):
        await client.complete("coder", [{"role": "user", "content": "b"}], est_cost=60)  # 120>100 拦
    await client.close()
