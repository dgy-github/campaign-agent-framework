"""M-G Agent.ask 询问式测试（InProcess transport）。"""
import os
import tempfile

import pytest

from campaign.core.events import SqliteEventLog
from campaign.core.models import AgentSpec, Task
from campaign.roles.base import Agent
from campaign.transport import InProcessTransport


class QueryAgent(Agent):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.seen_queries = []

    async def handle(self, task: Task) -> dict:
        return {"task_id": task.id}

    async def on_message(self, msg):
        q = msg.parts[0].data.get("query") if msg.parts and isinstance(msg.parts[0].data, dict) else None
        self.seen_queries.append(q)
        return msg.reply({"answer": f"echo:{q}"})


class ExplodingQueryAgent(QueryAgent):
    async def on_message(self, msg):
        raise RuntimeError("query boom")


@pytest.fixture
def log():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="ask_")
    os.close(fd)
    lg = SqliteEventLog(db_path=path)
    yield lg
    lg.close()
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.mark.asyncio
async def test_ask_returns_result_marked_untrusted(log):
    a = QueryAgent(AgentSpec(id="a", role="executor", model_tier="value"), log)
    b = QueryAgent(AgentSpec(id="b", role="retriever", model_tier="value"), log)
    transport = InProcessTransport({"a": a, "b": b}, log)

    res = await a.ask("b", "hello", transport, run_id="r1", task_id="t1")
    assert res["untrusted"] is True
    assert res["result"]["answer"] == "echo:hello"


@pytest.mark.asyncio
async def test_ask_transports_dict_query(log):
    a = QueryAgent(AgentSpec(id="a", role="executor", model_tier="value"), log)
    b = QueryAgent(AgentSpec(id="b", role="retriever", model_tier="value"), log)
    transport = InProcessTransport({"a": a, "b": b}, log)

    query = {"topic": "memory", "k": 3}
    res = await a.ask("b", query, transport, run_id="r1", task_id="t1")

    assert res["untrusted"] is True
    assert res["result"]["answer"] == "echo:{'topic': 'memory', 'k': 3}"
    assert b.seen_queries == [query]


@pytest.mark.asyncio
async def test_ask_missing_target_errors_without_crash(log):
    a = QueryAgent(AgentSpec(id="a", role="executor", model_tier="value"), log)
    transport = InProcessTransport({"a": a}, log)

    res = await a.ask("nonexistent", "hi", transport)
    assert res["untrusted"] is True
    assert "error" in res and res.get("result") is None or "error" in res
    assert "not found" in str(res.get("error", "")).lower()


@pytest.mark.asyncio
async def test_ask_target_exception_returns_untrusted_error(log):
    a = QueryAgent(AgentSpec(id="a", role="executor", model_tier="value"), log)
    b = ExplodingQueryAgent(AgentSpec(id="b", role="retriever", model_tier="value"), log)
    transport = InProcessTransport({"a": a, "b": b}, log)

    res = await a.ask("b", "hello", transport)

    assert res["untrusted"] is True
    assert "query boom" in res["error"]
