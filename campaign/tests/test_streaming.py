import httpx
import pytest

from campaign.core.models import AgentSpec, Task
from campaign.protocol import Message
from campaign.roles.base import Agent
from campaign.transport import HttpJsonRpcTransport, JsonRpcAgentServer


class StreamingEchoAgent(Agent):
    def __init__(self) -> None:
        super().__init__(AgentSpec(id="worker", role="executor", model_tier="value"), None)

    async def handle(self, task: Task) -> dict:
        return {"task_id": task.id, "answer": f"done: {task.goal}"}


def make_message(to_agent: str = "worker") -> Message:
    return Message.request(
        "coordinator",
        to_agent,
        Task(id="task-1", goal="stream this"),
        "run-1",
    )


def make_transport(
    server: JsonRpcAgentServer,
    auth_token: str | None = None,
) -> HttpJsonRpcTransport:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.asgi_app()),
        base_url="http://test",
    )
    return HttpJsonRpcTransport(
        "http://test/rpc",
        client=client,
        auth_token=auth_token,
    )


async def collect_stream(transport: HttpJsonRpcTransport, msg: Message) -> list[dict]:
    try:
        return [event async for event in transport.send_stream(msg)]
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_send_stream_yields_status_then_result_with_worker_answer():
    server = JsonRpcAgentServer({"worker": StreamingEchoAgent()})
    transport = make_transport(server)

    events = await collect_stream(transport, make_message())

    assert [event["event"] for event in events] == ["status", "result"]
    assert events[0]["data"] == {"task_id": "task-1", "state": "working"}
    result = events[1]["data"]
    assert result["kind"] == "response"
    assert result["error"] is None
    assert result["parts"][0]["data"] == {
        "task_id": "task-1",
        "answer": "done: stream this",
    }


@pytest.mark.asyncio
async def test_send_stream_missing_target_yields_error_event():
    server = JsonRpcAgentServer({})
    transport = make_transport(server)

    events = await collect_stream(transport, make_message(to_agent="missing"))

    assert events[-1]["event"] == "error"
    assert events[-1]["data"]["error"] == "target agent not found: missing"


@pytest.mark.asyncio
async def test_send_stream_auth_rejects_missing_and_wrong_tokens_then_accepts_valid_token():
    server = JsonRpcAgentServer({"worker": StreamingEchoAgent()}, auth_token="secret")

    missing = await collect_stream(make_transport(server), make_message())
    wrong = await collect_stream(make_transport(server, auth_token="wrong"), make_message())
    valid = await collect_stream(make_transport(server, auth_token="secret"), make_message())

    assert missing == [{"event": "error", "data": {"error": "unauthorized"}}]
    assert wrong == [{"event": "error", "data": {"error": "unauthorized"}}]
    assert [event["event"] for event in valid] == ["status", "result"]
    assert valid[1]["data"]["parts"][0]["data"]["answer"] == "done: stream this"
