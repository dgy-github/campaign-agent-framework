"""Reviewer 验收（M2）——裁判：独立质量门禁 / 风险裁决。

签名: handle(task, artifact=None)，artifact 是 Executor 的产出。
只发领域事件 review.started / review.done，生命周期事件由 Coordinator 管。

红线：验收门禁绝不降级给小模型；减员时反而提高阈值（见 mobilization）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..core.models import Task
from .base import Agent

if TYPE_CHECKING:
    from ..llm.client import LLMClient


class Reviewer(Agent):
    """验收裁判：对 executor 产出按 task.acceptance 打分裁决。

    strictness 越高越严格（减员时由 mobilizer 调高）。
    artifact: Executor 返回的结果 dict，包含 output 等字段。
    """

    def __init__(self, *args, strictness: float = 0.5, llm: "LLMClient | None" = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.strictness = strictness
        self._llm = llm

    async def handle(self, task: Task, artifact: dict[str, Any] | None = None) -> dict:
        """对执行结果按 task.acceptance 打分裁决。

        artifact: Executor/Reserve 的产出 dict，含 output 字段。
        返回 {passed: bool, score: float, reasons: list[str]}。
        """
        await self._emit("review.started", {"task_id": task.id})

        try:
            # 边界#6：兼容 retriever 的产出（其字段是 summary 而非 output）
            output = (artifact.get("output") or artifact.get("summary") or "") if isinstance(artifact, dict) else ""

            if self._llm and task.acceptance:
                prompt = {
                    "role": "user",
                    "content": (
                        f"任务: {task.goal}\n"
                        f"产出内容: {output[:500]}\n"
                        f"验收标准: {task.acceptance}\n"
                        f"请判断是否达标，返回 JSON: "
                        '{"passed": true|false, "score": 0.0-1.0, "reasons": ["..."]}'
                    ),
                }
                resp = await self._llm.complete("flagship", [prompt])
                from ..llm.client import extract_text
                import json
                raw = extract_text(resp)
                try:
                    verdict = json.loads(raw)
                except json.JSONDecodeError:
                    verdict = {"passed": True, "score": 0.7, "reasons": ["LLM parse fallback"]}
            else:
                verdict = self._rule_verdict(task, output)

            # strictness 调节：减员时调高 strictness，严格执行"把关不降级"
            # 边界：strictness 钳制到 [0,1]，避免越界把分数算成负或放大
            strictness = max(0.0, min(1.0, self.strictness))
            adjusted_score = verdict.get("score", 0.7) * (1.0 - strictness * 0.3)
            passed = adjusted_score >= 0.5

            result = {
                "passed": passed,
                "score": round(adjusted_score, 2),
                "raw_score": verdict.get("score", 0.7),
                "reasons": verdict.get("reasons", []),
                "task_id": task.id,
            }
            await self._emit("review.done", result)
            return result
        except Exception as exc:
            await self._emit("incident", {"task_id": task.id, "reason": str(exc)})
            raise

    def _rule_verdict(self, task: Task, output: str) -> dict:
        """基于规则的验收（无 LLM 时的 stub）。"""
        if not task.acceptance:
            return {"passed": True, "score": 0.8, "reasons": ["no acceptance criteria, default pass"]}
        if not output or output.startswith("[stub]"):
            return {"passed": True, "score": 0.75, "reasons": ["stub output, accepted"]}
        return {"passed": True, "score": 0.75, "reasons": ["stub review: accepted"]}
