"""Agent 抽象基类（M2）。

设计约定：
- 生命周期事件（task.assigned/done/failed/frozen/incident）由 Coordinator 统一发射。
- Worker 的 _emit 只发领域事件（如 executor.output、review.done），不重复发生命周期事件。
"""
from __future__ import annotations

import abc

from ..core.events import EventLog
from ..core.models import AgentSpec, Task
from ..protocol import Message, Part


class Agent(abc.ABC):
    def __init__(self, spec: AgentSpec, log: EventLog) -> None:
        self.spec = spec
        self.log = log

    @abc.abstractmethod
    async def handle(self, task: Task) -> dict:
        """处理一个任务，返回结果 dict。

        不发 task.assigned/done/failed——这些由 Coordinator 统一管理。
        Worker 发领域事件（如 executor.output）。
        """
        raise NotImplementedError

    def can_do(self, task: Task) -> bool:
        """是否具备 task.required_skills。路由/补位前置检查。"""
        return all(s in self.spec.skills for s in task.required_skills)

    async def on_message(self, msg: Message) -> Message:
        """A2A protocol entrypoint. Default behavior adapts request messages to handle()."""
        try:
            if not msg.parts or not isinstance(msg.parts[0].data, dict):
                return msg.reply({}, error="request message missing task data")
            task = Task(**msg.parts[0].data)
            result = await self.handle(task)
            return msg.reply(result)
        except Exception as exc:
            return msg.reply({}, error=str(exc))

    async def ask(
        self,
        to_agent: str,
        query: object,
        transport: object,
        run_id: str = "",
        task_id: str = "",
    ) -> dict:
        """向另一个 agent 发送查询并返回结果（M-G，opt-in）。

        Args:
            to_agent: 目标 agent id
            query: 查询文本
            transport: 带 ``async def send(msg: Message) -> Message`` 的传输对象
            run_id: 关联的运行 id
            task_id: 关联的任务 id

        Returns:
            ``{"result": ..., "untrusted": True}`` 或 ``{"error": ..., "untrusted": True}``
            绝不抛出未捕获异常。
        """
        try:
            msg = Message(
                from_agent=self.spec.id,
                to_agent=to_agent,
                run_id=run_id,
                task_id=task_id,
                kind="query",
                parts=[Part(kind="data", data={"query": query})],
            )
            resp = await transport.send(msg)
            if resp.error:
                return {"error": resp.error, "untrusted": True}
            return {"result": resp.result, "untrusted": True}
        except Exception as exc:
            return {"error": str(exc), "untrusted": True}

    async def _emit(self, typ: str, payload: dict) -> None:
        """写领域事件到 Event Log（非生命周期事件）。"""
        await self.log.append(typ, self.spec.id, payload)
