"""熔断 + 协同制动（M4）。对应框架文档：抗毁性 3.1 / ④协同制动。

单点熔断 = 停一个 agent；协同制动 = 向 Event Log 广播 brake.signal，
上游 Coordinator 背压暂停派活、同链路降并发，防连锁雪崩。
"""
from __future__ import annotations

import time

from ..core.events import EventLog


class CircuitBreaker:
    """熔断器：累计 agent 失败次数，达阈值触发熔断。

    协同制动：熔断后广播 brake.signal 到 Event Log，
    同链路的其他 agent 也可以监听该信号主动降并发。
    """

    def __init__(self, log: EventLog, fail_threshold: int = 3, cooldown_sec: float = 30.0) -> None:
        self.log = log
        self.fail_threshold = fail_threshold
        self.cooldown_sec = cooldown_sec
        self._fails: dict[str, int] = {}          # agent_id → 连续失败次数
        self._tripped: dict[str, float] = {}      # agent_id → 熔断触发时间戳
        self._link_braking: dict[str, bool] = {}  # link → 是否制动中

    def record_failure(self, agent_id: str) -> bool:
        """累计失败；达阈值返回 True（应熔断）。

        返回 True 时会将 agent_id 加入 _tripped。
        """
        self._fails[agent_id] = self._fails.get(agent_id, 0) + 1
        if self._fails[agent_id] >= self.fail_threshold:
            self._tripped[agent_id] = time.monotonic()
            return True
        return False

    def record_success(self, agent_id: str) -> None:
        """成功后重置该 agent 的失败计数。"""
        self._fails[agent_id] = 0

    def is_tripped(self, agent_id: str) -> bool:
        """该 agent 是否处于熔断状态。

        冷却时间过后自动恢复。
        """
        tripped_at = self._tripped.get(agent_id)
        if tripped_at is None:
            return False
        if time.monotonic() - tripped_at > self.cooldown_sec:
            # 冷却期过，自动恢复
            self._tripped.pop(agent_id, None)
            self._fails[agent_id] = 0
            return False
        return True

    async def broadcast_brake(self, link: str, reason: str) -> None:
        """协同制动：广播 brake.signal 到 Event Log（刹车信号总线）。

        link 标识受影响的链路（如 "execution"、"retrieval"），
        同链路 agent 和 Coordinator 监听该信号后减速/停派活。
        """
        self._link_braking[link] = True
        await self.log.append(
            "brake.signal",
            "circuit_breaker",
            {"link": link, "reason": reason},
        )

    async def release_brake(self, link: str) -> None:
        """释放制动。"""
        self._link_braking[link] = False
        await self.log.append(
            "brake.released",
            "circuit_breaker",
            {"link": link},
        )

    async def is_braking(self, link: str) -> bool:
        """该链路是否处于制动中（Coordinator 据此背压停派活）。

        从 Event Log 中检查最新的 brake.signal 是否未被 release。
        """
        # 边界#5：制动状态全程在内存维护（broadcast_brake/release_brake 同进程更新），
        # 不再每次 dispatch 全量回放 Event Log（避免 O(N) 随日志无界增长）。
        return self._link_braking.get(link, False)

    def reset(self) -> None:
        """重置所有熔断状态（用于测试）。"""
        self._fails.clear()
        self._tripped.clear()
        self._link_braking.clear()
