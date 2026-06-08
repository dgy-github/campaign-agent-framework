# 项目工作日志 / 上下文记忆（WORKLOG）

> 目的：把"开发阶段的上下文记忆"从易失的对话里**沉淀成仓库文件**，
> 这样换任何设备（平板/手机/另一台电脑）`git pull` 就能续上，不依赖某个 agent 的会话。
> 维护约定：每完成一段工作，在顶部追加一条「日期 + **执行 agent** + 做了什么 + 为什么 + 下一步」。
>
> **Agent 标签**（多 agent 协作必须标，便于跨设备分清责任）：
> - `[Claude]` — 规划 / 决策 / 验收 / Claude 直接改
> - `[CodeWhale/DeepSeek]` — 便宜杂活执行（`codewhale exec --auto`）
> - `[Codex]` — 重活执行（codex-runner，官方签名）
> - `[human]` — 人工手动
> - 不确定来源标 `[外部/待确认]`，不要瞎归属。

---

## 变更日志（最新在上）

- **`[Claude]` 文档刷新（消文档漂移）** — 发现真缺口：`campaign/README.md` 完全停在 M0–M8 骨架期（"实现待填"，A2A/记忆/分布式/流式/发现/HITL/DAG 一个没提）、`campaign-architecture.html` 缺 HttpJsonRpc 真实化/SSE/发现/分布式/记忆且测试数停在 99。代码已 42 模块 172 测试。**重写 `campaign/README.md`**（能力总览 + 模块地图 + 三个 demo 跑法 + 诚实边界）；**更新架构 HTML**（§4 传输补 HTTP/SSE/发现/幂等/RemoteAgentProxy/分布式 MVP、§5 加记忆三层+HITL 行、§6 模块地图刷新+测试数→172、§7 加 12+ 新能力 chips + 更新诚实边界）。纯文档、无代码改动、不影响测试。下一步：push 上云。


- **M1 send() 复用 _post_rpc（消债）+ M2 A2A SSE 流式** — `[Claude]`规划→`[DeepSeek]`实现→`[Codex]`测试→`[Claude]`验收。计划 `send-dry-and-sse-plan.md`。M1：`_post_rpc` 改为返回 JSON-RPC dict 或 `{_transport_error}` sentinel（永不抛），`send`/`discover` 共用同一 auth/重试/退避路径，send 行为逐字等价。M2：新增 A2A `message/stream`——server `handle_stream`(yield `status(working)`→路由→`result`/`error`) + `asgi_app` SSE(`text/event-stream`, `data: <json>\n\n` chunked) + client `send_stream()`(流式 POST 解析 SSE，错误 yield error 事件不抛，流式不重试)；`Transport` 基类默认 send_stream。Codex 加 `test_streaming.py`(3 测试，实现一次过)：status→result、目标缺失 error、流式 auth。Claude 复跑 **172 测试通过**、无新依赖、无循环 import、无残留 xml、向后兼容。下一步：push 上云。


- **Capstone 端到端 demo（全家桶集成验证）** — `[Claude]`规划→`[DeepSeek]`实现→`[Codex]`测试→`[Claude]`验收(亲跑)。计划 `capstone-demo-plan.md`。新增 `examples/capstone_demo.py`(~350行) + `examples/__init__.py`：一次 run 串起 **5 角色装配 + Governor(4规则) + CircuitBreaker + Mobilizer + InMemoryTracer + 并发(2) + enable_memory(session+scratch) + KnowledgeStore(RAG) + DAG(depends_on) + 预算 + EvalHarness 门禁**。实跑：t0/t1 并发、t2 等 t1，全 done、eval PASS 0.82、零 incident/violation。Codex 加 `test_capstone.py`(2 冒烟测试) + 给 main() summary 补 `event_counts`（demo 删临时 log 后供断言）。Claude 复跑 **169 测试通过** + 亲跑 demo 确认。`python -m campaign.examples.capstone_demo` 可独立运行。无新依赖、无循环 import、无残留 xml。**至此框架全部已实现能力有了一个可跑的集成示例。** 下一步：push 上云 / 消 send() 小债 / SSE 流式。


- **A2A 能力发现（Agent Card discovery）** — `[Claude]`规划→`[DeepSeek]`实现→`[Codex]`测试→`[Claude]`验收。计划 `a2a-discovery-plan.md`。server `JsonRpcAgentServer.handle_rpc` 加 `agent/cards` 方法（复用 auth，返回托管 agent 的 AgentCard 列表）；client `HttpJsonRpcTransport.discover()`（复用 `_post_rpc` 的 auth/重试，错误返 []）；`Runtime.discover_remote(transport)` 拉取并自动 `register_remote`——不再手动硬编码远程 spec。Codex 加 `test_discovery.py`(5 测试，实现一次过)：server 列卡/client discover/端到端自动注册路由/auth/向后兼容。Claude 复跑 **167 测试通过**、无新依赖、向后兼容。**顺手清理**：仓库根 5 个之前 Codex 跑 junitxml 残留(`pytest-*.xml`)删除 + `.gitignore` 加 `pytest-*.xml` 防再犯。小债：`send()` 未复用 `_post_rpc`（仍各自重试循环，非 bug）。下一步：push 上云。


- **长期记忆(KnowledgeStore)接进 Runtime 主链（RAG 上下文注入）** — `[Claude]`规划→`[DeepSeek]`实现→`[Codex]`测试→`[Claude]`验收。计划 `knowledge-into-runtime-plan.md`。`Runtime.set_knowledge_store(store,k=3)` opt-in；启用后 `_execute_one_task` 按 `task.goal` 检索（`filters={"sensitive_ok": not privacy_strict}` 隐私过滤），把命中文档作为 **untrusted** part 注入发给 worker 的 Message（追加，不动 task/parts[0]），并发 `knowledge.injected` 审计事件。默认不调用=零回归。Codex 加 `test_knowledge_runtime.py`(4 测试，实现一次过)：默认无注入无事件、启用后注入相关 doc 且 untrusted、privacy_strict 过滤 sensitive。Claude 复跑 **162 测试通过**、无新依赖、向后兼容。**至此记忆三层(短/中/长)全部接进 runtime 主链。** 遗留：knowledge 注入在选 agent 前发生（任务若 frozen 也会先检索，非阻塞）。下一步：push 上云。


- **A2A HTTP 传输加固（重试/退避/鉴权/幂等）** — `[Claude]`规划→`[CodeWhale/DeepSeek]`实现→`[Codex]`测试→`[Claude]`验收。计划 `a2a-http-hardening-plan.md`。`HttpJsonRpcTransport` 加 `retries/backoff/auth_token`（仅瞬时错误 502/503/504/429/网络/超时重试、指数退避、4xx 不重试、耗尽返 error Message 不抛）；`JsonRpcAgentServer` 加**幂等去重**（按 message_id 的 OrderedDict 缓存，重复投递不重复执行 worker）+ 可选 Bearer 鉴权，`asgi_app` 透传 headers。补 SCALING 列的"无重试/无鉴权/无幂等"缺口。Codex 加 `test_http_hardening.py`(8 测试) 并**修了个安全漏洞**（配了 auth_token 但 headers=None 时原会跳过鉴权→改为缺 header 也拒 -32001）。Claude 复跑 **158 测试通过**、无新依赖、向后兼容。遗留：幂等仅单进程内存（跨进程去重需共享存储，SCALING 已注）。下一步：push 上云。


- **`[Codex]` 桥接确认 + E/F/G 测试复核补全（`[Claude]`规划+验收）** — sandbox 修(unelevated)后首个真实 codex-runner 任务，**桥接确认正常**（Codex 读/改/跑全程通、无 spawn 错）。规划 `efg-review-bridge-confirm-plan.md`。Codex 复核 E/F/G 实现 + 三测试文件，**+9 用例**（真实 HTTP embedding 解析/query_vector 优先级/外部 vector 不污染 TF-IDF/memory 两开关单独+并发/ask dict query+目标异常）+ **2 处最小修复**（`base.py ask(query)` str→object 支持 dict；`tracer.py` span 改 perf_counter 避免 0ms 偶发不稳）。Claude 独立复跑 **150 测试通过**。三方流水线（Claude规划/DeepSeek实现/Codex测试修复/Claude验收）完全恢复。下一步：push 上云。


- **`[Claude]` 修复 codex sandbox（保沙箱）** — codex-runner 测试棒 6/5 起报 `windows sandbox: spawn setup refresh`，全 exec 卡死。根因：codex 6/4 更新把 `~/.codex/config.toml` 的 `[windows] sandbox` 设为 `elevated`（要 UAC 提权，codex-runner 无头/后台启动触发不了 → setup helper spawn 立即失败；read-only/workspace-write 都走该后端故全废）。重启桌面端无效。修法：合法值只有 `elevated`/`unelevated`，改成 **`unelevated`**（受限令牌沙箱，无头可用且仍隔离）。验证：`codex exec --sandbox workspace-write` 无头跑通（与 run.ps1 等价）→ codex-runner 恢复。注意：此为**全局** codex 配置（桌面端也改用 unelevated）；要回 elevated 则 codex-runner 又会失效。`danger-full-access` 是备选（绕过沙箱，未采用）。


- **E 神经embedding适配器 / F 三层Memory接入runtime(opt-in) / G 询问式ask（新流水线第3单）** — `[Claude]`规划→`[CodeWhale/DeepSeek]`实现→**Codex 测试棒两次被自身 sandbox 故障阻断(`windows sandbox: spawn setup refresh`)→`[Claude]`顶替写测试+修复+验收**。E：`LLMClient.embed()`+`mock_embeddings`+`extract_embeddings`、`knowledge.dense_to_sparse`+`LlmEmbedder.aembed`、`SqliteKnowledgeStore.add(vector=)/search(query_vector=)`（默认 TF-IDF 不变）。F：`Runtime.enable_memory(session,scratch)` opt-in 默认关；启用后 `SessionMemory.recall` 注入后续任务消息(`session_context` part, untrusted=False)、scratch 写 `result["scratchpad"]`；**默认关=零回归**。G：`Agent.ask(to,query,transport)` 经传输取数、标 untrusted、不崩；`protocol.Message.kind` 加 `"query"`。**Claude 验收抓修 DeepSeek 真 bug：`base.py ask()` 用 `Part` 未 import（NameError）**。最终 **142 测试通过**（132→+10：test_embeddings/test_ask/test_memory_runtime）、demo 正常、无新依赖、无循环 import。备注：codex-runner 这台 sandbox 当前不稳，测试棒临时由 Claude 兜。下一步：push 上云 / 修 codex sandbox。


- **长期记忆层 = Retriever 接最小真实 RAG（新流水线第2单）** — `[Claude]`规划→`[CodeWhale/DeepSeek]`实现→`[Codex]`测试→`[Claude]`验收。新增 `campaign/knowledge.py`(372行)：`Embedder` ABC + `TfidfEmbedder`(纯Python TF-IDF, CJK单字+拉丁分词, L2归一) + `cosine` + `KnowledgeStore` ABC + `SqliteKnowledgeStore`(sqlite持久, WAL, sensitive过滤, **跨进程重开同库可恢复**)；`Retriever` 加可选 `store`，有store走真实检索(返回 documents+标 `untrusted`)、无store保持stub向后兼容。Codex 加 `test_knowledge.py`(5测试，实现一次过无需修)。Claude 验收 **132 测试通过** + 确认 knowledge.py 是干净 UTF-8（Codex 报的"乱码"是其 GBK 终端显示误报）。**诚实边界：词法 TF-IDF 检索，非神经 embedding（留 Embedder 接口缝）；store 未强接进 runtime 默认装配**。至此记忆三层：短期✅ 中期✅ 长期✅(最小)。下一步：push 上云 / 神经 embedding 或 store 接进 runtime。


- **`[Claude]规划 + [CodeWhale/DeepSeek]执行 + [Codex]测试 + [Claude]验收` — 确立新流水线 + 三层 Memory 短/中期** — **新协作流水线（自此为准）：Claude 规划 → DeepSeek/CodeWhale 写实现 → Codex 写测试+跑 pytest+修复到绿 → Claude 最终验收**（Codex 从主执行退为测试/修复/辅助，DeepSeek 重新上场做执行）。首单：`campaign/memory.py` 由 DeepSeek 实现（`ScratchpadMemory` 短期工作记忆 + `SessionMemory` 中期情节记忆：按 run_id 读 EventLog、跨进程可读），Codex 加 `test_memory.py`(4 测试，本轮无 bug 需修)，Claude 复跑 **127 测试通过**。规划 `memory-3tier-shortmid-plan.md`。非目标（留后续）：长期层 RAG/Retriever 真实化；Memory 接进 runtime。下一步：长期记忆层 / push 上云。


- **`[Codex]` 分布式最小可用（`[Claude]` 规划+验收）** — 把 HTTP 传输用起来凑成跨节点 MVP。计划 `distributed-min-viable-plan.md`。新增 `RemoteAgentProxy`（节点A按 spec 选远程 agent，本地不执行）+ `Runtime.register_remote` + `examples/distributed_demo.py` + 更新 SCALING.md（MVP 已实现 + 诚实列出仍未分布式化项）。验收：节点A 经 `HttpJsonRpcTransport` 把任务派到节点B 的 `JsonRpcAgentServer` 执行，**两节点共享同一 SQLite 事件库**，统一 `derive_state` 还原状态；demo 实跑显示 A(task.assigned/done/review) 与 B(executor.output/a2a.message) 事件交织在一条共享流。**123 测试通过**（119→+4）、无新依赖、无循环 import、默认 Runtime 仍 InProcess。**关于 Postgres**：故意不做（新依赖+无库可测=假交付），SCALING.md 标注预算/熔断/health/mobilizer leader 仍每节点、SQLite 仅适合低并发。下一步：push 自建 GitLab。


- **`[Codex]` A2A HTTP/JSON-RPC 传输（`[Claude]` 规划+验收）** — 把 stub 做成真实远程传输。计划 `a2a-http-transport-plan.md`。`HttpJsonRpcTransport.send`：Message→JSON-RPC 2.0(`model_dump(mode=json)`)→POST→解析 result/error→`Message.model_validate`，异常转 error Message；新增 `JsonRpcAgentServer.handle_rpc`(校验 jsonrpc/method/params，**复用 InProcessTransport 做路由+信任校验+审计**，语义与本地一致) + 极简 `asgi_app`。测试用 httpx MockTransport/ASGITransport 不开端口。验收：**119 测试通过**（112→+7，Codex 用 --junitxml 确认 0 失败，我又独立复跑确认）、demo 正常、无新依赖（仅 httpx）、无循环 import。**意义：换上 HttpJsonRpcTransport 即可跨进程/跨机 A2A，落地 SCALING ②的传输层一环。** 下一步：push 自建 GitLab。


- **`[Codex]` HITL 跨进程持久 resume（`[Claude]` 规划+验收）** — 补上上一轮遗留的"换设备续作"缺口。计划 `hitl-durable-resume-plan.md`。`task.input_required` 事件现持久 `task`(序列化)+`budget`；`resume()` 先走内存快路径，未命中则**回放 EventLog → derive_state 校验仍 awaiting → 从最近 input_required 事件重建 Task** 再恢复执行。验收：全新 `SqliteEventLog`(同库) + 全新 Runtime 能 resume 成功（测试真模拟跨进程），幂等（已恢复/不存在→not_found）。**112 测试通过**（110→+2），demo 正常，无新依赖。这下 HITL 暂停的任务也能"git pull 后在另一设备继续"。下一步：push 自建 GitLab。


- **`[Codex]` 深层特性 A-B-C-D（`[Claude]` 规划+验收）** — 计划 `deep-features-abcd-plan.md`，Codex 顺序实现，Claude 独立验收并**抓修 1 个 Codex 漏检的测试 bug**（test_cost 用错 key `agent_id`→`actor`）。A 信任安全：A2A 发送方 allowlist 防伪 + `Part.untrusted` + `InjectionScanRule` 注入扫描 + 拒伪审计 `a2a.rejected`。B HITL：`input_required`/`needs_approval`/`Runtime.resume`/`task.resumed` + `tasks_awaiting_input`。C 依赖 DAG：`Task.depends_on` + 存在性/环校验 + 拓扑分层执行 + 依赖失败 `task.skipped`。D 真实成本：`extract_usage` + LLMClient 响应后按真实 token 入账/meter。验收：**110 测试通过**（99→+11）、demo 正常、无循环 import、无新依赖。**遗留（Codex 诚实标注）：HITL resume 目前是进程内索引，跨进程/换设备未持久**——与"git pull 续作"语义有差，待后续用事件溯源补。


- **`[Codex]` A2A 协议层（`[Claude]` 规划 + 验收）** — 由 Claude 写计划(`a2a-protocol-plan.md`)、Codex 执行、Claude 独立复核。新增 `campaign/protocol.py`(Message/Part/AgentCard/TaskState，对齐开放 A2A)、`campaign/transport.py`(Transport ABC + InProcessTransport + HttpJsonRpc stub)、`docs/A2A.md`；改 `roles/base.py`(加 `on_message`)、`app/runtime.py`(`set_transport` + 默认 InProcess + dispatch 改走 `Message.request→transport.send`)。意义：换 Transport 实现即可 in-process→分布式，不动编排（接 SCALING ②）。验收：**99 测试通过**(91 不回归 + 8 新增)、无循环 import、demo 正常、无新依赖。**这是 codex-runner 修复后首个真正由 Codex 落地的任务。** 下一步：push 自建 GitLab。


- **`[Claude]` 深层加固 ①③ + 文档 ②** — ①真并行：`Runtime.set_concurrency(n)`(Semaphore+gather，默认1=串行)，共享预算账本经统一执行闸的 `asyncio.Lock` 并发安全。③全链路治理：新增 `governance/gate.py::PolicyGate`(Governor+锁)收口，runtime 每任务 + `LLMClient` **每次调用**都过闸(spend/data_egress)；AuthorityRule 给会发起调用的角色补 data_egress 权限。②跨进程/分布式：诚实写成 `docs/SCALING.md`(逐组件未实现边界 + 落地顺序 + 5 条不变量)，**不在测试里假装分布式一致**。验收：**91 测试通过**(并发预算 race-safe、并行正确性、LLM 被治理拦截) + demo 正常。下一步：push 自建 GitLab。


- **`[Claude]` 边界加固（两轮）** — 浅层(10项)：decompose 容错校验错、State 加 failed 集、rollback 真分叉(`truncate_after`+`hard`)、事件总线丢弃计数告警、ExecutionOrder 重复 id 校验、mock 耗尽报错不偷打真 API、no-reviewer 可 fail-closed、strictness 钳制、artifact 非 dict 守卫、Router 同分确定性。结构层(7项)：**run_id 隔离**（多次运行不混流，`runtime.state(run_id)`/`derive_state(run_id=)`）、**预算治理真生效**（按 tier 估价入账，超预算真拦）、**减员按真实 health 驱动**（失败拉低 health→assess 升级）、**单任务超时**(`set_task_timeout`)、熔断状态纯内存(去无界 replay)、reviewer 兼容 retriever 的 summary、广播快照迭代。原因：此前安全子系统"接了线但没被真实信号驱动"+缺 run 隔离/超时。验收：**88 测试通过**（新增 test_boundaries.py）+ demo 正常。下一步：push 自建 GitLab。


- **`[Claude]` 新增运行态执行文档** — 创建 `docs/RUNBOOK.md`（各运行态：制动/熔断/三级减员/冻结/督军拦截/检查点回滚/事故响应，按"触发→行为→人工→恢复"，与 campaign 事件名对齐）+ `docs/RAG.md`（检索/回灌工程化、Retriever stub→真实路径、检索评测进门禁、数据飞轮）。原因：此前只有设计/施工文档，缺运行态手册和 RAG 工程化。下一步：代码+记忆 push 到自建 GitLab；RAG 真实实现待排期。

## 项目速览

三层结构（详见根 [README.md](../README.md)）：
- 📚 面试资料包（HTML，离线）
- 🧭 框架方法论：`docs/multi-agent-framework-v5.md`（指挥—运行—演练 多 Agent 框架 v5）+ `docs/deepseek-build-plan.md`（M0–M8 施工拆解）
- 🛠️ 参考实现：`campaign/`（Python 包，72 测试通过，`python -m campaign` 可跑 demo）

## 工具链与分工（长期约定）

| 工具 | 用途 | 调用 |
|---|---|---|
| **codex-runner**（MCP，官方签名 CLI） | 重要 / 需可靠性的改动 | 写计划 → `start_codex_with_plan` |
| **CodeWhale** `exec --auto`（DeepSeek，便宜，可本地） | 批量小杂活 | `& "D:\CodeWhale\bin\codewhale.exe" exec --auto -C <repo> "<task>"` |
| **真 Python** | 跑测试 | `C:\python-embed\python.exe`（3.11.9，已装 pydantic/pytest） |

原则：**便宜的干活、贵的把关，把关不降级**。CodeWhale 跑不了自己的 Python 验证，其产出**必须人工/真 pytest 复核**。

跑测试（存底）：
```
cmd /c "cd /d D:\agent-interview-kit\campaign && set PYTHONPATH=D:\agent-interview-kit && C:\python-embed\python.exe -m pytest -q"
```

## 关键决策记录

- **框架去对抗化**：剔除"诡道/示形/奇正"等零和概念，改协作工程隐喻（有目标、有约束、无敌人）。
- **架构定型（v5）**：三时态（演练/运行/复盘）+ 四层（意图/运行时/演练/闭环）+ 横切（治理 督军 / 部署隐私容灾）。
- **运行时**：Event Log 事件溯源取代可变 Blackboard；裁判(Reviewer)/运动员(Coordinator)分离；ROI 路由（置信度+成本+成功率）。
- **抗毁性**：韧性(熔断/检查点) + 协同制动(背压防雪崩) + 互助补位 + 动员减员(三级响应) + 通用预备队用**小模型**(执行可降级、把关不降级、高难宁冻结)。
- **治理**："制度治理没有爱" = Policy-as-Code，督军独立监察、对所有角色一视同仁。
- **组件选型**：工具层 Hermes(function-calling)；技能/隐私底座 OpenClaw；治理 ClawKeeper；训练飞轮 OpenClaw-RL；可观测 Langfuse/OTel；编排 LangGraph。

## 当前状态（截至本条）

- 分支 `feature/multi-agent-framework`，commit `e8e4a15`：campaign/ + docs/ + README + .gitignore + 框架 HTML + index 入口。
- `campaign/` M0–M8 全实现且接线（含 M7 可观测性 tracer 已接进 runtime），**72 测试通过**，demo 覆盖 executor/reviewer/retriever + 事件溯源 + 派生状态 + 可观测性。
- codex-runner 桥接已修复（旧进程加载旧代码问题，重启 MCP 即可）。

## 贡献归属（谁做的）

| 产物 / 改动 | 执行 agent |
|---|---|
| 框架方法论 v5、build-plan、README、WORKLOG、CLAUDE.md、框架 HTML、index 入口、.gitignore | `[Claude]` |
| `campaign/` 骨架接口 stub（M0–M8 占位、数据模型） | `[Claude]` |
| `campaign/` M0–M8 具体实现 + 测试套件（test_models / test_integration） | `[外部/待确认]`（在会话间被填入，非本会话 Claude/CodeWhale 所写） |
| `reserve.py` 补 `task.frozen`、`reviewer.py` try/except + 事件改名（reviewer.verdict→review.done） | `[CodeWhale/DeepSeek]` |
| `examples/demo.py` + `__main__.py` | `[CodeWhale/DeepSeek]` |
| `_review` guard + 文档一致性修复、M7 tracer 接线、Retriever 进 demo、demo 接 tracer | `[Claude]` |
| 全部 commit / 密钥扫描 / 真 pytest 验收背书 | `[Claude]` |

> 注：`campaign/` M0–M8 实现的确切作者本会话无法确认（曾试图用 codex-runner 但当时桥接故障无产物），故标 `[外部/待确认]`；后续若查清来源请更正本表。

## 已知遗留 / 下一步

- [ ] 代码 + 本记忆推到自建 GitLab（私有），实现跨设备同步。
- [ ] 平板"接着干活"方案落地（见下）。
- [ ] `campaign/` 真实 LLM/MCP 接入（目前 stub）；Router 已优化、BudgetRule 已无共享态。
- [ ] 仓库里还有 6 个会话前就修改的 HTML + `CLAUDE.md` + `_*.js` 未提交，待定是否纳入。

## 跨设备 / 平板续作方案（self-hosted GitLab + 要能干活）

- **记忆同步**：靠本文件 + git。换设备先 `git pull`，读本 WORKLOG 顶部即续上下文。
- **干活载体**（平板不能本机跑 agent，二选一）：
  1. **常开 dev box + 远程**：一台常开机器（家里 PC 或 GitLab 旁的小 VPS）装好仓库 + Claude Code/CodeWhale + 密钥；平板用 SSH（Termius/Blink）或 Web 终端（ttyd/wetty）接入。配 **Tailscale** 免公网端口转发最省心。
  2. **Claude Remote Control**：开 `autoUploadSessions` + claude.ai/code，平板浏览器驱动常开机器上的 Claude Code。
- **密钥不入库**：dev box 上单独配 `~/.codewhale/config.toml`；仓库永远不存 key。
