import pytest

from campaign.app.config import Config
from campaign.app.runtime import Runtime
from campaign.core.events import SqliteEventLog
from campaign.core.models import AgentSpec, ExecutionOrder, Task
from campaign.knowledge import SqliteKnowledgeStore
from campaign.roles.base import Agent


class RecordingKnowledgeAgent(Agent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seen_parts = []

    async def handle(self, task: Task) -> dict:
        return {"task_id": task.id, "output": "ok"}

    async def on_message(self, msg):
        self.seen_parts.append(list(msg.parts))
        return await super().on_message(msg)


@pytest.fixture
def log(tmp_path):
    lg = SqliteEventLog(db_path=str(tmp_path / "events.db"))
    yield lg
    lg.close()


@pytest.fixture
def store():
    ks = SqliteKnowledgeStore()
    yield ks
    ks.close()


def _agent(log):
    return RecordingKnowledgeAgent(
        AgentSpec(id="worker", role="executor", model_tier="value", skills=["research"]),
        log,
    )


def _order(goal: str = "pytest runtime knowledge injection") -> ExecutionOrder:
    return ExecutionOrder(
        objective="knowledge runtime",
        tasks=[Task(id="t1", goal=goal, required_skills=["research"])],
    )


def _knowledge_parts(agent: RecordingKnowledgeAgent):
    return [
        part
        for parts in agent.seen_parts
        for part in parts
        if isinstance(part.data, dict) and "knowledge" in part.data
    ]


async def _run_with_store(log, store, config: Config):
    runtime = Runtime(log, config)
    agent = _agent(log)
    runtime.register_agent(agent)
    runtime.set_knowledge_store(store, k=3)

    result = await runtime.run(_order())

    assert all(item["status"] == "done" for item in result["results"])
    return agent, await log.replay()


@pytest.mark.asyncio
async def test_knowledge_store_off_by_default_has_no_part_or_event(log, store):
    store.add("related", "pytest runtime knowledge injection related document")
    runtime = Runtime(log, Config())
    agent = _agent(log)
    runtime.register_agent(agent)

    result = await runtime.run(_order())

    assert all(item["status"] == "done" for item in result["results"])
    assert agent.seen_parts
    assert _knowledge_parts(agent) == []
    events = await log.replay()
    assert all(event.type != "knowledge.injected" for event in events)


@pytest.mark.asyncio
async def test_knowledge_store_injects_untrusted_related_document_and_event(log, store):
    store.add(
        "pytest-runtime-doc",
        "pytest runtime knowledge injection sends untrusted context to workers",
        {"source": "notes"},
    )
    store.add("unrelated", "orchard apples pears harvest cider")

    agent, events = await _run_with_store(log, store, Config())

    knowledge_parts = _knowledge_parts(agent)
    assert len(knowledge_parts) == 1
    part = knowledge_parts[0]
    assert part.untrusted is True
    injected = part.data["knowledge"]
    assert injected[0]["doc_id"] == "pytest-runtime-doc"
    assert injected[0]["metadata"] == {"source": "notes"}
    assert "untrusted context" in injected[0]["text"]

    injected_events = [event for event in events if event.type == "knowledge.injected"]
    assert len(injected_events) == 1
    assert injected_events[0].payload["task_id"] == "t1"
    assert "pytest-runtime-doc" in injected_events[0].payload["doc_ids"]


@pytest.mark.asyncio
async def test_knowledge_store_privacy_strict_filters_sensitive_documents(log, store):
    store.add(
        "sensitive-doc",
        "pytest runtime knowledge injection sensitive roadmap",
        {"sensitive": True, "source": "secret"},
    )
    store.add("public-doc", "pytest runtime public fallback")

    agent, events = await _run_with_store(log, store, Config(privacy_strict=True))

    knowledge_parts = _knowledge_parts(agent)
    assert len(knowledge_parts) == 1
    doc_ids = [hit["doc_id"] for hit in knowledge_parts[0].data["knowledge"]]
    assert "sensitive-doc" not in doc_ids
    assert "sensitive-doc" not in [
        doc_id
        for event in events
        if event.type == "knowledge.injected"
        for doc_id in event.payload["doc_ids"]
    ]


@pytest.mark.asyncio
async def test_knowledge_store_privacy_relaxed_allows_sensitive_documents(log, store):
    store.add(
        "sensitive-doc",
        "pytest runtime knowledge injection sensitive roadmap",
        {"sensitive": True, "source": "secret"},
    )
    store.add("public-doc", "pytest runtime public fallback")

    agent, events = await _run_with_store(log, store, Config(privacy_strict=False))

    knowledge_parts = _knowledge_parts(agent)
    assert len(knowledge_parts) == 1
    doc_ids = [hit["doc_id"] for hit in knowledge_parts[0].data["knowledge"]]
    assert "sensitive-doc" in doc_ids
    assert any(
        event.type == "knowledge.injected" and "sensitive-doc" in event.payload["doc_ids"]
        for event in events
    )
