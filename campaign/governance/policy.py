"""Policy-as-Code（M6）。对应框架文档：⑦治理层。

规则对所有 agent 一视同仁（Coordinator 自己也受约束）。

设计约定：
- BudgetRule 的花费累计记在 context["cumulative_spend"] 里，不依赖模块级单例共享状态。
- 每个 Governor 实例独立持有规则实例，防止跨 run 串预算。
"""
from __future__ import annotations

import abc
import re
from dataclasses import dataclass, field


@dataclass
class Action:
    """一次受监察的动作。"""

    actor: str
    kind: str            # tool_call / data_egress / spend / dispatch / ...
    payload: dict
    cost: float = 0.0
    sensitive: bool = False


class Rule(abc.ABC):
    name: str

    @abc.abstractmethod
    def check(self, action: Action, context: dict) -> str | None:
        """返回 None 表示通过；返回字符串表示违规原因。

        context 可包含:
        - budget: dict (token_limit 等)
        - cumulative_spend: float (累计花费，由 Governor 管理)
        - privacy_strict: bool
        - deploy_mode: str
        - is_local: bool
        - actor_role: str
        """
        raise NotImplementedError


class BudgetRule(Rule):
    """超预算检查。花费累计通过 context["cumulative_spend"] 读取，不由 rule 实例持有。"""

    name = "no_overspend"

    def check(self, action: Action, context: dict) -> str | None:
        budget = context.get("budget", {}).get("token_limit", 100.0)
        # 累计花费由 Governor 管理，通过 context 传入
        cumulative = context.get("cumulative_spend", 0.0) + action.cost
        # 将新累计写回 context（Governor 负责持久化）
        context["_new_spend"] = cumulative
        if cumulative > budget:
            return (
                f"budget exceeded: cumulative spend {cumulative:.1f} > "
                f"limit {budget}. Actor: {action.actor}"
            )
        return None


class DataEgressRule(Rule):
    """数据出域检查（hybrid/隐私模式下 sensitive 数据禁止外发）。"""

    name = "no_data_egress"

    def check(self, action: Action, context: dict) -> str | None:
        if not action.sensitive:
            return None
        privacy_strict = context.get("privacy_strict", True)
        is_local = context.get("is_local", False)

        if privacy_strict and action.kind == "data_egress" and not is_local:
            return (
                f"sensitive data egress blocked: actor={action.actor}, "
                f"kind={action.kind}, local_only"
            )
        return None


class AuthorityRule(Rule):
    """越权检查（actor 是否在其角色权限内）。"""

    name = "no_privilege_escalation"

    # data_egress 不归本规则管隐私（那是 DataEgressRule 的职责）——这里只防"角色越权"，
    # 故对会发起 LLM/工具调用的角色一律允许 data_egress，让隐私判定交给 DataEgressRule。
    ROLE_PERMISSIONS: dict[str, set[str]] = {
        "coordinator": {"dispatch", "decompose", "spend"},
        "executor": {"tool_call", "execute", "spend", "data_egress"},
        "reviewer": {"review", "score", "spend"},
        "retriever": {"retrieve", "search", "spend", "data_egress"},
        "reserve": {"execute", "tool_call", "spend", "data_egress"},
        "governor": {"vet", "report"},
    }

    def check(self, action: Action, context: dict) -> str | None:
        actor_role = context.get("actor_role", "")
        if not actor_role:
            return None

        allowed = self.ROLE_PERMISSIONS.get(actor_role, set())
        if action.kind not in allowed:
            return (
                f"privilege escalation: actor={action.actor} (role={actor_role}) "
                f"attempted {action.kind}, allowed: {allowed}"
            )
        return None


class InjectionScanRule(Rule):
    """Block untrusted content that looks like prompt injection."""

    name = "no_prompt_injection"
    _pattern = re.compile(
        r"ignore previous instructions|disregard above|system prompt|you are now|忽略(以上|之前)指令",
        re.IGNORECASE,
    )

    def check(self, action: Action, context: dict) -> str | None:
        if action.kind not in {"ingest", "a2a_recv"}:
            return None
        text = str(action.payload.get("text", ""))
        if self._pattern.search(text):
            return "prompt injection pattern detected in untrusted content"
        return None


def make_default_rules() -> list[Rule]:
    """工厂函数：每次调用返回新实例，避免共享状态。"""
    return [BudgetRule(), DataEgressRule(), AuthorityRule(), InjectionScanRule()]
