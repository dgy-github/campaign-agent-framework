"""随机突袭 / 混沌演练（M8）。对应框架文档：演练层（练机动不练剧本）。

随机让 agent 掉线，验证：协同制动是否及时、预备队是否正确补位、work-stealing 是否抢错。
"""
from __future__ import annotations

import random

from ..core.models import AgentSpec


class ChaosDrill:
    """混沌演练引擎：随机注入故障，验证系统韧性。

    用法:
        drill = ChaosDrill(seed=42)
        victim = drill.kill_random(agents)   # 选一个 agent 模拟掉线
        drill.inject_latency(agent_id, 5000) # 注入延迟
        drill.inject_error(agent_id, 0.5)    # 注入错误概率
    """

    def __init__(self, seed: int | None = None) -> None:
        self.rng = random.Random(seed)
        self._victims: set[str] = set()      # 已杀掉的 agent
        self._latency: dict[str, float] = {} # agent_id → injected latency ms
        self._error_rate: dict[str, float] = {}  # agent_id → error probability

    def kill_random(self, agents: list[AgentSpec]) -> AgentSpec:
        """随机选一个 agent 模拟掉线。

        返回被杀掉的 agent spec。该 agent 被标记为 victim，
        外部的 runtime 应检查 is_victim() 决定是否拒绝派活。
        """
        alive = [a for a in agents if a.id not in self._victims]
        if not alive:
            raise RuntimeError("No alive agents to kill")
        victim = self.rng.choice(alive)
        self._victims.add(victim.id)
        return victim

    def kill_by_role(self, agents: list[AgentSpec], role: str) -> list[AgentSpec]:
        """按角色批量杀掉 agent（模拟成建制减员）。"""
        victims = [a for a in agents if a.role == role and a.id not in self._victims]
        for v in victims:
            self._victims.add(v.id)
        return victims

    def is_victim(self, agent_id: str) -> bool:
        """该 agent 是否已被杀。"""
        return agent_id in self._victims

    def revive(self, agent_id: str) -> None:
        """复活 agent（演练结束恢复）。"""
        self._victims.discard(agent_id)
        self._latency.pop(agent_id, None)
        self._error_rate.pop(agent_id, None)

    def inject_latency(self, agent_id: str, latency_ms: float) -> None:
        """注入延迟（毫秒）。该 agent 所有调用附加此延迟。"""
        self._latency[agent_id] = latency_ms

    def inject_error(self, agent_id: str, error_rate: float) -> None:
        """注入错误概率（0..1）。每次调用有 error_rate 概率失败。"""
        self._error_rate[agent_id] = min(1.0, max(0.0, error_rate))

    def should_fail(self, agent_id: str) -> bool:
        """该 agent 此次调用是否应模拟失败（按 error_rate 概率）。"""
        rate = self._error_rate.get(agent_id, 0.0)
        return self.rng.random() < rate

    def get_latency(self, agent_id: str) -> float:
        """获取该 agent 的注入延迟（毫秒）。"""
        return self._latency.get(agent_id, 0.0)

    def reset(self) -> None:
        """重置所有注入（演练结束）。"""
        self._victims.clear()
        self._latency.clear()
        self._error_rate.clear()
