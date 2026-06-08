import os
import tempfile

import httpx
import pytest

from campaign.app.config import Config
from campaign.app.runtime import Runtime
from campaign.core.events import SqliteEventLog
from campaign.core.models import AgentSpec, ExecutionOrder, Task
from campaign.core.state import derive_state
from campaign.roles.executor import Executor
from campaign.roles.reviewer import Reviewer
from campaign.transport import HttpJsonRpcTransport, JsonRpcAgentServer, RemoteAgentProxy


def _shared_logs():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="distributed_")
    os.close(fd)
    return path, SqliteEventLog(path), SqliteEventLog(path)


def _transport_for(server: JsonRpcAgentServer) -> HttpJsonRpcTransport:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.asgi_app()),
        base_url="http://node-b",
    )
    return HttpJsonRpcTransport("http://node-b/rpc", client=client)


async def _close(path: str, log_a: SqliteEventLog, log_b: SqliteEventLog, transport: HttpJsonRpcTransport) -> None:
    await transport.aclose()
    log_a.close()
    log_b.close()
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.mark.asyncio
async def test_runtime_routes_remote_agent_over_http_with_shared_sqlite_log():
    path, log_a, log_b = _shared_logs()
    exec_spec = AgentSpec(id="exec", role="executor", model_tier="value", skills=["code"])
    server = JsonRpcAgentServer(
        {"exec": Executor(exec_spec, log_b)},
        known_senders={"coordinator", "system", "exec"},
        log=log_b,
    )
    transport = _transport_for(server)

    try:
        runtime = Runtime(log_a, Config())
        runtime.set_transport(transport)
        runtime.register_remote(exec_spec)
        runtime.register_agent(
            Reviewer(AgentSpec(id="reviewer-a", role="reviewer", model_tier="flagship", skills=["review"]), log_a)
        )

        result = await runtime.run(
            ExecutionOrder(
                objective="distributed success",
                tasks=[Task(id="t1", goal="write code", difficulty="simple", required_skills=["code"])],
            )
        )

        assert result["tasks_total"] == 1
        assert result["results"][0]["status"] == "done"

        events = await log_a.replay()
        state = derive_state(events, run_id=result["run_id"])
        assert state.tasks_done == ["t1"]
    finally:
        await _close(path, log_a, log_b, transport)


@pytest.mark.asyncio
async def test_shared_log_contains_node_b_worker_events():
    path, log_a, log_b = _shared_logs()
    exec_spec = AgentSpec(id="exec", role="executor", model_tier="value", skills=["code"])
    server = JsonRpcAgentServer(
        {"exec": Executor(exec_spec, log_b)},
        known_senders={"coordinator", "system", "exec"},
        log=log_b,
    )
    transport = _transport_for(server)

    try:
        runtime = Runtime(log_a, Config())
        runtime.set_transport(transport)
        runtime.register_remote(exec_spec)
        runtime.register_agent(
            Reviewer(AgentSpec(id="reviewer-a", role="reviewer", model_tier="flagship", skills=["review"]), log_a)
        )

        result = await runtime.run(
            ExecutionOrder(
                objective="shared event stream",
                tasks=[Task(id="t1", goal="write code", difficulty="simple", required_skills=["code"])],
            )
        )

        events = await log_a.replay()
        run_events = [event for event in events if event.payload.get("run_id") in (None, result["run_id"])]
        assert any(event.type == "executor.output" and event.actor == "exec" for event in run_events)
        assert any(event.type == "a2a.message" and event.actor == "coordinator" for event in run_events)
        assert any(event.type == "a2a.message" and event.actor == "exec" for event in run_events)
    finally:
        await _close(path, log_a, log_b, transport)


@pytest.mark.asyncio
async def test_remote_agent_missing_on_server_fails_task_without_crashing():
    path, log_a, log_b = _shared_logs()
    server = JsonRpcAgentServer({}, known_senders={"coordinator", "system"}, log=log_b)
    transport = _transport_for(server)

    try:
        runtime = Runtime(log_a, Config())
        runtime.set_transport(transport)
        runtime.register_remote(AgentSpec(id="exec", role="executor", model_tier="value", skills=["code"]))
        runtime.register_agent(
            Reviewer(AgentSpec(id="reviewer-a", role="reviewer", model_tier="flagship", skills=["review"]), log_a)
        )

        result = await runtime.run(
            ExecutionOrder(
                objective="remote missing",
                tasks=[Task(id="t1", goal="write code", difficulty="simple", required_skills=["code"])],
            )
        )

        assert result["tasks_total"] == 1
        assert result["results"][0]["status"] == "failed"
        assert "target agent not found: exec" in result["results"][0]["error"]

        state = derive_state(await log_a.replay(), run_id=result["run_id"])
        assert state.tasks_failed == ["t1"]
    finally:
        await _close(path, log_a, log_b, transport)


@pytest.mark.asyncio
async def test_remote_agent_proxy_handle_raises_if_used_locally():
    path, log_a, log_b = _shared_logs()
    try:
        proxy = RemoteAgentProxy(AgentSpec(id="exec", role="executor", model_tier="value", skills=["code"]), log_a)

        with pytest.raises(RuntimeError, match="RemoteAgentProxy.handle should not run locally"):
            await proxy.handle(Task(id="t1", goal="write code", required_skills=["code"]))
    finally:
        log_a.close()
        log_b.close()
        try:
            os.unlink(path)
        except OSError:
            pass
