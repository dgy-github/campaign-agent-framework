"""督军 Governor（M6）。对应框架文档：⑦治理层。

独立于指挥链：只管制度不管业务，违规即拦截并独立上报，不受 Coordinator 节制。
"""
from __future__ import annotations

from ..core.events import EventLog
from .policy import Action, Rule


class Governor:
    """督军：独立监察所有动作，违反红线即拦截。

    context["cumulative_spend"] 由 Governor 管理，跨多次 vet 调用累计。
    """

    def __init__(self, log: EventLog, rules: list[Rule] | None = None) -> None:
        self.log = log
        self.rules: list[Rule] = list(rules or [])
        self._cumulative_spend: float = 0.0

    def add_rule(self, rule: Rule) -> None:
        self.rules.append(rule)

    async def vet(self, action: Action, context: dict) -> bool:
        """逐条规则校验。任一违规 → 写 governance.violation 事件并返回 False（拦截）。

        context 被注入 cumulative_spend 供 BudgetRule 使用。
        """
        # 注入累计花费
        ctx = dict(context)
        ctx["cumulative_spend"] = self._cumulative_spend

        violations: list[str] = []
        for rule in self.rules:
            reason = rule.check(action, ctx)
            if reason is not None:
                violations.append(f"[{rule.name}] {reason}")

        # 更新累计花费（BudgetRule 写入 _new_spend）
        if "_new_spend" in ctx:
            self._cumulative_spend = ctx["_new_spend"]

        if violations:
            await self.log.append(
                "governance.violation",
                "governor",
                {
                    "actor": action.actor,
                    "kind": action.kind,
                    "violations": violations,
                    "action_payload": action.payload,
                },
            )
            return False
        return True
