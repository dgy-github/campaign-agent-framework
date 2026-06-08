"""Coordinator 协调器（M2）——运动员：只做拆解+调度+路由，不做验收。

对应框架文档：运行时 2.1 / 2.3。注意：必须与 Reviewer 是不同实例。
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import ValidationError

from ..core.models import ExecutionOrder, Task
from .base import Agent

if TYPE_CHECKING:
    from ..llm.client import LLMClient
    from ..routing.router import Router


class Coordinator(Agent):
    """协调器：拆解执行令 → 路由派活。

    llm 可选：提供时用 LLM 做智能拆解，否则直接使用 order.tasks。
    router 可选：提供时按 ROI 路由，否则直接遍历 agents。
    """

    def __init__(self, *args, llm: "LLMClient | None" = None, router: "Router | None" = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._llm = llm
        self._router = router

    async def decompose(self, order: ExecutionOrder) -> list[Task]:
        """把执行令拆成可调度的任务列表。

        有 LLM 时：调用 LLM 把 objective 拆成带 difficulty/required_skills 的 Task。
        无 LLM 时：直接返回 order.tasks（已由调用方预拆解）。
        """
        if self._llm and order.tasks == []:
            # 使用 LLM 拆解模糊目标
            prompt = {
                "role": "user",
                "content": (
                    f"目标: {order.objective}\n"
                    f"约束: {', '.join(order.constraints) if order.constraints else '无'}\n"
                    "请拆解为多个可执行任务，每个任务输出 JSON: "
                    '{"id":"t1","goal":"...","difficulty":"simple|medium|hard","required_skills":["..."]}'
                ),
            }
            resp = await self._llm.complete("flagship", [prompt])
            from ..llm.client import extract_text
            raw = extract_text(resp)
            try:
                tasks_data = json.loads(raw)
                if isinstance(tasks_data, dict):
                    tasks_data = [tasks_data]
                # 边界：LLM 返回的 task dict 可能缺字段/类型非法 → 捕获 ValidationError，
                # 不让单条坏数据炸掉整个 run；坏条跳过，全坏则回退 order.tasks。
                tasks = []
                for t in tasks_data:
                    try:
                        tasks.append(Task(**t))
                    except (ValidationError, TypeError):
                        await self._emit("incident", {"reason": "decompose: invalid task dict skipped", "raw": str(t)[:120]})
                if not tasks:
                    tasks = order.tasks
            except (json.JSONDecodeError, TypeError, ValueError):
                tasks = order.tasks  # fallback
            # 确保每个任务标注了 difficulty
            for t in tasks:
                if not t.difficulty:
                    t.difficulty = "medium"
            return tasks
        return order.tasks

    async def dispatch(self, task: Task, agents: list[Agent]) -> dict:
        """通过 router 选 agent 并派活；接收背压信号时暂停派活。

        有 router 时：按 ROI 打分选最优 agent。
        无 router 时：遍历 agents 选第一个 can_do 的。
        """
        selected: Agent | None = None

        if self._router:
            spec = await self._router.pick(task)
            if spec:
                selected = next((a for a in agents if a.spec.id == spec.id), None)
        else:
            # 简单遍历
            for agent in agents:
                if agent.can_do(task):
                    selected = agent
                    break

        if selected is None:
            await self._emit("task.frozen", {"task_id": task.id, "reason": "no_available_agent"})
            return {"status": "frozen", "task_id": task.id}

        await self._emit("task.assigned", {"task_id": task.id, "agent": selected.spec.id})

        try:
            result = await selected.handle(task)
            await self._emit("task.done", {"task_id": task.id, "agent": selected.spec.id, "result": result})
            return {"status": "done", "task_id": task.id, "agent": selected.spec.id, "result": result}
        except Exception as exc:
            await self._emit("task.failed", {"task_id": task.id, "agent": selected.spec.id, "error": str(exc)})
            await self._emit("incident", {"task_id": task.id, "reason": str(exc), "agent": selected.spec.id})
            return {"status": "failed", "task_id": task.id, "error": str(exc)}

    async def handle(self, task: Task) -> dict:
        """Coordinator 自身的 handle：协调任务（作为运动员而非裁判）。"""
        return await self.dispatch(task, [])
