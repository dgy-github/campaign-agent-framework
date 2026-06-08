"""动员与减员（M5）。对应框架文档：抗毁性 3.3 / ④互助 / ⑤。

减员检测 + 三级响应（替补/降级运转/战时动员）+ 兵力台账 Capacity Ledger。
"""
from __future__ import annotations

from enum import Enum

from ..core.events import EventLog
from ..core.models import AgentSpec


class AttritionLevel(str, Enum):
    NONE = "none"
    LIGHT = "light"      # 单点失败 → 重试+替补
    MEDIUM = "medium"    # 某类能力大面积失能 → 降级运转
    SEVERE = "severe"    # 核心能力崩溃 → 战时动员


class CapacityLedger:
    """兵力台账：现役 / 预备役 / 配额水位 / 健康度。数据来自可观测性层。"""

    def __init__(self) -> None:
        self.active: dict[str, AgentSpec] = {}
        self.reserve: dict[str, AgentSpec] = {}
        self.quota: dict[str, float] = {}      # 剩余 token/调用/预算
        self.health: dict[str, float] = {}     # agent -> 近期成功率

    def register_active(self, spec: AgentSpec) -> None:
        self.active[spec.id] = spec
        self.health.setdefault(spec.id, 1.0)

    def register_reserve(self, spec: AgentSpec) -> None:
        self.reserve[spec.id] = spec
        self.health.setdefault(spec.id, 1.0)

    def mark_unhealthy(self, agent_id: str, health: float) -> None:
        """标记健康度（0..1）。"""
        self.health[agent_id] = max(0.0, min(1.0, health))

    def active_count(self) -> int:
        """健康活跃的现役 agent 数量。"""
        return sum(
            1 for aid in self.active
            if self.health.get(aid, 0.0) > 0.3
        )

    def reserve_count(self) -> int:
        """健康预备役数量。"""
        return sum(
            1 for rid in self.reserve
            if self.health.get(rid, 0.0) > 0.3
        )

    def assess(self) -> AttritionLevel:
        """根据 health 评估当前减员等级。

        判定逻辑：
        - 核心现役全部健康 → NONE
        - 少数 agent (<30%) 不健康 → LIGHT
        - 多数 agent (30-60%) 不健康 → MEDIUM
        - 超多半 agent (>60%) 不健康或配额耗尽 → SEVERE
        """
        if not self.active:
            return AttritionLevel.NONE

        total = len(self.active)
        unhealthy = sum(
            1 for aid in self.active
            if self.health.get(aid, 1.0) < 0.5
        )
        ratio = unhealthy / total if total > 0 else 0.0

        if unhealthy <= 1 or ratio < 0.35:
            return AttritionLevel.LIGHT if unhealthy > 0 else AttritionLevel.NONE
        elif ratio < 0.6:
            return AttritionLevel.MEDIUM
        else:
            return AttritionLevel.SEVERE


class Mobilizer:
    """动员器：检测减员并执行三级响应。"""

    def __init__(self, log: EventLog, ledger: CapacityLedger) -> None:
        self.log = log
        self.ledger = ledger

    async def respond(self, level: AttritionLevel) -> dict:
        """三级响应。

        LIGHT：替补（预备役顶单点失败）。
        MEDIUM：降级运转（弱模型顶 + Reviewer 加严 + 先 checkpoint）。
        SEVERE：战时动员（扩编/切厂商/优先级重排/超出则上报 human-in-the-loop）。

        返回响应摘要。
        """
        response = {"level": level.value, "actions": []}

        if level == AttritionLevel.LIGHT:
            # 轻度：启用预备役替补
            if self.ledger.reserve:
                activated = []
                for rid, rspec in list(self.ledger.reserve.items())[:2]:  # 最多启用 2 个
                    self.ledger.active[rid] = rspec
                    self.ledger.health[rid] = 0.8
                    activated.append(rid)
                    await self.log.append("mobilization.substitute", "mobilizer", {"reserve_id": rid})
                response["actions"].append(f"activated reserves: {activated}")
            else:
                response["actions"].append("no reserves available for substitution")

        elif level == AttritionLevel.MEDIUM:
            # 中度：降级运转
            # 1. checkpoint
            await self.log.append("mobilization.checkpoint", "mobilizer", {"reason": "medium attrition"})
            response["actions"].append("checkpoint taken")

            # 2. 弱模型顶上（预备队接 simple/medium 任务）
            for rid, rspec in self.ledger.reserve.items():
                self.ledger.active.setdefault(rid, rspec)
                self.ledger.health[rid] = 0.7
            response["actions"].append(f"degraded mode: {len(self.ledger.reserve)} reserves activated, reviewer strictness increased")

            # 3. Reviewer 加严信号
            await self.log.append("mobilization.reviewer_strict", "mobilizer", {"strictness_increase": 0.2})

        elif level == AttritionLevel.SEVERE:
            # 重度：战时动员
            # 1. 全部预备队激活
            for rid, rspec in self.ledger.reserve.items():
                self.ledger.active[rid] = rspec
                self.ledger.health[rid] = 0.5
            response["actions"].append(f"full mobilization: all {len(self.ledger.reserve)} reserves activated")

            # 2. 优先级重排信号
            await self.log.append("mobilization.reprioritize", "mobilizer", {"reason": "severe attrition"})

            # 3. 上报 human-in-the-loop
            await self.log.append("mobilization.human_escalation", "mobilizer", {
                "level": "severe",
                "active_count": self.ledger.active_count(),
                "reserve_count": self.ledger.reserve_count(),
            })
            response["actions"].append("human-in-the-loop escalation triggered")

        await self.log.append("mobilization.response", "mobilizer", response)
        return response
