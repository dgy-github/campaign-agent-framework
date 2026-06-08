import pytest

from campaign.core.events import SqliteEventLog
from campaign.core.models import AgentSpec, Task
from campaign.knowledge import SqliteKnowledgeStore, TfidfEmbedder, cosine
from campaign.roles.retriever import Retriever


def test_tfidf_embedder_ranks_related_document_above_unrelated():
    embedder = TfidfEmbedder()
    related = "python pytest unit tests assertions fixtures"
    unrelated = "orchard apples pears harvest cider"

    embedder.fit_add(related)
    embedder.fit_add(unrelated)

    query_vec = embedder.embed("pytest assertions for python tests")
    related_score = cosine(query_vec, embedder.embed(related))
    unrelated_score = cosine(query_vec, embedder.embed(unrelated))

    assert related_score > unrelated_score
    assert related_score > 0


def test_sqlite_knowledge_store_search_respects_ranking_k_and_sensitive_filter():
    store = SqliteKnowledgeStore()
    try:
        store.add("public-rag", "retrieval augmented generation uses documents", {"source": "public"})
        store.add("public-sql", "sqlite stores durable rows on disk", {"source": "public"})
        store.add("secret-rag", "retrieval augmented generation secret roadmap", {"sensitive": True})

        public_hits = store.search("retrieval augmented generation", k=5)

        assert [hit["doc_id"] for hit in public_hits] == ["public-rag", "public-sql"]
        assert all(hit["doc_id"] != "secret-rag" for hit in public_hits)
        assert len(store.search("retrieval augmented generation", k=1)) == 1

        all_hits = store.search(
            "retrieval augmented generation secret roadmap",
            k=5,
            filters={"sensitive_ok": True},
        )

        assert all_hits[0]["doc_id"] == "secret-rag"
        assert any(hit["doc_id"] == "public-rag" for hit in all_hits)
    finally:
        store.close()


def test_sqlite_knowledge_store_reopens_same_database_and_searches(tmp_path):
    db_path = tmp_path / "knowledge.db"
    first = SqliteKnowledgeStore(db_path=str(db_path))
    try:
        first.add("durable", "persistent sqlite knowledge survives process restart")
    finally:
        first.close()

    second = SqliteKnowledgeStore(db_path=str(db_path))
    try:
        hits = second.search("sqlite persistent restart", k=1)

        assert hits
        assert hits[0]["doc_id"] == "durable"
    finally:
        second.close()


@pytest.mark.asyncio
async def test_retriever_with_store_returns_documents_marked_untrusted(tmp_path):
    store = SqliteKnowledgeStore()
    log = SqliteEventLog(db_path=str(tmp_path / "events.db"))
    try:
        store.add("rag-doc", "retriever uses sqlite tfidf knowledge store", {"kind": "note"})
        retriever = Retriever(
            AgentSpec(id="ret-1", role="retriever", model_tier="value", skills=["search"]),
            log,
            store=store,
        )

        result = await retriever.handle(Task(id="t1", goal="sqlite tfidf retriever", human_input={"k": 1}))

        assert result["task_id"] == "t1"
        assert len(result["documents"]) == 1
        document = result["documents"][0]
        assert document["doc_id"] == "rag-doc"
        assert document["score"] > 0
        assert document["metadata"] == {"kind": "note"}
        assert document["untrusted"] is True
        assert "rag-doc" in result["summary"]

        events = await log.replay()
        assert events[-1].type == "retriever.result"
        assert events[-1].payload["doc_ids"] == ["rag-doc"]
    finally:
        store.close()
        log.close()


@pytest.mark.asyncio
async def test_retriever_without_store_keeps_stub_behavior(tmp_path):
    log = SqliteEventLog(db_path=str(tmp_path / "events.db"))
    try:
        retriever = Retriever(
            AgentSpec(id="ret-1", role="retriever", model_tier="value", skills=["search"]),
            log,
        )

        result = await retriever.handle(Task(id="t1", goal="find background"))

        assert result == {
            "summary": "[stub] no information retrieved for 't1'",
            "documents": [],
            "task_id": "t1",
        }
    finally:
        log.close()
