"""Retriever 检索（M2）——后勤：摘要 / 抽样 / 情报供给。

只发领域事件 retriever.result，生命周期事件由 Coordinator 管。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..core.models import Task
from .base import Agent

if TYPE_CHECKING:
    from ..llm.client import LLMClient
    from ..knowledge import KnowledgeStore


class Retriever(Agent):
    """检索器：摘要/抽样/情报供给。可接向量库 + RAG。

    当提供 *store* 时使用 TF-IDF 词法检索（非神经网络 embedding）；
    未提供时回退到原有 llm/stub 行为，保持向后兼容。
    """

    def __init__(
        self,
        *args: Any,
        llm: "LLMClient | None" = None,
        store: "KnowledgeStore | None" = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._llm = llm
        self.store = store

    async def handle(self, task: Task) -> dict[str, Any]:
        """检索/摘要并返回情报。

        RAG 路径 (store 非空): 将 task.goal 作为查询，在知识库中做
        TF-IDF 词法检索，返回 top-k 文档。检索结果标记为 untrusted。

        回退路径 (store 为空): 保持原有行为不变。
        """
        if self.store is not None:
            return await self._handle_with_store(task)
        return await self._handle_without_store(task)

    async def _handle_with_store(self, task: Task) -> dict[str, Any]:
        k = task.human_input.get("k", 5)
        filters = task.human_input.get("search_filters", {})
        hits = self.store.search(task.goal, k=k, filters=filters)

        parts = [
            f"[{h['doc_id']}] (score={h['score']:.4f}) {h['text'][:300]}"
            for h in hits
        ]
        summary = "\n".join(parts) if parts else "(no results)"

        documents: list[dict[str, Any]] = [
            {"doc_id": h["doc_id"], "score": h["score"], "metadata": h.get("metadata", {}), "untrusted": True}
            for h in hits
        ]

        result: dict[str, Any] = {"summary": summary, "documents": documents, "task_id": task.id}
        await self._emit("retriever.result", {"task_id": task.id, "doc_ids": [h["doc_id"] for h in hits], "hit_count": len(hits)})
        return result

    async def _handle_without_store(self, task: Task) -> dict[str, Any]:
        if self._llm:
            prompt = {"role": "user", "content": f"检索任务: {task.goal}\n请返回相关情报摘要。"}
            resp = await self._llm.complete(self.spec.model_tier, [prompt])
            from ..llm.client import extract_text
            summary = extract_text(resp)
        else:
            summary = f"[stub] no information retrieved for '{task.id}'"

        result: dict[str, Any] = {"summary": summary, "documents": [], "task_id": task.id}
        await self._emit("retriever.result", {"task_id": task.id, "summary_length": len(summary)})
        return result