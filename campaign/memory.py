"""会话记忆层（M0）。

ScratchpadMemory  — 短期、per-task 工作记忆，纯内存 dict。
SessionMemory     — 中期情节记忆，从 EventLog 跨进程读取，按 run_id 隔离。

不导入 campaign.roles 或 campaign.app.runtime（避免循环引用）。
"""
from __future__ import annotations

from campaign.core.events import EventLog
from campaign.core.state import derive_state


class ScratchpadMemory:
    """短期 per-task 工作记忆 —— 纯内存 dict，无持久化，无外部依赖。"""

    def __init__(self) -> None:
        self._store: dict[str, object] = {}

    def set(self, key: str, value: object) -> None:
        """写入一个键值对。"""
        self._store[key] = value

    def get(self, key: str, default: object = None) -> object:
        """读取一个键值对；键不存在时返回 default。"""
        return self._store.get(key, default)

    def items(self) -> dict[str, object]:
        """返回内部存储的浅拷贝，用于快照/检查点回放（调用方可安全修改）。"""
        return dict(self._store)

    def clear(self) -> None:
        """清空全部键值对。"""
        self._store.clear()


class SessionMemory:
    """中期情节记忆 —— 从共享 EventLog 中按 run_id 提取历史任务摘要。

    纯读操作：不向事件流写入任何内容，跨进程安全（仅依赖 EventLog.replay）。
    """

    # recall / summary 关注的终端事件类型 → 状态名映射
    _TERMINAL_TYPES: dict[str, str] = {
        "task.done": "done",
        "task.failed": "failed",
        "task.frozen": "frozen",
        "task.input_required": "awaiting",
        "task.skipped": "skipped",
    }

    def __init__(self, log: EventLog) -> None:
        """
        Args:
            log: 共享的 EventLog 实例（通常是 SqliteEventLog）。
        """
        self.log = log

    async def recall(self, run_id: str, limit: int | None = None) -> list[dict]:
        """回放事件流，按 run_id 过滤，为每个到达终态/等待输入的任务生成一条摘要。

        摘要字段：
          - task_id: str
          - status:  "done" | "failed" | "frozen" | "awaiting" | "skipped"
          - agent:   事件 payload 中的 agent 字段（若无则为 None）
          - score:   事件 payload 中的 review_score 字段（若无则为 None）

        每个 task 取 seq 最大的终端事件；结果按事件 seq 升序排列。
        若指定 limit，仅返回最近的 N 条。
        """
        events = await self.log.replay()

        # 按 run_id 过滤 + 按 task_id 保留最新终端事件
        task_best: dict[str, tuple[int, object]] = {}  # task_id → (seq, Event)
        for event in events:
            if event.payload.get("run_id") != run_id:
                continue
            status = self._TERMINAL_TYPES.get(event.type)
            if status is None:
                continue
            task_id = event.payload.get("task_id", "")
            if not task_id:
                continue
            if task_id not in task_best or event.seq > task_best[task_id][0]:
                task_best[task_id] = (event.seq, event)

        # 按 seq 升序构造结果列表
        results: list[dict] = []
        for task_id, (seq, event) in sorted(task_best.items(), key=lambda kv: kv[1][0]):
            results.append(
                {
                    "task_id": task_id,
                    "status": self._TERMINAL_TYPES[event.type],
                    "agent": event.payload.get("agent"),
                    "score": event.payload.get("review_score"),
                }
            )

        if limit is not None:
            results = results[-limit:]

        return results

    async def summary(self, run_id: str) -> dict[str, int]:
        """返回指定 run_id 的各状态任务计数。

        Returns:
            {"done": n, "failed": n, "frozen": n, "awaiting": n, "skipped": n}
        """
        events = await self.log.replay()
        state = derive_state(events, run_id)
        return {
            "done": len(state.tasks_done),
            "failed": len(state.tasks_failed),
            "frozen": len(state.tasks_frozen),
            "awaiting": len(state.tasks_awaiting_input),
            "skipped": len(state.tasks_skipped),
        }
