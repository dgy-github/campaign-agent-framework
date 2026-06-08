import os
import tempfile

import pytest

from campaign.app.config import Config
from campaign.app.runtime import Runtime
from campaign.core.events import SqliteEventLog
from campaign.core.models import AgentSpec, ExecutionOrder, Task
from campaign.core.state import derive_state
from campaign.protocol import AgentCard, Message
from campaign.roles.base import Agent
from campaign.roles.coordinator import Coordinator
from campaign.roles.executor import Executor
from campaign.roles.reviewer import Reviewer
from campaign.transport import HttpJsonRpcTransport, InProcessTransport


@pytest.fixture
def event_log():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="a2a_")
    os.close(fd)
    log = SqliteEventLog(db_path=path)
    yield log
    log.close()
    try:
        os.unlink(path)
    except OSError:
        pass


def test_message_request_reply_round_trip():
    task = Task(id="t1", goal="write tests", difficulty="simple", required_skills=["coding"])
    msg = Message.request("coordinator", "exec-1", task, "run-1")

    assert msg.task_id == "t1"
    restored = Task(**msg.parts[0].data)
    assert restored == task

    resp = msg.reply({"task_id": "t1", "output": "ok"})
    assert resp.kind == "response"
    assert resp.from_agent == "exec-1"
    assert resp.to_agent == "coordinator"
    assert resp.correlation_id == msg.message_id
    assert resp.result["output"] == "ok"


def test_agent_card_from_spec():
    spec = AgentSpec(id="exec-1", role="executor", model_tier="coder", skills=["coding"])
    card = AgentCard.from_spec(spec)

    assert card.id == "exec-1"
    assert card.role == "executor"
    assert card.model_tier == "coder"
    assert card.skills == ["coding"]
    assert card.endpoint is None
    assert card.transport == "in_process"


@pytest.mark.asyncio
async def test_in_process_transport_routes_request(event_log):
    spec = AgentSpec(id="exec-1", role="executor", model_tier="coder", skills=["coding"])
    executor = Executor(spec, event_log)
    transport = InProcessTransport({"exec-1": executor}, event_log)
    task = Task(id="t1", goal="implement feature", required_skills=["coding"])

    resp = await transport.send(Message.request("coordinator", "exec-1", task, "run-1"))

    assert resp.error is None
    assert resp.kind == "response"
    assert resp.result["task_id"] == "t1"
    assert "output" in resp.result
    assert transport.cards()[0].id == "exec-1"


@pytest.mark.asyncio
async def test_in_process_transport_missing_target_returns_error(event_log):
    transport = InProcessTransport(log=event_log)
    task = Task(id="t1", goal="missing")

    resp = await transport.send(Message.request("coordinator", "missing", task, "run-1"))

    assert resp.kind == "response"
    assert resp.error
    assert "missing" in resp.error


class EchoAgent(Agent):
    async def handle(self, task: Task) -> dict:
        return {"task_id": task.id, "echo": task.goal}


class FailingAgent(Agent):
    async def handle(self, task: Task) -> dict:
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_agent_on_message_adapts_handle(event_log):
    agent = EchoAgent(AgentSpec(id="echo", role="executor", model_tier="value", skills=[]), event_log)
    msg = Message.request("coordinator", "echo", Task(id="t1", goal="hello"), "run-1")

    resp = await agent.on_message(msg)

    assert resp.error is None
    assert resp.result == {"task_id": "t1", "echo": "hello"}


@pytest.mark.asyncio
async def test_agent_on_message_returns_error_for_handle_exception(event_log):
    agent = FailingAgent(AgentSpec(id="fail", role="executor", model_tier="value", skills=[]), event_log)
    msg = Message.request("coordinator", "fail", Task(id="t1", goal="hello"), "run-1")

    resp = await agent.on_message(msg)

    assert resp.error == "boom"
    assert resp.result == {}


@pytest.mark.asyncio
async def test_runtime_uses_default_in_process_transport_end_to_end(event_log):
    runtime = Runtime(event_log, Config())
    runtime.set_coordinator(
        Coordinator(AgentSpec(id="coordinator", role="coordinator", model_tier="flagship", skills=[]), event_log)
    )
    runtime.register_agent(
        Executor(AgentSpec(id="exec-1", role="executor", model_tier="coder", skills=["coding"]), event_log)
    )
    runtime.register_agent(
        Reviewer(AgentSpec(id="rev-1", role="reviewer", model_tier="flagship", skills=[]), event_log)
    )

    order = ExecutionOrder(
        objective="a2a runtime",
        tasks=[
            Task(id="t1", goal="implement", difficulty="simple", required_skills=["coding"]),
            Task(id="t2", goal="test", difficulty="simple", required_skills=["coding"]),
        ],
    )

    result = await runtime.run(order)
    assert result["tasks_total"] == 2
    assert all(r["status"] == "done" for r in result["results"])

    events = await event_log.replay()
    event_types = [e.type for e in events]
    assert "task.assigned" in event_types
    assert "task.done" in event_types
    assert "a2a.message" in event_types

    state = derive_state(events, run_id=result["run_id"])
    assert state.tasks_done == ["t1", "t2"]


@pytest.mark.asyncio
async def test_http_json_rpc_transport_returns_error_message_on_failure():
    transport = HttpJsonRpcTransport()
    task = Task(id="t1", goal="remote")

    resp = await transport.send(Message.request("coordinator", "remote", task, "run-1"))

    assert resp.kind == "response"
    assert resp.error
    assert "http transport error" in resp.error
