"""ROI 路由（M3）。对应框架文档：运行时 2.3。

按 置信度 + 成本 + 历史成功率 三信号打分选 agent；成功率从 Event Log 统计。
一次 replay 聚合所有 agent 统计，避免 O(候选数×全部事件) 的 N+1 查询。
"""
from __future__ import annotations

from ..core.events import EventLog
from ..core.models import AgentSpec, Task
from .skill_registry import SkillRegistry


class RouteScore:
    """路由打分：三信号 + 可解释的加权总分。"""

    COST_NORM_DIVISOR = 1000.0  # 成本归一化基准（边界：常量化，避免散落硬编码）

    def __init__(
        self,
        agent_id: str,
        confidence: float,
        cost: float,
        success_rate: float,
        proficiency_bonus: float = 0.0,
    ) -> None:
        self.agent_id = agent_id
        self.confidence = confidence       # 0..1
        self.cost = cost                   # 估算 token/$ 消耗
        self.success_rate = success_rate   # 0..1，历史成功率
        self.proficiency_bonus = proficiency_bonus

    @property
    def total(self) -> float:
        """综合分：高置信/高成功率/低成本者优先。"""
        cost_norm = min(self.cost / self.COST_NORM_DIVISOR, 1.0)
        return (
            0.35 * self.confidence
            + 0.15 * (1.0 - cost_norm)
            + 0.40 * self.success_rate
            + 0.10 * self.proficiency_bonus
        )


class Router:
    """ROI 路由器。"""

    TIER_COST: dict[str, int] = {"flagship": 500, "coder": 300, "value": 100, "small": 50}
    DIFFICULTY_CONFIDENCE: dict[str, float] = {"simple": 0.9, "medium": 0.6, "hard": 0.3}

    def __init__(self, registry: SkillRegistry, log: EventLog) -> None:
        self.registry = registry
        self.log = log
        self._aggregated: dict[str, dict[str, int]] | None = None  # cache

    async def _aggregate_stats(self) -> dict[str, dict[str, int]]:
        """一次 replay 聚合所有 agent 的 assigned/done 计数。

        返回: {agent_id: {"assigned": N, "done": M}}。
        """
        events = await self.log.replay(since=0)
        stats: dict[str, dict[str, int]] = {}
        for ev in events:
            s = stats.setdefault(ev.actor, {"assigned": 0, "done": 0})
            if ev.type == "task.assigned":
                s["assigned"] += 1
            elif ev.type == "task.done":
                s["done"] += 1
        return stats

    async def success_rate(self, agent_id: str) -> float:
        """从聚合缓存中获取该 agent 的历史成功率。"""
        stats = await self._aggregate_stats()
        s = stats.get(agent_id, {"assigned": 0, "done": 0})
        if s["assigned"] == 0:
            return 0.5  # 无历史 → 中性
        return s["done"] / s["assigned"]

    async def pick(self, task: Task) -> AgentSpec | None:
        """选最优 agent；无候选返回 None。"""
        candidates = self.registry.candidates(task)
        if not candidates:
            return None

        stats = await self._aggregate_stats()  # 一次回放，供所有候选复用

        scores: list[RouteScore] = []
        for spec in candidates:
            conf = self.DIFFICULTY_CONFIDENCE.get(task.difficulty, 0.5)
            cost = float(self.TIER_COST.get(spec.model_tier, 200))
            s = stats.get(spec.id, {"assigned": 0, "done": 0})
            rate = s["done"] / s["assigned"] if s["assigned"] > 0 else 0.5
            prof_sum = sum(spec.proficiency.get(s, 0) for s in task.required_skills) if task.required_skills else 0
            prof_bonus = min(prof_sum / 500.0, 0.1)
            scores.append(RouteScore(spec.id, conf, cost, rate, prof_bonus))

        # 边界：同分时按 agent_id 升序稳定决断（避免任选导致不确定）
        scores.sort(key=lambda s: (-s.total, s.agent_id))
        return self.registry.get(scores[0].agent_id)
