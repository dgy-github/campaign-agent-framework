import os
import tempfile

import pytest
from pydantic import ValidationError

from campaign.app.config import Config
from campaign.app.runtime import Runtime
from campaign.core.events import SqliteEventLog
from campaign.core.models import AgentSpec, ExecutionOrder, Task
from campaign.core.state import derive_state
from campaign.roles.base import Agent


@pytest.fixture
def event_log():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="dag_")
    os.close(fd)
    log = SqliteEventLog(db_path=path)
    yield log
    log.close()
    try:
        os.unlink(path)
    except OSError:
        pass


class RecordingAgent(Agent):
    async def handle(self, task: Task) -> dict:
        return {"task_id": task.id, "output": task.goal}


class FailsFirstAgent(Agent):
    async def handle(self, task: Task) -> dict:
        if task.id == "t1":
            raise RuntimeError("boom")
        return {"task_id": task.id, "output": "ok"}


@pytest.mark.asyncio
async def test_dependency_runs_before_dependent(event_log):
    runtime = Runtime(event_log, Config())
    runtime.set_concurrency(2)
    runtime.register_agent(RecordingAgent(AgentSpec(id="exec", role="executor", model_tier="value"), event_log))
    order = ExecutionOrder(
        objective="dag",
        tasks=[
            Task(id="t1", goal="first"),
            Task(id="t2", goal="second", depends_on=["t1"]),
        ],
    )

    result = await runtime.run(order)

    assert [r["status"] for r in result["results"]] == ["done", "done"]
    events = await event_log.replay()
    done_seq = {e.payload["task_id"]: e.seq for e in events if e.type == "task.done"}
    assigned_seq = {e.payload["task_id"]: e.seq for e in events if e.type == "task.assigned"}
    assert done_seq["t1"] < assigned_seq["t2"]


@pytest.mark.asyncio
async def test_failed_dependency_skips_dependent(event_log):
    runtime = Runtime(event_log, Config())
    runtime.register_agent(FailsFirstAgent(AgentSpec(id="exec", role="executor", model_tier="value"), event_log))
    order = ExecutionOrder(
        objective="dag",
        tasks=[
            Task(id="t1", goal="fail"),
            Task(id="t2", goal="skip", depends_on=["t1"]),
        ],
    )

    result = await runtime.run(order)

    assert [r["status"] for r in result["results"]] == ["failed", "skipped_dependency"]
    state = derive_state(await event_log.replay(), run_id=result["run_id"])
    assert state.tasks_failed == ["t1"]
    assert state.tasks_skipped == ["t2"]


def test_cycle_dependency_rejected():
    with pytest.raises(ValidationError):
        ExecutionOrder(
            objective="cycle",
            tasks=[
                Task(id="t1", goal="one", depends_on=["t2"]),
                Task(id="t2", goal="two", depends_on=["t1"]),
            ],
        )
