"""通用型预备队（M5）——小模型常驻，机动补位。

对应框架文档：④互助、⑤。红线：只接 simple/medium，hard 拒接并回报冻结。
只发领域事件，Task 生命周期由 Coordinator 管理。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..core.models import Task
from .base import Agent

if TYPE_CHECKING:
    from ..llm.client import LLMClient


class Reserve(Agent):
    """通用预备队：小模型 (model_tier='small') 常驻，机动补位。"""

    def __init__(self, *args, llm: "LLMClient | None" = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._llm = llm

    async def handle(self, task: Task) -> dict:
        """补位执行。task.difficulty == "hard" 必须拒接。"""
        if task.difficulty == "hard":
            await self._emit("task.frozen", {"task_id": task.id, "reason": "reserve refuses hard task; freeze & queue"})
            return {"accepted": False, "reason": "reserve refuses hard task; freeze & queue", "task_id": task.id}

        if self._llm:
            prompt = {"role": "user", "content": f"请执行以下{task.difficulty}难度任务: {task.goal}"}
            resp = await self._llm.complete(self.spec.model_tier, [prompt])
            from ..llm.client import extract_text
            output = extract_text(resp)
        else:
            output = f"[stub] reserve executed '{task.id}' ({task.difficulty}): {task.goal}"

        result = {
            "accepted": True,
            "output": output,
            "task_id": task.id,
            "difficulty": task.difficulty,
            "needs_strict_review": task.difficulty == "medium",
        }
        await self._emit("reserve.output", {"task_id": task.id, "difficulty": task.difficulty})
        return result

    async def steal_work(self, frozen_tasks: list[Task]) -> Task | None:
        """work-stealing：从积压/冻结队列中抢 simple/medium 任务。"""
        for task in frozen_tasks:
            if task.difficulty == "hard":
                continue
            if self.can_do(task):
                return task
        return None
