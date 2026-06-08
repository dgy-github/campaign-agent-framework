"""能力注册表（M3）——会烧饭/会射箭。对应框架文档：治理(能力表)。"""
from __future__ import annotations

from ..core.models import AgentSpec, Task


class SkillRegistry:
    """能力注册表：按技能标签查找可用 agent。"""

    def __init__(self) -> None:
        self._agents: dict[str, AgentSpec] = {}

    def register(self, spec: AgentSpec) -> None:
        self._agents[spec.id] = spec

    def unregister(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)

    def candidates(self, task: Task) -> list[AgentSpec]:
        """返回具备 task.required_skills 的 agent。无人具备 → 空列表（上层冻结排队）。

        按 proficiency 降序排列，优先选熟练度高的。
        """
        if not task.required_skills:
            # 无技能要求 → 所有 agent 都是候选
            return list(self._agents.values())

        matched: list[AgentSpec] = []
        for spec in self._agents.values():
            if all(s in spec.skills for s in task.required_skills):
                matched.append(spec)

        # 按该任务所需技能的总熟练度降序
        def _proficiency_sum(spec: AgentSpec) -> int:
            return sum(spec.proficiency.get(s, 0) for s in task.required_skills)

        matched.sort(key=_proficiency_sum, reverse=True)
        return matched

    def get(self, agent_id: str) -> AgentSpec | None:
        return self._agents.get(agent_id)

    def list_by_skill(self, skill: str) -> list[AgentSpec]:
        """按技能标签反查：哪些 agent 会某个技能。"""
        return [s for s in self._agents.values() if skill in s.skills]

    def __len__(self) -> int:
        return len(self._agents)
