# SCALING —— 并发 / 跨进程 / 分布式 边界与方案

> 诚实声明：`campaign/` 当前是**单进程**参考实现。本文说明：
> ① 进程内并发（**已实现**）、③ 全链路治理（**已实现**），以及
> ② 跨进程 / 分布式一致性（**未实现**，仅设计 + 边界说明）。
> 不在测试里假装"证明分布式一致性"——那需要真正的基础设施。

## 已实现

### ① 进程内并发（真并行）
- `Runtime.set_concurrency(n)`：用 `asyncio.Semaphore` + `gather` 并发跑任务（默认 1=串行，行为不变）。
- **并发安全**：共享可变状态（预算账本）的读改写经 **`PolicyGate` 的 `asyncio.Lock`** 串行化；`EventLog.append` 本就由单线程 executor 串行化，`seq` 单调安全。
- 假设：任务**相互独立**（无依赖 DAG）。有依赖关系时需先建依赖模型，再分层并发。

### ③ 全链路治理（统一执行闸）
- `governance/gate.py::PolicyGate` = Governor + 锁，单一入口。
- 被两处共享：`Runtime`（每任务 spend 入账）+ `LLMClient`（**每次调用** spend / data_egress 过闸）。
- 治理从"派活一点 vet"扩展到"每个真实动作边界"。

---

## ② 跨进程 / 分布式（未实现 —— 设计）

当前所有"状态"分两类，分布式化要求各自换底座：

| 组件 | 现状（单进程） | 分布式需要 |
|---|---|---|
| **Event Log** | SQLite + 线程池；`seq` 靠 AUTOINCREMENT | 共享事件存储（Postgres/Kafka）；**乐观并发**或单写者；**幂等键**防重复 append |
| **事件订阅 / 制动信号** | 进程内 `asyncio.Queue` | 分布式 pub/sub（Redis/NATS/Kafka）广播 `brake.signal`/`score` 到所有 worker |
| **预算账本** | `Governor._cumulative_spend` 进程内 + 进程内锁 | 共享原子计数器（Redis INCR / DB 行锁）做全局预算 |
| **熔断 / 兵力台账 health** | 进程内 dict | 共享状态存储 + TTL；或每 worker 上报、中心聚合 |
| **Mobilizer 动员** | 任意进程都会跑 | **leader / 租约（lease）**：只许一个实例做动员决策，避免重复扩编 |
| **Checkpoint / rollback** | `truncate_after` 删本地行 | 分布式下需协调（停写 → 截断 → 广播），或改用不可变快照分支 |

### 关键不变量（分布式必须保住）
1. **seq 全局单调唯一** —— 多写者下用 DB 序列/单写者 leader。
2. **幂等** —— 同一 `run_id + task_id` 的事件不可重复生效（加幂等键 / 去重）。
3. **run 隔离** —— 已有 `run_id`，分布式下天然成为分片/路由键。
4. **预算全局一致** —— 不能各进程各算，必须共享原子计数。
5. **动员单点决策** —— leader 选举 / 租约，防多实例同时动员。

### 接口就绪度
- `EventLog` 是 ABC：可新增 `PostgresEventLog` / `KafkaEventLog` 实现，不动上层。
- `PolicyGate` 的锁可换成分布式锁（接口不变）。
- 这些是"换实现"，不是"改架构"——架构已为分布式留了缝。

### 落地顺序（若要做）
1. EventLog → Postgres（seq 用序列，append 幂等键）。
2. 预算/制动/health → Redis（原子计数 + pub/sub + TTL）。
3. Mobilizer → 加 leader 租约。
4. 多 worker 部署 + 端到端一致性压测（含 chaos）。

> 结论：当前单进程实现 + 已留好的接口缝，足以支撑 ①③ 的真实并发与全链路治理；
> ② 分布式是独立的基础设施项目，按上表逐组件替换底座即可，**切勿在未替换前假装已具备分布式一致性**。
## Distributed MVP now implemented

The project now has a minimal cross-node path:

- `HttpJsonRpcTransport` sends A2A `message/send` requests over HTTP/JSON-RPC.
- `JsonRpcAgentServer` hosts real agents on the worker node.
- `RemoteAgentProxy` lets the coordinator node select a remote agent by `AgentSpec`/skills without running its `handle()` locally.
- `Runtime.register_remote(card_or_spec)` registers that proxy for selection only. The default `Runtime` behavior remains in-process unless a custom transport is set.
- Two `SqliteEventLog` instances can point at the same SQLite file for a small shared event stream. The distributed demo and tests show node A writing lifecycle/review events while node B writes A2A and worker domain events, then `derive_state(shared_log, run_id)` reconstructs one run state.

This is intentionally a minimum viable distributed setup, not production distributed consistency. It proves routing and event replay across node boundaries without adding infrastructure.

Demo:

```bash
python -m campaign.examples.distributed_demo
```

Test coverage:

```bash
python -m pytest campaign/tests/test_distributed.py
```

Remaining gaps before production distributed operation:

- Budget accounting, circuit breakers, health, and subscription queues are still per-process unless replaced by shared stores or pub/sub.
- Mobilizer has no leader election or lease, so multiple coordinator processes could make duplicate mobilization decisions.
- SQLite sharing is suitable only for a small number of writers and low write volume. Use Postgres, a serialized event service, or another globally ordered log for higher concurrency.
- There is no idempotency key enforcement for duplicate remote delivery.
- HTTP transport has no service discovery, retries with backoff, health checks, authentication, or mTLS.
- Checkpoint rollback still uses local SQLite truncation semantics and is not coordinated across distributed writers.

---
