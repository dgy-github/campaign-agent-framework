import json

import httpx
import pytest

from campaign.core.models import AgentSpec, Task
from campaign.protocol import Message
from campaign.roles.base import Agent
from campaign.transport import HttpJsonRpcTransport, JsonRpcAgentServer


class CountingAgent(Agent):
    def __init__(self) -> None:
        super().__init__(AgentSpec(id="exec", role="executor", model_tier="value"), None)
        self.calls = 0

    async def handle(self, task: Task) -> dict:
        self.calls += 1
        return {"task_id": task.id, "calls": self.calls}


def make_message(message_id: str = "msg-1") -> Message:
    msg = Message.request("coordinator", "exec", Task(id="t1", goal="ship"), "run-1")
    msg.message_id = message_id
    return msg


def rpc_request(msg: Message) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": msg.message_id,
        "method": "message/send",
        "params": msg.model_dump(mode="json"),
    }


def rpc_result(request: httpx.Request) -> httpx.Response:
    payload = json.loads(request.content.decode("utf-8"))
    msg = Message.model_validate(payload["params"])
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": payload["id"],
            "result": msg.reply({"ok": True}).model_dump(mode="json"),
        },
    )


@pytest.mark.asyncio
async def test_http_transport_retries_transient_then_succeeds():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503)
        return rpc_result(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = HttpJsonRpcTransport("http://test/rpc", client=client, retries=1, backoff=0)

    resp = await transport.send(make_message())
    await client.aclose()

    assert calls == 2
    assert resp.error is None
    assert resp.result == {"ok": True}


@pytest.mark.asyncio
async def test_http_transport_retry_exhaustion_returns_error_message():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = HttpJsonRpcTransport("http://test/rpc", client=client, retries=2, backoff=0)

    resp = await transport.send(make_message())
    await client.aclose()

    assert calls == 3
    assert resp.kind == "response"
    assert resp.error is not None
    assert "HTTP 503" in resp.error


@pytest.mark.asyncio
async def test_http_transport_does_not_retry_non_429_4xx():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = HttpJsonRpcTransport("http://test/rpc", client=client, retries=3, backoff=0)

    resp = await transport.send(make_message())
    await client.aclose()

    assert calls == 1
    assert resp.error == "http transport error: HTTP 400"


@pytest.mark.asyncio
async def test_json_rpc_server_deduplicates_by_message_id():
    agent = CountingAgent()
    server = JsonRpcAgentServer({"exec": agent})
    request = rpc_request(make_message("same-id"))

    first = await server.handle_rpc(request)
    second = await server.handle_rpc(request)

    assert agent.calls == 1
    assert second == first
    assert first["result"]["parts"][0]["data"] == {"task_id": "t1", "calls": 1}


@pytest.mark.asyncio
async def test_json_rpc_server_auth_token_rejects_missing_and_wrong_tokens():
    server = JsonRpcAgentServer({"exec": CountingAgent()}, auth_token="secret")
    request = rpc_request(make_message())

    missing = await server.handle_rpc(request)
    wrong = await server.handle_rpc(request, headers={"Authorization": "Bearer wrong"})
    valid = await server.handle_rpc(request, headers={"Authorization": "Bearer secret"})

    assert missing["error"]["code"] == -32001
    assert wrong["error"]["code"] == -32001
    assert "result" in valid
    assert valid["result"]["error"] is None


@pytest.mark.asyncio
async def test_http_transport_asgi_auth_end_to_end():
    server = JsonRpcAgentServer({"exec": CountingAgent()}, auth_token="secret")
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.asgi_app()),
        base_url="http://test",
    )
    transport = HttpJsonRpcTransport(
        "http://test/rpc",
        client=client,
        auth_token="secret",
    )

    resp = await transport.send(make_message())
    await client.aclose()

    assert resp.error is None
    assert resp.result == {"task_id": "t1", "calls": 1}


@pytest.mark.asyncio
async def test_http_transport_asgi_auth_mismatch_returns_error_message():
    server = JsonRpcAgentServer({"exec": CountingAgent()}, auth_token="secret")
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.asgi_app()),
        base_url="http://test",
    )
    transport = HttpJsonRpcTransport(
        "http://test/rpc",
        client=client,
        auth_token="wrong",
    )

    resp = await transport.send(make_message())
    await client.aclose()

    assert resp.kind == "response"
    assert resp.error is not None
    assert "-32001" in resp.error


@pytest.mark.asyncio
async def test_http_transport_defaults_remain_backward_compatible():
    server = JsonRpcAgentServer({"exec": CountingAgent()})
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.asgi_app()),
        base_url="http://test",
    )
    transport = HttpJsonRpcTransport("http://test/rpc", client=client)

    resp = await transport.send(make_message())
    await client.aclose()

    assert resp.error is None
    assert resp.result == {"task_id": "t1", "calls": 1}
