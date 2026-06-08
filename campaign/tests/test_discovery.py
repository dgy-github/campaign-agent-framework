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
from campaign.transport import HttpJsonRpcTransport, InProcessTransport, JsonRpcAgentServer


def _shared_logs():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="discovery_")
    os.close(fd)
    return path, SqliteEventLog(path), SqliteEventLog(path)


def _transport_for(
    server: JsonRpcAgentServer,
    *,
    auth_token: str | None = None,
) -> HttpJsonRpcTransport:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.asgi_app()),
        base_url="http://node-b",
    )
    return HttpJsonRpcTransport("http://node-b/rpc", client=client, auth_token=auth_token)


async def _close(
    path: str,
    log_a: SqliteEventLog,
    log_b: SqliteEventLog,
    *transports: HttpJsonRpcTransport,
) -> None:
    for transport in transports:
        await transport.aclose()
    log_a.close()
    log_b.close()
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.mark.asyncio
async def test_json_rpc_server_agent_cards_returns_hosted_cards():
    path, log_a, log_b = _shared_logs()
    try:
        exec_spec = AgentSpec(id="exec", role="executor", model_tier="value", skills=["code"])
        rev_spec = AgentSpec(id="rev", role="reviewer", model_tier="flagship", skills=["review"])
        server = JsonRpcAgentServer(
            {
                "exec": Executor(exec_spec, log_b),
                "rev": Reviewer(rev_spec, log_b),
            },
            log=log_b,
        )

        response = await server.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "agent/cards"})

        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 1
        cards = sorted(response["result"]["cards"], key=lambda card: card["id"])
        assert [card["id"] for card in cards] == ["exec", "rev"]
        assert cards[0]["role"] == "executor"
        assert cards[0]["skills"] == ["code"]
        assert cards[1]["role"] == "reviewer"
        assert cards[1]["skills"] == ["review"]
    finally:
        await _close(path, log_a, log_b)


@pytest.mark.asyncio
async def test_http_transport_discover_returns_agent_cards_from_asgi_server():
    path, log_a, log_b = _shared_logs()
    transport = None
    try:
        exec_spec = AgentSpec(id="exec", role="executor", model_tier="value", skills=["code"])
        server = JsonRpcAgentServer({"exec": Executor(exec_spec, log_b)}, log=log_b)
        transport = _transport_for(server)

        cards = await transport.discover()

        assert len(cards) == 1
        assert cards[0].id == "exec"
        assert cards[0].role == "executor"
        assert cards[0].model_tier == "value"
        assert cards[0].skills == ["code"]
    finally:
        await _close(path, log_a, log_b, *(transport,) if transport else ())


@pytest.mark.asyncio
async def test_runtime_discovers_remote_agent_and_routes_task_over_http():
    path, log_a, log_b = _shared_logs()
    transport = None
    try:
        exec_spec = AgentSpec(id="exec", role="executor", model_tier="value", skills=["code"])
        server = JsonRpcAgentServer(
            {"exec": Executor(exec_spec, log_b)},
            known_senders={"coordinator", "system", "exec"},
            log=log_b,
        )
        transport = _transport_for(server)

        runtime = Runtime(log_a, Config())
        runtime.set_transport(transport)
        cards = await runtime.discover_remote(transport)
        runtime.register_agent(
            Reviewer(AgentSpec(id="reviewer-a", role="reviewer", model_tier="flagship", skills=["review"]), log_a)
        )

        result = await runtime.run(
            ExecutionOrder(
                objective="discovered distributed success",
                tasks=[Task(id="t1", goal="write code", difficulty="simple", required_skills=["code"])],
            )
        )

        assert [card.id for card in cards] == ["exec"]
        assert result["tasks_total"] == 1
        assert result["results"][0]["status"] == "done"

        events = await log_a.replay()
        state = derive_state(events, run_id=result["run_id"])
        assert state.tasks_done == ["t1"]
        assert any(event.type == "executor.output" and event.actor == "exec" for event in events)
    finally:
        await _close(path, log_a, log_b, *(transport,) if transport else ())


@pytest.mark.asyncio
async def test_discover_respects_bearer_auth_token():
    path, log_a, log_b = _shared_logs()
    missing_token = wrong_token = correct_token = None
    try:
        exec_spec = AgentSpec(id="exec", role="executor", model_tier="value", skills=["code"])
        server = JsonRpcAgentServer(
            {"exec": Executor(exec_spec, log_b)},
            log=log_b,
            auth_token="secret",
        )
        missing_token = _transport_for(server)
        wrong_token = _transport_for(server, auth_token="wrong")
        correct_token = _transport_for(server, auth_token="secret")

        assert await missing_token.discover() == []
        assert await wrong_token.discover() == []

        cards = await correct_token.discover()
        assert len(cards) == 1
        assert cards[0].id == "exec"
        assert cards[0].skills == ["code"]
    finally:
        transports = tuple(t for t in (missing_token, wrong_token, correct_token) if t is not None)
        await _close(path, log_a, log_b, *transports)


@pytest.mark.asyncio
async def test_in_process_transport_default_discover_is_empty_for_compatibility():
    assert await InProcessTransport({}).discover() == []
