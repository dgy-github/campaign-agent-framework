"""M-E 神经 embedding 适配器测试（mock，无网络）。"""
import pytest
import httpx

from campaign.knowledge import LlmEmbedder, SqliteKnowledgeStore, dense_to_sparse
from campaign.llm.client import LLMClient, LLMError, TierConfig, extract_embeddings


def test_dense_to_sparse_l2_normalized():
    v = dense_to_sparse([3.0, 4.0])
    assert v == {"0": pytest.approx(0.6), "1": pytest.approx(0.8)}
    assert dense_to_sparse([]) == {}
    assert dense_to_sparse([0.0, 0.0]) == {}
    assert dense_to_sparse([5.0]) == {"0": pytest.approx(1.0)}


def test_extract_embeddings():
    resp = {"data": [{"embedding": [1.0, 2.0]}, {"embedding": [3.0, 4.0]}]}
    assert extract_embeddings(resp) == [[1.0, 2.0], [3.0, 4.0]]


@pytest.mark.asyncio
async def test_llm_embed_mock_and_exhaustion():
    client = LLMClient(
        tiers={"value": TierConfig(model="m", base_url="http://localhost")},
        mock_embeddings={"value": [[[1.0, 2.0], [3.0, 4.0]]]},
    )
    out = await client.embed(["a", "b"], "value")
    assert out == [[1.0, 2.0], [3.0, 4.0]]
    with pytest.raises(LLMError, match="exhausted"):
        await client.embed("a", "value")   # mock 用完 → 报错，不偷打真实 API
    await client.close()


@pytest.mark.asyncio
async def test_llm_embed_real_http_path_parses_embeddings():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"data": [{"embedding": [0.25, 0.75]}]},
        )

    client = LLMClient(
        tiers={"value": TierConfig(model="embed-model", base_url="http://llm.local", api_key="secret")},
    )
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        out = await client.embed("hello", "value", encoding_format="float")
    finally:
        await client.close()

    assert out == [[0.25, 0.75]]
    assert len(requests) == 1
    assert str(requests[0].url) == "http://llm.local/v1/embeddings"
    assert requests[0].headers["authorization"] == "Bearer secret"
    assert requests[0].read()
    assert requests[0].content


@pytest.mark.asyncio
async def test_llm_embedder_aembed_to_sparse():
    client = LLMClient(
        tiers={"value": TierConfig(model="m", base_url="http://localhost")},
        mock_embeddings={"value": [[[3.0, 4.0]]]},
    )
    emb = LlmEmbedder(client, "value")
    vec = await emb.aembed("hi")
    assert vec == {"0": pytest.approx(0.6), "1": pytest.approx(0.8)}
    await client.close()


def test_store_external_vector_path():
    # M-E：store 用预算向量检索（dense→sparse），相关向量得分更高
    store = SqliteKnowledgeStore(db_path=":memory:")
    store.add("doc1", "irrelevant text", vector={"0": 1.0, "1": 0.0})
    store.add("doc2", "irrelevant text", vector={"0": 0.0, "1": 1.0})
    hits = store.search(query_vector={"0": 1.0, "1": 0.0}, k=2)
    assert hits[0]["doc_id"] == "doc1"
    assert hits[0]["score"] > hits[1]["score"]


def test_store_query_vector_takes_precedence_over_query_text():
    store = SqliteKnowledgeStore(db_path=":memory:")
    try:
        store.add("lexical", "banana fruit smoothie")
        store.add("vector", "unrelated text", vector={"dense": 1.0})

        hits = store.search(query="banana", query_vector={"dense": 1.0}, k=2)

        assert hits[0]["doc_id"] == "vector"
        assert hits[0]["score"] > hits[1]["score"]
    finally:
        store.close()


def test_store_external_vector_does_not_pollute_tfidf_stats():
    store = SqliteKnowledgeStore(db_path=":memory:")
    try:
        store.add("external", "rareword vector only", vector={"dense": 1.0})
        assert store.embedder._N == 0
        assert store.embedder._df == {}

        store.add("tfidf", "python async event loop")
        assert store.embedder._N == 1
        assert "rareword" not in store.embedder._df

        hits = store.search("python async", k=2)
        assert hits[0]["doc_id"] == "tfidf"
        assert hits[0]["score"] > 0
    finally:
        store.close()


def test_store_default_tfidf_unchanged():
    # 不传 vector/query_vector = 原 TF-IDF 行为
    store = SqliteKnowledgeStore(db_path=":memory:")
    store.add("a", "python async event loop")
    store.add("b", "banana fruit smoothie")
    hits = store.search("async event", k=2)
    assert hits[0]["doc_id"] == "a"
