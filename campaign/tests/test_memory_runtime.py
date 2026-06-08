"""M-F 三层 Memory 接入 runtime（opt-in，默认 OFF）测试。"""
import os
import tempfile

import pytest

from campaign.app.config import Config
from campaign.app.runtime import Runtime
from campaign.core.events import SqliteEventLog
from campaign.core.models import AgentSpec, ExecutionOrder, Task
from campaign.roles.base import Agent


class RecordingExecutor(Agent):
    """记录每次收到的 session_context，便于断言注入。"""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.seen_sessions: list = []

    async def handle(self, task: Task) -> dict:
        return {"task_id": task.id, "output": "ok"}

    async def on_message(self, msg):
        for p in msg.parts:
            if isinstance(p.data, dict) and "session_context" in p.data:
                self.seen_sessions.append(p.data["session_context"])
        return await super().on_message(msg)


@pytest.fixture
def log():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="memrt_")
    os.close(fd)
    lg = SqliteEventLog(db_path=path)
    yield lg
    lg.close()
    try:
        os.unlink(path)
    except OSError:
        pass


def _order():
    return ExecutionOrder(objective="mem", tasks=[
        Task(id="t1", goal="g1", required_skills=["x"]),
        Task(id="t2", goal="g2", required_skills=["x"]),
    ])


@pytest.mark.asyncio
async def test_memory_off_by_default_no_injection_no_scratch(log):
    rt = Runtime(log, Config())
    rt.register_agent(RecordingExecutor(AgentSpec(id="e", role="executor", model_tier="value", skills=["x"]), log))
    result = await rt.run(_order())
    assert all(r["status"] == "done" for r in result["results"])
    # 默认未 enable_memory：无 scratchpad 字段、无 session 注入
    assert all("scratchpad" not in r for r in result["results"])


@pytest.mark.asyncio
async def test_session_memory_injects_prior_task_outcome(log):
    rt = Runtime(log, Config())
    agent = RecordingExecutor(AgentSpec(id="e", role="executor", model_tier="value", skills=["x"]), log)
    rt.register_agent(agent)
    rt.enable_memory(session=True, scratch=True)

    result = await rt.run(_order())
    assert all(r["status"] == "done" for r in result["results"])
    # 第二个任务应看到第一个任务 (t1) 的 session 摘要被注入
    assert agent.seen_sessions, "expected session_context injected into a later task"
    flat = [entry.get("task_id") for ctx in agent.seen_sessions for entry in ctx]
    assert "t1" in flat
    # scratch 启用 → 结果含 scratchpad 字段
    assert all("scratchpad" in r for r in result["results"])


@pytest.mark.asyncio
async def test_memory_session_only_enables_session_without_scratch(log):
    rt = Runtime(log, Config())
    agent = RecordingExecutor(AgentSpec(id="e", role="executor", model_tier="value", skills=["x"]), log)
    rt.register_agent(agent)
    rt.enable_memory(session=True, scratch=False)

    result = await rt.run(_order())

    assert all(r["status"] == "done" for r in result["results"])
    assert agent.seen_sessions, "expected session_context when session memory is enabled"
    assert all("scratchpad" not in r for r in result["results"])


@pytest.mark.asyncio
async def test_memory_scratch_only_enables_scratch_without_session(log):
    rt = Runtime(log, Config())
    agent = RecordingExecutor(AgentSpec(id="e", role="executor", model_tier="value", skills=["x"]), log)
    rt.register_agent(agent)
    rt.enable_memory(session=False, scratch=True)

    result = await rt.run(_order())

    assert all(r["status"] == "done" for r in result["results"])
    assert agent.seen_sessions == []
    assert all("scratchpad" in r for r in result["results"])


@pytest.mark.asyncio
async def test_session_memory_with_concurrency_above_one_does_not_error(log):
    rt = Runtime(log, Config())
    rt.register_agent(RecordingExecutor(AgentSpec(id="e", role="executor", model_tier="value", skills=["x"]), log))
    rt.enable_memory(session=True, scratch=False)
    rt.set_concurrency(2)

    result = await rt.run(_order())

    assert result["tasks_total"] == 2
    assert all(r["status"] == "done" for r in result["results"])
