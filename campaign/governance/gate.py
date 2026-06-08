"""统一执行闸 PolicyGate（深层#1 并发安全 + #3 全链路治理）。

把"督军校验"收口成单一入口：
- 内含 asyncio.Lock：在真并行下序列化预算等读改写状态（避免竞争）。
- 被 Runtime（每任务 spend）与 LLMClient（每次调用 spend/egress）共享，
  让治理从"派活一点 vet"扩展到"每个真实动作边界都 vet"。
"""
from __future__ import annotations

import asyncio

from .governor import Governor
from .policy import Action


class PolicyGate:
    def __init__(self, governor: Governor, lock: asyncio.Lock | None = None) -> None:
        self.governor = governor
        self._lock = lock or asyncio.Lock()

    async def check(self, action: Action, context: dict) -> bool:
        """并发安全地过督军：返回 True 放行，False 拦截（已写 governance.violation）。"""
        async with self._lock:
            return await self.governor.vet(action, context)
