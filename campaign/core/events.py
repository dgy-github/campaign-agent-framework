"""事件溯源地基（M0）。对应框架文档：运行时 2.2。

状态永远由事件回放派生，不存可变共享状态。
Event 模型已给全；EventLog 抽象 + SQLite 实现——使用标准库 sqlite3 + asyncio 线程池，
保持核心零外部依赖。
"""
from __future__ import annotations

import abc
import asyncio
import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class Event(BaseModel):
    seq: int  # 单调递增
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    type: str  # task.assigned / task.done / brake.signal / incident / governance.violation ...
    actor: str  # agent id 或 "governor"/"system"
    payload: dict[str, Any] = Field(default_factory=dict)


class EventLog(abc.ABC):
    """append-only 事件流；兼任"协同制动信号总线"。"""

    @abc.abstractmethod
    async def append(self, type: str, actor: str, payload: dict[str, Any]) -> Event:
        """追加一个事件并返回（含分配好的 seq）。"""
        raise NotImplementedError

    @abc.abstractmethod
    async def replay(self, since: int = 0) -> list[Event]:
        """回放 seq > since 的所有事件。"""
        raise NotImplementedError

    @abc.abstractmethod
    async def subscribe(self) -> AsyncIterator[Event]:
        """异步迭代新事件（用于协同制动广播 / 在线打分）。"""
        raise NotImplementedError

    @abc.abstractmethod
    async def truncate_after(self, seq: int) -> int:
        """删除 seq > 给定值的事件（真回滚/分叉用），返回删除条数。"""
        raise NotImplementedError


class SqliteEventLog(EventLog):
    """SQLite (jsonl 表) 实现。接口留好可换 Postgres。

    使用标准库 sqlite3 + asyncio (ThreadPoolExecutor) 实现，不引入 aiosqlite 等外部依赖。
    所有 DB 操作在专用线程中序列化，保证 seq 单调递增。
    """

    def __init__(self, db_path: str = "campaign_events.db") -> None:
        self.db_path = db_path
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="eventlog")
        self._subscribers: list[asyncio.Queue[Event]] = []
        self._closed = False
        self.dropped_events = 0  # 边界：订阅者队列满时丢弃的事件计数（不再静默）
        self._init_db_sync()

    # ── 同步（在线程池中运行）─────────────────────────────

    def _init_db_sync(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                "  seq   INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  ts    TEXT    NOT NULL,"
                "  type  TEXT    NOT NULL,"
                "  actor TEXT    NOT NULL,"
                "  payload TEXT  NOT NULL DEFAULT '{}'"
                ")"
            )
            conn.commit()
        finally:
            conn.close()

    def _append_sync(self, type: str, actor: str, payload: dict[str, Any]) -> Event:
        conn = sqlite3.connect(self.db_path)
        try:
            ts = datetime.now(timezone.utc).isoformat()
            payload_json = json.dumps(payload, ensure_ascii=False)
            cur = conn.execute(
                "INSERT INTO events (ts, type, actor, payload) VALUES (?, ?, ?, ?)",
                (ts, type, actor, payload_json),
            )
            seq = cur.lastrowid
            conn.commit()
            return Event(seq=seq, ts=datetime.fromisoformat(ts), type=type, actor=actor, payload=payload)
        finally:
            conn.close()

    def _replay_sync(self, since: int = 0) -> list[Event]:
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT seq, ts, type, actor, payload FROM events WHERE seq > ? ORDER BY seq ASC",
                (since,),
            ).fetchall()
            events: list[Event] = []
            for seq, ts, typ, actor, payload_json in rows:
                try:
                    payload = json.loads(payload_json) if payload_json else {}
                except json.JSONDecodeError:
                    payload = {}
                events.append(
                    Event(
                        seq=seq,
                        ts=datetime.fromisoformat(ts),
                        type=typ,
                        actor=actor,
                        payload=payload,
                    )
                )
            return events
        finally:
            conn.close()

    # ── 异步接口 ─────────────────────────────────────────

    async def _run_in_thread(self, fn, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, lambda: fn(*args, **kwargs))

    async def append(self, type: str, actor: str, payload: dict[str, Any]) -> Event:
        if self._closed:
            raise RuntimeError("EventLog closed")
        event = await self._run_in_thread(self._append_sync, type, actor, payload)
        # 广播给所有订阅者（边界#7：用快照迭代，避免广播时被增删订阅者）
        dropped = 0
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dropped += 1
        if dropped:
            self.dropped_events += dropped  # 边界：持久计数，可被监控查询
            logger.warning(
                "EventLog subscriber queue full; dropped %d for event seq=%s type=%s (total dropped=%d)",
                dropped, event.seq, event.type, self.dropped_events,
            )
        return event

    async def replay(self, since: int = 0) -> list[Event]:
        return await self._run_in_thread(self._replay_sync, since)

    def _truncate_sync(self, seq: int) -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.execute("DELETE FROM events WHERE seq > ?", (seq,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    async def truncate_after(self, seq: int) -> int:
        """物理删除 seq > 给定值的事件，返回删除条数（真回滚/分叉）。"""
        if self._closed:
            raise RuntimeError("EventLog closed")
        return await self._run_in_thread(self._truncate_sync, seq)

    async def subscribe(self) -> AsyncIterator[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=256)
        self._subscribers.append(q)
        try:
            while not self._closed:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=1.0)
                    yield event
                except asyncio.TimeoutError:
                    continue  # 超时后继续等待，同时检查 _closed
        finally:
            self._subscribers.remove(q)

    def close(self) -> None:
        """关闭 EventLog，释放线程池。"""
        self._closed = True
        self._executor.shutdown(wait=True)
