"""检查点 / 回滚（M4）。对应框架文档：抗毁性 3.1。

状态由事件派生，所以 checkpoint = 记录某个 seq；rollback = replay 到该 seq。
"""
from __future__ import annotations

from ..core.events import EventLog
from ..core.state import State, derive_state


class Checkpointer:
    """检查点管理器：snapshot 记录当前 seq 为检查点，rollback 重放到指定 seq。

    注意：rollback 不删除事件（Event Log 不可变），只返回该 seq 时的 State 视图。
    逻辑回滚（如重建状态）由上层根据返回的 State 决定。
    """

    def __init__(self, log: EventLog) -> None:
        self.log = log
        self._checkpoints: dict[str, int] = {}  # checkpoint_name → seq

    async def snapshot(self, name: str = "auto") -> int:
        """记录当前最新 seq 作为检查点，返回该 seq。

        name 用于区分多个检查点。
        """
        events = await self.log.replay(since=0)
        if events:
            seq = events[-1].seq
        else:
            seq = 0  # 空日志
        self._checkpoints[name] = seq
        return seq

    async def rollback(self, name: str = "auto", *, hard: bool = False) -> State:
        """回放到指定检查点得到 State。

        hard=False（默认）：逻辑回滚，只返回该 seq 的 State 视图，日志保留。
        hard=True：**真回滚**，物理截断 seq>checkpoint 的事件（log.truncate_after），
                   之后继续运行不会再把被回滚的事件算回来。
        """
        seq = self._checkpoints.get(name, 0)
        return await self.rollback_to_seq(seq, hard=hard)

    async def rollback_to_seq(self, seq: int, *, hard: bool = False) -> State:
        """回放到指定 seq 得到 State；hard=True 时物理截断之后的事件。"""
        if hard:
            await self.log.truncate_after(seq)
        events = await self.log.replay(since=0)
        rolled_back_events = [e for e in events if e.seq <= seq]
        return derive_state(rolled_back_events)

    def list_checkpoints(self) -> dict[str, int]:
        return dict(self._checkpoints)

    def clear(self) -> None:
        self._checkpoints.clear()
