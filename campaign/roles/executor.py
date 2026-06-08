"""Executor 执行（M2）——主力：patch / 测试 / 局部执行。

只发领域事件 executor.output，生命周期事件由 Coordinator 管。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..core.models import Task
from .base import Agent

if TYPE_CHECKING:
    from ..llm.client import LLMClient


class Executor(Agent):
    """执行器：完成代码/工具调用任务。"""

    def __init__(self, *args, llm: "LLMClient | None" = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._llm = llm

    async def handle(self, task: Task) -> dict:
        """执行任务。失败抛异常，由 Coordinator 统一发 incident。"""
        if self._llm:
            prompt = {"role": "user", "content": f"请执行以下任务: {task.goal}\n验收标准: {task.acceptance}"}
            resp = await self._llm.complete(self.spec.model_tier, [prompt])
            from ..llm.client import extract_text
            output = extract_text(resp)
        else:
            output = f"[stub] executed task '{task.id}': {task.goal}"

        result = {"output": output, "task_id": task.id}
        await self._emit("executor.output", {"task_id": task.id, "output_length": len(output)})
        return result
