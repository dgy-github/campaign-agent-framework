import pytest

from campaign.app.config import Config
from campaign.app.runtime import Runtime
from campaign.core.events import SqliteEventLog
from campaign.core.models import AgentSpec, ExecutionOrder, Task
from campaign.core.state import derive_state
from campaign.roles.base import Agent


class DurableAwaitingAgent(Agent):
    async def handle(self, task: Task) -> dict:
        if not task.human_input:
            return {
                "task_id": task.id,
                "status": "input_required",
                "prompt": "need durable input",
            }
        return {"task_id": task.id, "answer": task.human_input["decision"]}


def make_runtime(log: SqliteEventLog) -> Runtime:
    runtime = Runtime(log, Config())
    runtime.register_agent(
        DurableAwaitingAgent(
            AgentSpec(id="exec", role="executor", model_tier="value"),
            log,
        )
    )
    return runtime


@pytest.mark.asyncio
async def test_resume_rebuilds_awaiting_task_from_sqlite_event_log(tmp_path):
    db_path = tmp_path / "durable_hitl.db"
    budget = {"tokens": 123}

    log_a = SqliteEventLog(db_path=str(db_path))
    try:
        runtime_a = make_runtime(log_a)
        result = await runtime_a.run(
            ExecutionOrder(
                objective="durable hitl",
                budget=budget,
                tasks=[Task(id="t1", goal="ask for input")],
            )
        )
        run_id = result["run_id"]

        events = await log_a.replay()
        input_event = [event for event in events if event.type == "task.input_required"][-1]
        assert input_event.payload["task"]["id"] == "t1"
        assert input_event.payload["budget"] == budget
        assert derive_state(events, run_id=run_id).tasks_awaiting_input == ["t1"]
    finally:
        log_a.close()

    log_b = SqliteEventLog(db_path=str(db_path))
    try:
        runtime_b = make_runtime(log_b)

        resumed = await runtime_b.resume(run_id, "t1", {"decision": "ship"})

        assert resumed["status"] == "done"
        assert resumed["answer"] == "ship"
        events = await log_b.replay()
        state = derive_state(events, run_id=run_id)
        assert state.tasks_awaiting_input == []
        assert state.tasks_done == ["t1"]
        assert any(event.type == "task.resumed" for event in events)
    finally:
        log_b.close()


@pytest.mark.asyncio
async def test_durable_resume_rejects_task_not_awaiting_input(tmp_path):
    db_path = tmp_path / "durable_hitl_not_found.db"

    log_a = SqliteEventLog(db_path=str(db_path))
    try:
        runtime_a = make_runtime(log_a)
        result = await runtime_a.run(
            ExecutionOrder(
                objective="durable hitl",
                tasks=[Task(id="t1", goal="ask for input")],
            )
        )
        run_id = result["run_id"]
    finally:
        log_a.close()

    log_b = SqliteEventLog(db_path=str(db_path))
    try:
        runtime_b = make_runtime(log_b)
        resumed = await runtime_b.resume(run_id, "t1", {"decision": "ship"})
        assert resumed["status"] == "done"
    finally:
        log_b.close()

    log_c = SqliteEventLog(db_path=str(db_path))
    try:
        runtime_c = make_runtime(log_c)

        assert await runtime_c.resume(run_id, "t1", {"decision": "again"}) == {
            "task_id": "t1",
            "status": "not_found",
        }
        assert await runtime_c.resume(run_id, "missing", {"decision": "ship"}) == {
            "task_id": "missing",
            "status": "not_found",
        }
    finally:
        log_c.close()
