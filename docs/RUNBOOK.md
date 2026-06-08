# RUNBOOK —— 运行态执行手册

> 各运行态的「触发条件 → 系统行为 → 人工动作 → 恢复」。
> 与 `campaign/` 实现对齐：事件名、组件、状态都以代码为准。
> 配套：设计见 [multi-agent-framework-v5.md](multi-agent-framework-v5.md)；记忆/归属见 [WORKLOG.md](WORKLOG.md)。

## 0. 速查：事件 → 含义

| 事件 type | 由谁发 | 含义 |
|---|---|---|
| `run.started` / `run.completed` | system | 一次执行令的开始/结束 |
| `task.assigned` / `task.done` / `task.failed` | coordinator | 任务生命周期（**单一来源**：Coordinator 管） |
| `task.frozen` | coordinator / reserve | 无人可接 或 预备队拒接高难 → 冻结排队 |
| `incident` | coordinator / reviewer | 异常落库（含 reason/agent） |
| `executor.output` / `retriever.result` / `review.started` / `review.done` | worker | 领域事件（不重复发生命周期） |
| `brake.signal` / `brake.released` | circuit_breaker | 协同制动开/关（按 link） |
| `governance.violation` | governor | 督军拦截 |
| `mobilization.*` | mobilizer | 动员动作（substitute/checkpoint/reviewer_strict/reprioritize/human_escalation/response） |

> 当前状态随时可由 `derive_state(await log.replay())` 还原（pending/done/frozen/incidents）。

---

## 1. 正常运行

- **触发**：`runtime.run(order)`。
- **行为**：Coordinator 拆解 → 按技能/ROI 路由 → worker 执行 → Reviewer 独立验收 → 通过记 `task.done`，否则 `task.failed`。
- **人工**：无。看 `[Result Summary]` 与事件流即可（demo：`python -m campaign`）。

## 2. eval 门禁（上线前）

- **触发**：发布前 `EvalHarness.gate(runtime, cases)`。
- **行为**：跑 eval 集 → 加权分 `>= threshold`(默认 0.8) 才放行；否则**禁止进入生产**。
- **人工**：未达标 → 看 `results_summary()` 找低分用例 → 修复/调参 → 重跑。**不要跳过门禁**。
- **恢复**：分数达标后放行。

## 3. 协同制动（防雪崩）

- **触发**：某 agent 连续失败达 `fail_threshold`(默认 3) → `CircuitBreaker` 触发 → `broadcast_brake("execution", ...)` 发 `brake.signal`。
- **行为**：Coordinator 在派活前查 `is_braking(link)`，制动中则把任务 `task.frozen`(reason=braking)，**停止往该链路派新活**（背压）。
- **人工**：查 `brake.signal` 的 reason、定位故障 agent；修复后调用 `release_brake(link)` 发 `brake.released`。
- **恢复**：`release_brake` 后链路恢复派活；被冻结的任务重新排队。

## 4. 单点熔断

- **触发**：`record_failure(agent_id)` 累计达阈值 → `is_tripped` 为真。
- **行为**：路由跳过被熔断的 agent；`cooldown_sec`(默认 30s) 后自动半恢复。成功一次 `record_success` 清零。
- **人工**：频繁熔断同一 agent → 查该模型/endpoint 配额或健康度（兵力台账 `CapacityLedger.health`）。
- **恢复**：冷却自动恢复，或换备用 tier。

## 5. 减员 → 三级响应（动员）

由 `CapacityLedger.assess()` 评级，`Mobilizer.respond(level)` 执行：

| 等级 | 触发(健康<0.5 占比) | 系统动作 | 人工 |
|---|---|---|---|
| LIGHT | ≤1 个或 <35% | 启用预备役替补（`mobilization.substitute`） | 观察 |
| MEDIUM | 35–60% | 降级运转：先 checkpoint、预备队顶 simple/medium、**Reviewer 加严**(strictness↑0.2) | 确认非关键任务可砍 |
| SEVERE | >60% 或配额耗尽 | 战时动员：全预备队激活、优先级重排、**上报 human-in-the-loop**(`mobilization.human_escalation`) | **必须介入**：扩容/切厂商/砍低优 |

- **恢复**：健康度回升后 `assess()` 降级；手动把临时激活的预备队退回 reserve。

## 6. 高难冻结 / 互助补位

- **触发**：无 agent 具备 `required_skills`，或预备队(`Reserve`)收到 `difficulty=="hard"` → `task.frozen`。
- **行为**：任务进冻结队列等待；预备队只接 simple/medium（红线：**把关不降级、高难宁冻结**）。
- **人工**：高难任务堆积 → 恢复/扩容主力 agent，或人工接管。
- **恢复**：有能力 agent 上线后，冻结任务重新路由（`task.assigned` 会把它移出 frozen）。

## 7. 督军拦截（治理）

- **触发**：`Governor.vet(action, ctx)` 命中规则 → `governance.violation`，动作被拦(返回 False)。
- **规则**：`BudgetRule`(超 `context["cumulative_spend"]`>预算)、`DataEgressRule`(隐私模式 sensitive 数据外发)、`AuthorityRule`(越权)。
- **人工**：看 violation 的 `violations[]`；超预算→提额或停；数据出域→改走本地 tier；越权→修角色权限矩阵。**督军一视同仁，Coordinator 自身也受检**。
- **恢复**：消除违规条件后重试该动作。

## 8. 检查点 / 回滚 / 容灾(DR)

- **检查点**：`Checkpointer.snapshot()` 记录当前 seq（中度减员时 mobilizer 会自动打点）。
- **回滚**：`Checkpointer.rollback(seq)` = 回放到该 seq 得到 State（逻辑回滚，事件不可变）。
- **容灾(系统级，区别于运行时备份)**：多区域主备 + 配置/知识库/权重异地备份 + **定期演练恢复**（备份不演练=没备份）。
- **人工**：事故后选最近健康 checkpoint 回滚，从该点重跑。

## 9. 事故响应（incident）

1. 看 `incident` / `task.failed` 事件的 reason、actor。
2. 判类型：参数错(不重试) / 临时失败(退避重试) / 配额(切 tier) / 权限(停)。
3. 触发熔断/制动则按 §3/§4 处理；超出自动恢复 → human-in-the-loop。
4. 复盘：把根因 + 处置写入 [WORKLOG.md](WORKLOG.md)（标执行 agent），成功处置沉淀为预案。

---

## 附：可观测性（看运行态）

- `tracer`(InMemoryTracer)：每次 agent 调用有 `span`(含 duration/status)，验收后 `score(task_id, score)`。
- 生产建议接 Langfuse / OpenTelemetry（adapter 已留）。
- 兵力台账 `CapacityLedger`(active/reserve/quota/health) 是动员决策依据，数据来自可观测层。
