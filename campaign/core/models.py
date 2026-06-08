"""公共数据模型。对应框架文档：① 意图层、运行时 2.1、治理(能力表)。

这些是纯数据结构，已给出完整定义（M0 可直接用，不需要填实现）。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

ModelTier = Literal["flagship", "coder", "value", "small"]
Role = Literal["coordinator", "executor", "retriever", "reviewer", "reserve"]
Difficulty = Literal["simple", "medium", "hard"]


class AgentSpec(BaseModel):
    """一个 agent / 部队的声明。skills 即"会烧饭/会射箭"。"""

    id: str
    role: Role
    model_tier: ModelTier
    skills: list[str] = Field(default_factory=list)
    proficiency: dict[str, int] = Field(default_factory=dict)  # skill -> 0..100


class Task(BaseModel):
    """意图层拆出的单个任务。difficulty + degradable 驱动减员时的降级决策。"""

    id: str
    goal: str
    difficulty: Difficulty = "medium"
    degradable: bool = True
    required_skills: list[str] = Field(default_factory=list)
    acceptance: str = ""  # 验收标准（交给 Reviewer）
    needs_approval: bool = False
    human_input: dict = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)


class ExecutionOrder(BaseModel):
    """意图层产出：带验收标准的结构化任务书（执行令）。"""

    objective: str
    constraints: list[str] = Field(default_factory=list)  # 红线：成本/安全/合规
    budget: dict = Field(default_factory=dict)  # token/$/时间上限
    tasks: list[Task] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_unique_task_ids(self) -> "ExecutionOrder":
        """边界：拒绝重复 task id（否则事件流/派生状态会张冠李戴）。"""
        ids = [t.id for t in self.tasks]
        dups = sorted({i for i in ids if ids.count(i) > 1})
        if dups:
            raise ValueError(f"duplicate task ids in ExecutionOrder: {dups}")

        id_set = set(ids)
        missing = sorted({dep for task in self.tasks for dep in task.depends_on if dep not in id_set})
        if missing:
            raise ValueError(f"unknown task dependencies in ExecutionOrder: {missing}")

        deps = {task.id: list(task.depends_on) for task in self.tasks}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visited:
                return
            if task_id in visiting:
                raise ValueError(f"cyclic task dependency in ExecutionOrder: {task_id}")
            visiting.add(task_id)
            for dep in deps[task_id]:
                visit(dep)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in ids:
            visit(task_id)
        return self
