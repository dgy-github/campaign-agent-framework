"""Runtime 装配（M2 起逐步长大）。把所有组件串成最小闭环并逐 Milestone 接入。

最小链路(M2): Coordinator 拆解 → Router 选 agent → Executor 执行 → Reviewer 验收。
随 Milestone 接入：M4 breaker/checkpoint、M5 mobilization/reserve、M6 governor、
M7 tracer/config、M8 eval gate。

事件归属约定：
- 生命周期事件（task.assigned/done/failed/frozen/incident）由 Coordinator/Runtime 统一发射。
- Worker 只发领域事件（executor.output、review.done、retriever.result 等）。
"""
from __future__ import annotations

import asyncio
import uuid

from ..app.config import Config
from ..core.events import EventLog
from ..core.models import AgentSpec, ExecutionOrder, Task
from ..roles.base import Agent
from ..roles.coordinator import Coordinator
from ..roles.executor import Executor
from ..roles.reviewer import Reviewer
from ..protocol import AgentCard
from ..transport import InProcessTransport, RemoteAgentProxy, Transport


class Runtime:
    """多 Agent 运行时。把各组件装配成可运行系统。"""

    def __init__(self, log: EventLog, config: Config) -> None:
        self.log = log
        self.config = config
        self._agents: dict[str, Agent] = {}
        self._transport: Transport | None = None
        self._coordinator: Coordinator | None = None
        self._governor = None
        self._mobilizer = None
        self._tracer = None
        self._breaker = None
        self._checkpointer = None
        self._require_reviewer = False  # 边界：True 时无 Reviewer 则 fail-closed（不静默放行）
        self._task_timeout: float | None = None  # 边界：单任务执行超时(秒)，None=不限
        self._concurrency = 1            # 深层#1：并发上限，默认 1=串行（行为不变）
        self._gate = None                # 深层#3：统一执行闸（PolicyGate），随 governor 创建
        self._run_tasks: dict[str, dict[str, Task]] = {}
        self._run_budgets: dict[str, dict] = {}
        self._memory_session: bool = False   # M-F: opt-in session memory
        self._memory_scratch: bool = False   # M-F: opt-in scratchpad memory
        self._knowledge = None               # M-G: opt-in KnowledgeStore for RAG injection
        self._knowledge_k = 3                # M-G: default top-k for knowledge retrieval

    # ── 组件注册 ────────────────────────────────────────

    def register_agent(self, agent: Agent) -> None:
        self._agents[agent.spec.id] = agent
        register = getattr(self._transport, "register", None)
        if callable(register):
            register(agent)

    def register_remote(self, card_or_spec: AgentCard | AgentSpec) -> RemoteAgentProxy:
        """Register a transport-routed agent for selection only.

        The proxy lets Runtime choose the remote agent by spec/skills. Execution
        still goes through self._transport, typically HttpJsonRpcTransport.
        """
        proxy = RemoteAgentProxy(card_or_spec, self.log)
        self._agents[proxy.spec.id] = proxy
        return proxy

    async def discover_remote(self, transport) -> list[AgentCard]:
        """Discover remote agent cards via transport and register them.

        Calls transport.discover(), registers each discovered card via
        register_remote, and returns the list of discovered cards.
        On discovery failure (empty list) this is a no-op.
        """
        cards = await transport.discover()
        for card in cards:
            self.register_remote(card)
        return cards

    def set_transport(self, transport: Transport) -> None:
        self._transport = transport

    def set_coordinator(self, coordinator: Coordinator) -> None:
        self._coordinator = coordinator

    def set_governor(self, governor) -> None:
        self._governor = governor
        # 深层#3：把 governor 收口成统一执行闸（并发安全 + 可被 LLMClient 共享）
        from ..governance.gate import PolicyGate
        self._gate = PolicyGate(governor)

    def set_concurrency(self, n: int) -> None:
        """并发执行任务的上限（深层#1）。默认 1=串行。共享状态由 PolicyGate 锁保护。"""
        self._concurrency = max(1, int(n))

    @property
    def gate(self):
        """暴露统一执行闸，供 LLMClient 等共享同一把锁/账本。"""
        return self._gate

    def set_mobilizer(self, mobilizer) -> None:
        self._mobilizer = mobilizer

    def set_tracer(self, tracer) -> None:
        self._tracer = tracer

    def set_breaker(self, breaker) -> None:
        self._breaker = breaker

    def set_checkpointer(self, checkpointer) -> None:
        self._checkpointer = checkpointer

    def set_require_reviewer(self, required: bool) -> None:
        """True：无 Reviewer 时验收 fail-closed（不放行）。默认 False 保持向后兼容。"""
        self._require_reviewer = required

    def set_task_timeout(self, seconds: float | None) -> None:
        """单任务执行超时（秒）。挂住的 agent 不会再卡死整个 run。"""
        self._task_timeout = seconds

    def enable_memory(self, session: bool = True, scratch: bool = True) -> None:
        """启用会话/工作记忆注入（M-F，opt-in，默认 OFF）。

        不调用此方法时，run / _execute_one_task 行为与之前完全一致。
        """
        self._memory_session = session
        self._memory_scratch = scratch

    def set_knowledge_store(self, store, k: int = 3) -> None:
        """注入长期知识库用于 RAG 上下文（M-G，opt-in，默认 OFF）。

        store 需实现 KnowledgeStore 接口（search 方法）。不调用时行为不变。
        """
        self._knowledge = store
        self._knowledge_k = max(1, int(k))

    async def state(self, run_id: str):
        """返回某次 run 的派生状态（按 run_id 隔离，多次运行互不混流）。"""
        from ..core.state import derive_state
        return derive_state(await self.log.replay(), run_id=run_id)

    # ── 健康/失败处理（驱动减员真实生效）────────────────

    def _update_health(self, agent_id: str, success: bool) -> None:
        if not self._mobilizer:
            return
        cur = self._mobilizer.ledger.health.get(agent_id, 1.0)
        new = min(1.0, cur + 0.1) if success else max(0.0, cur - 0.4)
        self._mobilizer.ledger.mark_unhealthy(agent_id, new)

    async def _after_failure(self, agent_id: str) -> None:
        if self._breaker:
            tripped = self._breaker.record_failure(agent_id)
            if tripped:
                await self._breaker.broadcast_brake(
                    "execution", f"agent {agent_id} tripped by {self._breaker.fail_threshold} failures")
        self._update_health(agent_id, success=False)

    # ── 主体执行 ────────────────────────────────────────

    async def run(self, order: ExecutionOrder) -> dict:
        """执行一个执行令，返回汇总结果。全程事件落到 self.log（带 run_id 隔离），可 replay。"""
        run_id = uuid.uuid4().hex[:12]  # 边界#1：每次 run 唯一 id，多次运行互不混流
        await self.log.append("run.started", "system", {"run_id": run_id, "objective": order.objective})

        if self._coordinator is None:
            coord_spec = AgentSpec(id="coordinator", role="coordinator", model_tier="flagship", skills=[])
            self._coordinator = Coordinator(coord_spec, self.log)

        agents_list = list(self._agents.values())
        if self._transport is None:
            known_senders = set(self._agents) | {"coordinator", "system"}
            self._transport = InProcessTransport(self._agents, self.log, known_senders=known_senders, gate=self._gate)

        # 边界#3：把 agents 登记进兵力台账，让 health/assess 真实生效
        if self._mobilizer:
            for a in agents_list:
                self._mobilizer.ledger.register_active(a.spec)

        if self._checkpointer:
            await self._checkpointer.snapshot("pre_run")

        tasks = await self._coordinator.decompose(order)
        self._run_tasks[run_id] = {task.id: task for task in tasks}
        self._run_budgets[run_id] = dict(order.budget)
        await self.log.append("run.decomposed", "coordinator", {"run_id": run_id, "task_count": len(tasks)})

        if self._mobilizer:
            level = self._mobilizer.ledger.assess()
            if level.value != "none":
                await self._mobilizer.respond(level)

        # 深层#1：并发执行（信号量限流）。共享状态(预算/账本)由 PolicyGate 锁保护；
        # EventLog.append 本就串行化，seq 安全。默认 concurrency=1 即原串行行为。
        sem = asyncio.Semaphore(self._concurrency)

        async def _guarded(t: Task) -> dict:
            async with sem:
                return await self._execute_one_task(t, agents_list, run_id, order.budget)

        results: list[dict] = []
        done: set[str] = set()
        for layer in self._topological_layers(tasks):
            runnable = [t for t in layer if all(dep in done for dep in t.depends_on)]
            skipped = [t for t in layer if t not in runnable]
            for task in skipped:
                missing = [dep for dep in task.depends_on if dep not in done]
                await self.log.append("task.skipped", "coordinator", {
                    "run_id": run_id,
                    "task_id": task.id,
                    "reason": "dependency_not_done",
                    "dependencies": missing,
                })
                results.append({"task_id": task.id, "status": "skipped_dependency", "dependencies": missing})

            layer_results = list(await asyncio.gather(*(_guarded(t) for t in runnable))) if runnable else []
            for result in layer_results:
                if result.get("status") == "done":
                    done.add(result.get("task_id", ""))
            results.extend(layer_results)

        # 按真实结果重评减员，必要时动员（并发跑完后统一评估）
        if self._mobilizer:
            lvl = self._mobilizer.ledger.assess()
            if lvl.value in ("medium", "severe"):
                await self._mobilizer.respond(lvl)

        await self.log.append("run.completed", "system", {
            "run_id": run_id, "task_count": len(tasks), "results_count": len(results),
        })
        return {"run_id": run_id, "objective": order.objective, "tasks_total": len(tasks), "results": results}

    def _topological_layers(self, tasks: list[Task]) -> list[list[Task]]:
        remaining = {task.id: task for task in tasks}
        completed: set[str] = set()
        layers: list[list[Task]] = []
        while remaining:
            layer = [task for task in remaining.values() if all(dep in completed for dep in task.depends_on)]
            if not layer:
                raise ValueError("cyclic task dependency in ExecutionOrder")
            layers.append(layer)
            for task in layer:
                remaining.pop(task.id)
                completed.add(task.id)
        return layers

    async def resume(self, run_id: str, task_id: str, human_input: dict) -> dict:
        tasks = self._run_tasks.get(run_id)
        budget = self._run_budgets.get(run_id, {})
        original: Task | None = None
        if tasks and task_id in tasks:
            original = tasks[task_id]
        else:
            from ..core.state import derive_state

            events = await self.log.replay()
            state = derive_state(events, run_id=run_id)
            if task_id not in state.tasks_awaiting_input:
                return {"task_id": task_id, "status": "not_found"}

            input_events = [
                event for event in events
                if event.type == "task.input_required"
                and event.payload.get("run_id") == run_id
                and event.payload.get("task_id") == task_id
            ]
            if not input_events or "task" not in input_events[-1].payload:
                return {"task_id": task_id, "status": "not_found"}

            payload = input_events[-1].payload
            original = Task.model_validate(payload["task"])
            budget = payload.get("budget", {})

        merged_input = dict(original.human_input)
        merged_input.update(human_input)
        if original.needs_approval:
            merged_input.setdefault("approved", True)
        task = original.model_copy(update={"human_input": merged_input})
        if tasks is not None:
            tasks[task_id] = task

        await self.log.append("task.resumed", "system", {
            "run_id": run_id,
            "task_id": task_id,
            "human_input": human_input,
        })
        return await self._execute_one_task(
            task,
            list(self._agents.values()),
            run_id,
            budget,
        )

    async def _execute_one_task(self, task: Task, agents_list: list[Agent],
                                run_id: str = "", budget: dict | None = None) -> dict:
        """单任务执行：制动检查 → 选 agent → 治理(真实入账) → 超时执行 → review。"""
        budget = budget or {}

        # ── M-F: opt-in memory injection (default OFF, no-op when flags are False) ──
        session_context: list[dict] | None = None
        scratchpad = None
        if self._memory_session and run_id:
            from ..memory import SessionMemory
            sm = SessionMemory(self.log)
            session_context = await sm.recall(run_id)
        if self._memory_scratch:
            from ..memory import ScratchpadMemory
            scratchpad = ScratchpadMemory()

        # ── M-G: opt-in knowledge injection (default OFF, no-op when _knowledge is None) ──
        knowledge_hits = None
        if self._knowledge is not None:
            filters = {"sensitive_ok": not self.config.privacy_strict}
            hits = self._knowledge.search(task.goal, k=self._knowledge_k, filters=filters)
            if hits:
                knowledge_hits = hits
                await self.log.append("knowledge.injected", "coordinator", {
                    "run_id": run_id,
                    "task_id": task.id,
                    "doc_ids": [h["doc_id"] for h in hits],
                })

        # 检查协同制动
        if self._breaker and await self._breaker.is_braking("execution"):
            await self.log.append("task.frozen", "system", {
                "run_id": run_id, "task_id": task.id, "reason": "execution link braking",
            })
            return {"task_id": task.id, "status": "frozen", "reason": "braking"}

        if task.needs_approval and not task.human_input.get("approved"):
            prompt = task.human_input.get("prompt", "approval required")
            await self.log.append("task.input_required", "coordinator", {
                "run_id": run_id,
                "task_id": task.id,
                "prompt": prompt,
                "reason": "approval",
                "task": task.model_dump(),
                "budget": budget,
            })
            return {"task_id": task.id, "status": "input_required", "prompt": prompt}

        # 选 agent
        selected: Agent | None = None
        if self._coordinator and self._coordinator._router:
            spec = await self._coordinator._router.pick(task)
            if spec:
                selected = next((a for a in agents_list if a.spec.id == spec.id), None)
        else:
            for agent in agents_list:
                if agent.can_do(task):
                    if self._breaker and self._breaker.is_tripped(agent.spec.id):
                        continue
                    selected = agent
                    break

        if selected is None:
            await self.log.append("task.frozen", "coordinator", {
                "run_id": run_id, "task_id": task.id, "reason": "no_available_agent",
            })
            return {"task_id": task.id, "status": "frozen"}

        cost_est = 0.0
        # 边界#2：治理用真实预估成本入账（让 BudgetRule 真正生效）+ 越权检查
        if self._governor:
            from ..governance.policy import Action
            from ..routing.router import Router
            cost_est = float(Router.TIER_COST.get(selected.spec.model_tier, 100))
            action = Action(actor=selected.spec.id, kind="spend", cost=cost_est, payload={"task_id": task.id})
            ctx = {"actor_role": selected.spec.role, "budget": budget, "run_id": run_id}
            # 经统一执行闸（并发安全）；gate 在 set_governor 时创建
            if not await self._gate.check(action, ctx):
                await self.log.append("governance.violation", "governor", {
                    "run_id": run_id, "task_id": task.id, "agent": selected.spec.id,
                    "reason": "budget/authority blocked",
                })
                return {"task_id": task.id, "status": "blocked_by_governor"}

        await self.log.append("task.assigned", "coordinator", {
            "run_id": run_id, "task_id": task.id, "agent": selected.spec.id,
        })

        async def _do():
            from ..protocol import Message, Part
            if self._transport is None:
                known_senders = set(self._agents) | {"coordinator", "system"}
                self._transport = InProcessTransport(self._agents, self.log, known_senders=known_senders, gate=self._gate)

            msg = Message.request(
                from_agent="coordinator",
                to_agent=selected.spec.id,
                task=task,
                run_id=run_id,
            )
            # M-F: inject session context as internal TRUSTED part (alongside existing task part)
            if session_context:
                msg.parts.append(Part(kind="data", data={"session_context": session_context}, untrusted=False))
            # M-G: inject knowledge as untrusted data part (external content, opt-in)
            if knowledge_hits:
                msg.parts.append(Part(kind="data", data={
                    "knowledge": [
                        {
                            "doc_id": h["doc_id"],
                            "score": h["score"],
                            "metadata": h.get("metadata"),
                            "text": (h.get("text") or "")[:500],
                        }
                        for h in knowledge_hits
                    ]
                }, untrusted=True))
            if self._tracer:
                async with self._tracer.span("task.execute", selected.spec.id):
                    resp = await self._transport.send(msg)
            else:
                resp = await self._transport.send(msg)
            if resp.error:
                raise RuntimeError(resp.error)
            return resp.result

        try:
            # 边界#4：单任务超时，挂住的 agent 不卡死整个 run
            if self._task_timeout:
                result = await asyncio.wait_for(_do(), timeout=self._task_timeout)
            else:
                result = await _do()

            if result.get("status") == "input_required":
                prompt = result.get("prompt", "")
                await self.log.append("task.input_required", "coordinator", {
                    "run_id": run_id,
                    "task_id": task.id,
                    "prompt": prompt,
                    "reason": result.get("reason", "agent_input_required"),
                    "task": task.model_dump(),
                    "budget": budget,
                })
                return {"task_id": task.id, "status": "input_required", "prompt": prompt}

            tokens = result.get("tokens")
            if tokens is None and isinstance(result.get("usage"), dict):
                tokens = result["usage"].get("total_tokens")
            if tokens is not None:
                token_cost = float(tokens)
                if self._tracer:
                    self._tracer.meter(selected.spec.id, int(token_cost), token_cost, 0.0)
                if self._governor and token_cost > cost_est:
                    from ..governance.policy import Action

                    delta = token_cost - cost_est
                    action = Action(
                        actor=selected.spec.id,
                        kind="spend",
                        cost=delta,
                        payload={"task_id": task.id, "tokens": token_cost, "adjustment": "actual_usage_delta"},
                    )
                    ctx = {"actor_role": selected.spec.role, "budget": budget, "run_id": run_id}
                    if not await self._gate.check(action, ctx):
                        await self.log.append("governance.violation", "governor", {
                            "run_id": run_id, "task_id": task.id, "agent": selected.spec.id,
                            "reason": "actual token usage blocked",
                        })
                        return {"task_id": task.id, "status": "blocked_by_governor"}

            review_result = await self._review(task, result, agents_list, run_id)
            result["review"] = review_result
            if self._tracer:
                self._tracer.score(task.id, review_result.get("score", 0.0))

            passed = review_result.get("passed", True)
            if passed:
                await self.log.append("task.done", "coordinator", {
                    "run_id": run_id, "task_id": task.id, "agent": selected.spec.id,
                    "review_score": review_result.get("score"),
                })
            else:
                await self.log.append("task.failed", "coordinator", {
                    "run_id": run_id, "task_id": task.id, "agent": selected.spec.id,
                    "reason": "review_rejected",
                })
            if self._breaker:
                self._breaker.record_success(selected.spec.id)
            self._update_health(selected.spec.id, success=passed)

            result["status"] = "done" if passed else "failed"
            # M-F: include scratchpad snapshot in result (empty dict when not enabled)
            if scratchpad is not None:
                result["scratchpad"] = scratchpad.items()
            return result

        except asyncio.TimeoutError:
            await self.log.append("task.failed", "coordinator", {
                "run_id": run_id, "task_id": task.id, "agent": selected.spec.id, "error": "timeout",
            })
            await self.log.append("incident", "coordinator", {
                "run_id": run_id, "task_id": task.id, "reason": "timeout", "agent": selected.spec.id,
            })
            await self._after_failure(selected.spec.id)
            return {"task_id": task.id, "status": "failed", "error": "timeout"}

        except Exception as exc:
            await self.log.append("task.failed", "coordinator", {
                "run_id": run_id, "task_id": task.id, "agent": selected.spec.id, "error": str(exc),
            })
            await self.log.append("incident", "coordinator", {
                "run_id": run_id, "task_id": task.id, "reason": str(exc), "agent": selected.spec.id,
            })
            await self._after_failure(selected.spec.id)
            return {"task_id": task.id, "status": "failed", "error": str(exc)}

    async def _review(self, task: Task, artifact: dict, agents_list: list[Agent], run_id: str = "") -> dict:
        """找到 Reviewer 并验收。artifact 为 Executor 的产出。"""
        reviewers = [a for a in agents_list if isinstance(a, Reviewer)]
        if not reviewers:
            # 边界：默认 fail-open（放行）保持兼容；require_reviewer=True 时 fail-closed
            if self._require_reviewer:
                await self.log.append("incident", "system", {
                    "run_id": run_id, "task_id": task.id, "reason": "no reviewer configured (fail-closed)",
                })
                return {"passed": False, "score": 0.0, "reasons": ["no reviewer configured (fail-closed)"]}
            return {"passed": True, "score": 1.0, "reasons": ["no reviewer configured (fail-open)"]}

        reviewer = reviewers[0]
        # 减员时 Reviewer 加严
        if self._mobilizer:
            level = self._mobilizer.ledger.assess()
            if level.value in ("medium", "severe"):
                reviewer.strictness = max(reviewer.strictness, 0.7)

        # 传 artifact；Reviewer 出错时不让异常冒泡崩掉整个 run，记为未通过
        try:
            return await reviewer.handle(task, artifact)
        except Exception as exc:
            return {"passed": False, "score": 0.0, "reasons": [f"reviewer error: {exc}"], "task_id": task.id}
