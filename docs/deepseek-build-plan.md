# DeepSeek TUI 开发拆解：把「指挥—运行—演练」框架落成可运行系统

> 本文件是给 **DeepSeek 编码 Agent（TUI）** 的施工说明书。
> 配套设计文档：[multi-agent-framework-v5.md](multi-agent-framework-v5.md)（先读它，理解概念，再按本文件分阶段实现）。
> 目标读者是一个**没有本对话上下文**的编码 agent，所以下面的接口、目录、验收标准都写到可直接动手的粒度。

---

## 0. 给 DeepSeek 的工作纪律（先读）

1. **按 Milestone 顺序开发**，一个 milestone 一个提交（或一个 PR），不要一次性写完所有东西。
2. **每个 milestone 必须带单元测试**，且测试通过才算完成。
3. **先写接口（abstract base class / Pydantic model），再写实现**。接口稳定后实现可替换。
4. **框架核心保持依赖最小**：核心层只依赖 `pydantic`、`anyio`/`asyncio`、标准库；模型/工具/可观测性等用**适配器（adapter）**接入，不把第三方 SDK 写进核心。
5. **遇到与本计划冲突的现状，以代码现状为准**，并在提交说明里写清你做了什么调整。
6. 每个模块顶部写一句 docstring 说明它对应框架文档的哪一节。

---

## 1. 技术栈与项目骨架

- 语言：**Python 3.11+**，异步用 `asyncio`。
- 校验/序列化：**Pydantic v2**。
- 存储：Event Log 先用 **SQLite（jsonl 表）** 起步，接口留好可换 Postgres。
- 测试：**pytest** + `pytest-asyncio`。
- 模型接入：**OpenAI 兼容协议**（DeepSeek / Ollama / vLLM 都兼容），用一个 `LLMClient` 适配器统一。
- 可选编排：核心自带轻量循环；后续可接 LangGraph，但不作为 M0 依赖。

建议目录：

```
campaign/                 # 包名（指挥—会战—演武）
  core/
    events.py             # Event 模型 + EventLog 抽象/SQLite 实现
    state.py              # 由事件回放派生的 State 视图
    models.py             # 公共数据模型（Task, ExecutionOrder, AgentSpec...）
  roles/
    base.py               # Agent 抽象基类
    coordinator.py        # 协调器（拆解+调度+路由）
    executor.py           # 执行
    retriever.py          # 检索
    reviewer.py           # 验收（裁判）
    reserve.py            # 通用预备队（小模型）
  routing/
    router.py             # ROI 路由（置信度/成本/成功率）
    skill_registry.py     # 能力注册表（会烧饭/会射箭）
  resilience/
    breaker.py            # 熔断 + 协同制动（背压）
    checkpoint.py         # 检查点/回滚
    mobilization.py       # 减员检测 + 三级响应 + 兵力台账
  governance/
    policy.py             # Policy-as-Code 规则
    governor.py           # 督军：监察 + 独立上报
  observability/
    tracer.py             # trace/metering/replay/live-scoring
  llm/
    client.py             # OpenAI 兼容 LLMClient 适配器 + 模型档位配置
  eval/
    harness.py            # eval 集运行 + 打分 + 上线门禁
    chaos.py              # 随机突袭（故障注入）
  app/
    runtime.py            # 把以上装配成可运行 Runtime
    config.py             # 部署模式（local/cloud/hybrid）+ 隐私开关
  tests/
docs/
```

---

## 2. 核心数据模型（M0 必须先定）

```python
# core/models.py
class AgentSpec(BaseModel):
    id: str
    role: Literal["coordinator","executor","retriever","reviewer","reserve"]
    model_tier: Literal["flagship","coder","value","small"]
    skills: list[str]            # 会烧饭/会射箭：技能标签
    proficiency: dict[str, int]  # skill -> 0..100 熟练度

class Task(BaseModel):
    id: str
    goal: str
    difficulty: Literal["simple","medium","hard"]   # 难度分级
    degradable: bool                                # 可降级性
    required_skills: list[str]
    acceptance: str                                 # 验收标准

class ExecutionOrder(BaseModel):     # 意图层产出
    objective: str
    constraints: list[str]           # 红线：成本/安全/合规
    budget: dict                     # token/$/时间上限
    tasks: list[Task]
```

事件模型（事件溯源核心）：

```python
# core/events.py
class Event(BaseModel):
    seq: int                 # 单调递增
    ts: datetime
    type: str                # task.assigned / task.done / brake.signal / incident ...
    actor: str               # agent id 或 "governor"/"system"
    payload: dict

class EventLog(ABC):
    async def append(self, type, actor, payload) -> Event: ...
    async def replay(self, since: int = 0) -> list[Event]: ...
# 状态永远 = replay 后派生，不存可变状态。
```

---

## 3. 分阶段施工（Milestones）

> 每个 milestone 标了【对应框架节】【交付】【验收标准（DoD）】。

### M0 · 地基：Event Log + 状态派生
- 【框架】运行时 2.2
- 【交付】`core/events.py`（Event + 抽象 + SQLite 实现）、`core/state.py`（replay 派生 State）、`core/models.py`。
- 【DoD】能 append 多个事件并 replay 还原；State 完全由 replay 计算，无可变共享状态；单测覆盖追加/回放/回滚到 seq=N。

### M1 · LLM 适配器 + 模型档位
- 【框架】6.1 角色×模型
- 【交付】`llm/client.py`：OpenAI 兼容 `LLMClient`（DeepSeek/Ollama/vLLM），按 `model_tier` 映射到具体模型；统一 `complete()/tool_call()`；带超时与重试。
- 【DoD】用一个 mock server 跑通 complete；档位配置可在 `app/config.py` 切换；不在核心层 import 厂商 SDK（仅在 adapter 内）。

### M2 · 角色骨架 + 最小 Agent Loop
- 【框架】运行时 2.1
- 【交付】`roles/base.py` 抽象基类；coordinator/executor/retriever/reviewer/reserve 五个实现的最小版；`app/runtime.py` 串成一条最小链路（Coordinator 拆解 → Executor 执行 → Reviewer 验收）。
- 【DoD】跑一个玩具 ExecutionOrder 能走完 拆解→执行→验收，全程事件落到 Event Log；**Reviewer 与 Coordinator 是不同实例（裁判/运动员分离）**。

### M3 · 能力注册表 + ROI 路由
- 【框架】运行时 2.3、治理 7（能力表）
- 【交付】`routing/skill_registry.py`（按 skill 查可用 agent）、`routing/router.py`（按 置信度+成本+历史成功率 打分选 agent；成功率从 Event Log 统计）。
- 【DoD】不具备 `required_skills` 的 agent 不会被派该任务；路由分数可解释（返回各信号分值）；单测覆盖"无人具备技能→冻结排队"。

### M4 · 韧性 + 协同制动
- 【框架】抗毁性 3.1、④协同制动
- 【交付】`resilience/breaker.py`：单点熔断 + **协同制动**（过载 agent 向 Event Log 广播 `brake.signal`，Coordinator 背压暂停派活、同链路降并发）；`resilience/checkpoint.py`：checkpoint/rollback。
- 【DoD】注入一个持续失败的 executor，观察到：单点熔断 → 广播制动 → 上游停止派新活，且不发生雪崩（其它链路仍可推进）；可 rollback 到上一个 checkpoint。

### M5 · 动员减员 + 小模型预备队
- 【框架】3.3、④互助、⑤
- 【交付】`resilience/mobilization.py`：减员检测（重试耗尽/错误率突增/429）、三级响应（替补/降级运转/战时动员）、**兵力台账 Capacity Ledger**；`roles/reserve.py`：小模型预备队，work-stealing 抢积压任务，**只接 simple/medium**，hard 任务拒接并回报 Coordinator 冻结排队。
- 【DoD】模拟某类 executor 大面积失能 → 预备队补位 simple/medium、hard 被冻结；台账实时反映可用兵力；**Reviewer 门禁不被降级**（用小模型替执行但验收仍用原档位，且减员时 Reviewer 阈值提高）。

### M6 · 治理层（督军）
- 【框架】⑦
- 【交付】`governance/policy.py`（Policy-as-Code：越权调用/数据出域/超预算/绕过审计规则）；`governance/governor.py`（督军：独立校验每个动作，违规则拦截并独立上报，**不受 Coordinator 节制**，Coordinator 自身动作也受检）。
- 【DoD】构造一个超预算/越权动作 → 督军拦截并产生 `governance.violation` 事件上报；督军逻辑独立运行，关闭 Coordinator 不影响督军判定；规则对所有角色一视同仁。

### M7 · 可观测性 + 部署/隐私配置
- 【框架】3.2、⑧
- 【交付】`observability/tracer.py`（trace/metering/replay/在线打分，建议对接 OpenTelemetry 接口）；`app/config.py` 支持 local/cloud/hybrid 三模 + 隐私开关（敏感任务强制走本地模型 tier，督军校验"数据出域"红线）。
- 【DoD】每个 agent 调用有 trace + token 计费；hybrid 模式下标记 sensitive 的任务只路由到本地模型，违反则被督军拦下；能基于 Event Log 做 replay。

### M8 · 演练层：eval 门禁 + 随机突袭 + 数据飞轮
- 【框架】演练时、⑨
- 【交付】`eval/harness.py`（跑 eval 集→打分→未达阈值禁止"上线"标记）；`eval/chaos.py`（随机让 agent 掉线，验证制动/补位/work-stealing）；导出复盘产物为训练集（jsonl）供蒸馏（对接 OpenClaw-RL 留接口）。
- 【DoD】eval 不达阈值时 runtime 拒绝进入"生产"模式；chaos 测试能随机杀 agent 且系统不崩；能把成功/失败案例导出成可训练的 jsonl。

---

## 4. 组件接入对照（哪个 milestone 用哪个外部组件）

| Milestone | 可接入的外部组件 | 接入方式 |
|---|---|---|
| M1 LLM | DeepSeek / Ollama / vLLM | OpenAI 兼容 adapter |
| M2 角色循环 | Hermes Agent（function-calling 循环参考） | 借鉴其 tool loop，或后期替换 |
| M3 技能 | OpenClaw Skill Layer | skill_registry 对接其技能模块 |
| M4/M6 治理 | OpenClaw ClawKeeper（Watchers） | governor 对接其安全防护 |
| M7 可观测 | LangSmith / Langfuse / OpenTelemetry | tracer adapter |
| M7 编排（可选） | LangGraph | runtime 可替换为 LangGraph 图 |
| M8 训练 | OpenClaw-RL | 导出 jsonl → 蒸馏小模型 |

---

## 5. 全局验收（系统级 DoD）

1. 跑一个完整 demo：给定 ExecutionOrder → 拆解 → 路由 → 执行 → 验收 → 复盘导出，全程事件可 replay。
2. 注入故障（杀掉某类 executor）系统不崩，预备队补位、督军不缺位、Reviewer 不降级。
3. eval 门禁生效：分数不达标禁止上线。
4. 三种部署模式可切换，hybrid 下敏感数据不出域。
5. 所有 milestone 单测通过，核心层不依赖任何厂商 SDK。

---

## 6. 一句话交给 DeepSeek

> 读 `multi-agent-framework-v5.md` 理解概念，然后从 **M0 开始按顺序实现**，每个 milestone 一个提交、带测试、先接口后实现，核心层依赖最小，外部组件用 adapter 接入。完成一个 milestone 就停下汇报 DoD 是否达成，再继续下一个。
