import os
import tempfile

import pytest

from campaign.app.config import Config
from campaign.app.runtime import Runtime
from campaign.core.events import SqliteEventLog
from campaign.core.models import AgentSpec, ExecutionOrder, Task
from campaign.core.state import derive_state
from campaign.roles.base import Agent


@pytest.fixture
def event_log():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="hitl_")
    os.close(fd)
    log = SqliteEventLog(db_path=path)
    yield log
    log.close()
    try:
        os.unlink(path)
    except OSError:
        pass


class AwaitingAgent(Agent):
    async def handle(self, task: Task) -> dict:
        if not task.human_input:
            return {"task_id": task.id, "status": "input_required", "prompt": "need a decision"}
        return {"task_id": task.id, "answer": task.human_input["decision"]}


class CountingAgent(Agent):
    def __init__(self, spec: AgentSpec, log: SqliteEventLog) -> None:
        super().__init__(spec, log)
        self.calls = 0

    async def handle(self, task: Task) -> dict:
        self.calls += 1
        return {"task_id": task.id, "approved": task.human_input.get("approved", False)}


@pytest.mark.asyncio
async def test_agent_input_required_then_resume_done(event_log):
    runtime = Runtime(event_log, Config())
    runtime.register_agent(AwaitingAgent(AgentSpec(id="exec", role="executor", model_tier="value"), event_log))
    order = ExecutionOrder(objective="hitl", tasks=[Task(id="t1", goal="ask")])

    result = await runtime.run(order)

    assert result["results"] == [{"task_id": "t1", "status": "input_required", "prompt": "need a decision"}]
    state = derive_state(await event_log.replay(), run_id=result["run_id"])
    assert state.tasks_awaiting_input == ["t1"]
    assert state.tasks_done == []

    resumed = await runtime.resume(result["run_id"], "t1", {"decision": "ship"})

    assert resumed["status"] == "done"
    assert resumed["answer"] == "ship"
    state = derive_state(await event_log.replay(), run_id=result["run_id"])
    assert state.tasks_awaiting_input == []
    assert state.tasks_done == ["t1"]


@pytest.mark.asyncio
async def test_needs_approval_waits_until_resume(event_log):
    runtime = Runtime(event_log, Config())
    agent = CountingAgent(AgentSpec(id="exec", role="executor", model_tier="value"), event_log)
    runtime.register_agent(agent)
    order = ExecutionOrder(objective="approval", tasks=[Task(id="t1", goal="deploy", needs_approval=True)])

    result = await runtime.run(order)

    assert result["results"][0]["status"] == "input_required"
    assert result["results"][0]["prompt"] == "approval required"
    assert agent.calls == 0

    resumed = await runtime.resume(result["run_id"], "t1", {"approved_by": "human"})

    assert resumed["status"] == "done"
    assert resumed["approved"] is True
    assert agent.calls == 1
