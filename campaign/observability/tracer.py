"""可观测性（M7）。对应框架文档：抗毁性 3.2。实时态势屏 + 黑匣子，看真实而非阅兵。

提供 trace/metering/在线打分能力。对接 OpenTelemetry 接口留好 adapter。
"""
from __future__ import annotations

import abc
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator


class Tracer(abc.ABC):
    """可观测性适配器基类。对接 OpenTelemetry / Langfuse 等。"""

    @abc.abstractmethod
    def span(self, name: str, actor: str):
        """上下文管理器：包裹一次 agent 调用，记录 trace。"""
        raise NotImplementedError

    @abc.abstractmethod
    def meter(self, actor: str, tokens: int, cost: float, latency_ms: float) -> None:
        """成本计量：token / 价格 / 耗时。喂给 CapacityLedger.health。"""
        raise NotImplementedError

    @abc.abstractmethod
    def score(self, task_id: str, score: float) -> None:
        """在线打分：运行中质量监控，过低可触发熔断。"""
        raise NotImplementedError


class InMemoryTracer(Tracer):
    """内存 Tracer 实现：用于测试和开发环境。

    不依赖外部可观测性系统，数据存内存中。
    """

    def __init__(self) -> None:
        self.traces: list[dict] = []
        self.metrics: list[dict] = []
        self.scores: dict[str, float] = {}

    @asynccontextmanager
    async def span(self, name: str, actor: str) -> AsyncIterator[None]:
        """记录一次 span。"""
        start = time.perf_counter()
        trace_entry = {
            "name": name,
            "actor": actor,
            "start": start,
            "status": "started",
        }
        try:
            yield
            trace_entry["status"] = "ok"
        except Exception:
            trace_entry["status"] = "error"
            raise
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            trace_entry["duration_ms"] = duration_ms if duration_ms > 0.0 else 0.001
            self.traces.append(trace_entry)

    def meter(self, actor: str, tokens: int, cost: float, latency_ms: float) -> None:
        self.metrics.append({
            "actor": actor,
            "tokens": tokens,
            "cost": cost,
            "latency_ms": latency_ms,
        })

    def score(self, task_id: str, score: float) -> None:
        self.scores[task_id] = score

    def recent_latency(self, actor: str, window: int = 10) -> float:
        """该 actor 最近 N 次调用的平均延迟。"""
        relevant = [m for m in self.metrics if m["actor"] == actor]
        if not relevant:
            return 0.0
        recent = relevant[-window:]
        return sum(m["latency_ms"] for m in recent) / len(recent)

    def recent_cost(self, actor: str, window: int = 10) -> float:
        """该 actor 最近 N 次调用的总花费。"""
        relevant = [m for m in self.metrics if m["actor"] == actor]
        if not relevant:
            return 0.0
        recent = relevant[-window:]
        return sum(m["cost"] for m in recent)

    def clear(self) -> None:
        self.traces.clear()
        self.metrics.clear()
        self.scores.clear()
