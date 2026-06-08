# RAG —— 知识检索与回灌工程化

> 框架里 `Retriever` 角色和"知识回灌/数据飞轮"目前是 **stub / 设计态**。
> 本文是把它接成真实 RAG 的工程文档：怎么建库、怎么检索、怎么注入、怎么评测、怎么回灌。
> 与实现对齐：`campaign/roles/retriever.py`（待从 stub→真实）、`campaign/eval/`（检索评测）、`docs/multi-agent-framework-v5.md` ⑨ 数据飞轮。

## 0. RAG 在本框架的两个位置

1. **运行时检索（读侧）**：`Retriever` 角色按任务去知识库取情报，喂给 Coordinator/Executor 决策。
2. **复盘回灌（写侧 / 数据飞轮）**：把 Event Log 里的成功/失败案例 → 沉淀进知识库（few-shot）或训练集（蒸馏小模型）。读写合起来才是闭环。

---

## 1. 建库（离线索引）

| 环节 | 选型 / 要点 |
|---|---|
| **chunking** | 按语义/标题切，重叠 10–20%；代码按符号块。过大稀释、过小丢上下文 |
| **embedding** | 中文优先开源可本地：`bge-m3`(一模型出 dense+sparse+ColBERT，适合 Hybrid)、`bge-large-zh`；省事走 API：`text-embedding-3` / Voyage。先小模型测 Recall 再升级 |
| **向量库** | 起步 SQLite/`sqlite-vec` 或 FAISS；规模上 Qdrant/Milvus/pgvector |
| **元数据** | 每 chunk 存来源 ID、时间、敏感标记(`sensitive`)、版本，便于过滤+引用+隐私管控 |

> 隐私红线：标 `sensitive` 的内容只用**本地 embedding + 本地向量库**，绝不送云（督军 `DataEgressRule` 把关）。

## 2. 检索（运行时，读侧）

推荐管线（按需裁剪）：

```
query → query 改写/扩写(可选) → Hybrid 检索(dense + BM25/sparse)
      → rerank(cross-encoder/ColBERT MaxSim, top-k→top-n)
      → 上下文裁剪(字段白名单/摘要/去重) → 注入 prompt(带引用 ID)
```

- **query 改写**：把口语任务改写成检索友好 query（多查询/HyDE 视价值开启）。
- **Hybrid**：稠密召回语义、稀疏(BM25)兜专有名词/代码符号；`bge-m3` 可一站式。
- **rerank**：召回宽(top-50)、精排窄(top-5)，是性价比最高的一档提升。
- **注入**：只塞决策必需信息，保留 `doc_id` 供追溯；不要把窗口当日志桶。

## 3. 接进 `Retriever`（从 stub 到真实）

`campaign/roles/retriever.py` 当前：无 llm 时返回 `[stub] ...`。真实化建议：

- 注入一个 `KnowledgeStore` 适配器（`search(query, k, filters) -> list[Doc]`），**核心层保持零依赖**，真实实现放 adapter（同 `LLMClient` 模式）。
- `handle(task)`：`docs = store.search(task.goal, filters={sensitive_ok: is_local})` → 组织 summary + `documents`(带 doc_id) → 发 `retriever.result`(已实现) 带 `summary_length`/`doc_ids`。
- **降级**：向量库不可用 → 退回 BM25/关键词；再不行返回空摘要并标记，让上层决定冻结还是继续（呼应 RUNBOOK §5 降级）。

## 4. 检索评测（接 eval 门禁）

把检索质量纳入 `campaign/eval/`：

| 指标 | 说明 |
|---|---|
| Recall@k / MRR / nDCG | 召回与排序质量（需 golden 标注集） |
| 命中率 / 引用准确 | 答案是否真用到检索内容、引用是否对得上 |
| 成本/延迟 | 每次检索 token/ms，进可观测层 `tracer.meter` |

- 检索 eval 不达阈值 → 同 RUNBOOK §2，**禁止上线**。
- 回归集随 badcase 增长，避免"改好一个坏一片"。

## 5. 回灌闭环（写侧 / 数据飞轮）

```
运行 Event Log → 复盘提炼(成功/失败案例) → 写知识库(few-shot 可检索)
                                         └→ 导出训练集 jsonl → 蒸馏小模型(预备队更能打)
```

- 已有钩子：`EvalHarness.export_trainset_async(log, out)` 按 task 聚合成功/失败样本导出 jsonl。
- 成功案例 → Golden Case 库（运行时 RAG few-shot 注入）；失败案例 → 恢复预案库。
- 成功率统计反向更新 ROI 路由信号（`Router.success_rate`，已从 Event Log 聚合）。
- 蒸馏落地可对接 OpenClaw-RL（"对话即训练"），见框架文档 ⑨。

## 6. 最小落地顺序（建议）

1. `KnowledgeStore` 适配器接口 + 一个本地实现(sqlite-vec/FAISS) + bge-m3 embedding。
2. `Retriever.handle` 接 store（带 sensitive 过滤 + 降级）。
3. 检索 eval（Recall@k）进 `campaign/eval/`，纳入门禁。
4. `export_trainset` → 知识库回灌 + few-shot 注入 Coordinator。
5. （可选）蒸馏小模型，更新预备队。

> 注：资料包里的 RAG **面试**内容（`interview-rag-mindmap.html`、`interview-schedule-tech-deep.html`）可作选型参考，但本文是**框架运行态**的 RAG 工程文档，定位不同。
