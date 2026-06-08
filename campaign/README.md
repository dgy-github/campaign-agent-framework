# campaign —— 多 Agent 框架参考实现

「指挥—运行—演练 多 Agent 框架」的**可运行参考实现**。核心层零外部依赖（仅 `pydantic`；HTTP 传输用 `httpx`），事件溯源驱动，**172 测试全绿**。

- 概念/方法论：[../docs/multi-agent-framework-v5.md](../docs/multi-agent-framework-v5.md)
- 运行态手册：[../docs/RUNBOOK.md](../docs/RUNBOOK.md) ｜ 检索/RAG：[../docs/RAG.md](../docs/RAG.md) ｜ 分布式/A2A：[../docs/SCALING.md](../docs/SCALING.md) / [../docs/A2A.md](../docs/A2A.md)
- 架构图（实现视图）：[../campaign-architecture.html](../campaign-architecture.html)

## 快速开始

```bash
pip install -e ".[dev]"
pytest -q                              # 全量测试（172）
python -m campaign                     # 最小端到端 demo
python -m campaign.examples.capstone_demo      # 全能力集成 demo
python -m campaign.examples.distributed_demo   # 跨节点 A2A（HTTP + 共享事件库）
```
> Windows 上若 `python` 是 Store 占位，用真实解释器：
> `cmd /c "cd /d <repo>\campaign && set PYTHONPATH=<repo> && <python.exe> -m pytest -q -p no:cacheprovider"`

## 能力总览

| 领域 | 能力 |
|---|---|
| **事件溯源** | `EventLog`(SQLite, append-only, 跨进程可共享)；状态由 `derive_state(events, run_id)` 派生；**run_id 隔离**（多次运行/换设备互不混流） |
| **角色** | Coordinator / Executor / Retriever / Reviewer / Reserve（裁判-运动员分离） |
| **路由** | 能力注册表 + ROI 路由（置信度/成本/历史成功率，同分确定性） |
| **并发** | `set_concurrency(n)`（Semaphore+gather）+ DAG `depends_on` 拓扑分层；共享账本由 PolicyGate 锁保护 |
| **治理** | Governor + `PolicyGate`（统一执行闸）+ Policy-as-Code（预算/数据出域/越权/注入扫描）；runtime 每任务 + LLMClient 每次调用都过闸 |
| **韧性** | 熔断 + 协同制动(背压) + 检查点/真回滚 + 动员减员(三级, 真实 health 驱动) + 小模型预备队 |
| **可观测** | Tracer(span/meter/score)，可对接 Langfuse/OTel |
| **A2A 通信** | Message/Part/AgentCard 协议；Transport：InProcess + HTTP/JSON-RPC（重试/退避/鉴权/**幂等去重**）+ **SSE 流式**(`message/stream`) + **能力发现**(`agent/cards`) + RemoteAgentProxy；分布式 MVP（跨节点 + 共享事件库） |
| **安全** | 发送方 allowlist 防伪、`Part.untrusted` 标记、注入扫描、敏感数据本地隔离 |
| **HITL** | `input_required`/`needs_approval`/`Runtime.resume`（**事件溯源、跨进程可恢复**） |
| **记忆三层** | 短期 `ScratchpadMemory` / 中期 `SessionMemory`(按 run_id 读 EventLog) / 长期 `SqliteKnowledgeStore`(TF-IDF RAG，留神经 embedding 接口缝)；均可经 `enable_memory`/`set_knowledge_store` 接进 runtime 主链 |
| **成本** | 真实 token 计量（`LLMClient.embed`/usage → 预算入账） |
| **演练** | `EvalHarness` 门禁（不达阈值禁上线）+ `ChaosDrill` 随机减员 + 数据飞轮导出 |

## 模块地图

| 目录/文件 | 职责 |
|---|---|
| `core/events.py` `state.py` `models.py` | 事件溯源 / 派生状态 / 数据模型 |
| `protocol.py` `transport.py` | A2A 协议 / 传输（InProcess+HTTP+SSE+发现）+ JsonRpcAgentServer + RemoteAgentProxy |
| `roles/` | base(on_message/ask) + coordinator/executor/retriever/reviewer/reserve |
| `routing/` | skill_registry + router(ROI) |
| `governance/` | policy + governor + gate(PolicyGate) |
| `resilience/` | breaker + checkpoint + mobilization |
| `observability/tracer.py` | span/meter/score |
| `llm/client.py` | OpenAI 兼容适配器（chat/embed，mock + gate） |
| `memory.py` `knowledge.py` | 短/中期记忆 + 长期 RAG 知识库 |
| `eval/` | harness(门禁) + chaos(演练) |
| `app/runtime.py` `config.py` | 装配 + 主执行 run()/resume()/discover_remote() + 部署/隐私配置 |
| `examples/` | demo / capstone_demo / distributed_demo |
| `tests/` | 172 测试 |

## 设计约束
- 核心层只依赖 `pydantic` + 标准库；HTTP 用 `httpx`；外部组件（Hermes/OpenClaw/LangGraph/Langfuse）一律 adapter 接入。
- 所有进阶能力**默认关闭 / 可选**（opt-in），不破坏最小链路。
- 诚实边界：长期记忆是词法 TF-IDF（非神经）；分布式是单进程共享 SQLite 的 MVP（预算/熔断/health/leader 仍每进程，详见 SCALING.md）。
