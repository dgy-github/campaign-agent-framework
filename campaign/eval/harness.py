"""eval 驱动闭环（M8）。对应框架文档：演练时 / ⑨。

实弹考核：跑 eval 集 → 打分 → 未达阈值禁止"上线"。硬性门禁。
也负责把复盘产物导出为训练集（数据飞轮 → 蒸馏小模型 / OpenClaw-RL）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..core.events import EventLog
from ..core.models import ExecutionOrder


@dataclass
class EvalCase:
    id: str
    order: dict          # ExecutionOrder 序列化
    expected: dict       # golden 结果/标准
    weight: float = 1.0  # 用例权重


class EvalHarness:
    """eval 驱动机：跑用例 → 打分 → 门禁判断。

    门禁逻辑：所有用例的加权平均分 >= threshold 才允许"上线"。
    """

    def __init__(self, threshold: float = 0.8) -> None:
        self.threshold = threshold
        self._results: dict[str, dict] = {}

    async def run(self, runtime, cases: list[EvalCase]) -> float:
        """跑全部用例，返回加权总分。

        runtime: Runtime 实例（用于执行 order）。
        """
        total_weight = 0.0
        weighted_score = 0.0

        for case in cases:
            order = ExecutionOrder(**case.order)
            result = await runtime.run(order)
            score = self._score_result(result, case.expected)
            self._results[case.id] = {"case": case, "score": score, "result": result}
            weighted_score += score * case.weight
            total_weight += case.weight

        return weighted_score / total_weight if total_weight > 0 else 0.0

    def _score_result(self, result: dict, expected: dict) -> float:
        """对比执行结果与期望输出，计算 0..1 分。"""
        tasks_done = 0
        tasks_expected = expected.get("tasks_done", 1)
        min_score = expected.get("min_score", 0.5)

        for r in result.get("results", []):
            if r.get("status") == "done":
                tasks_done += 1
            # 检查 review 结果
            review = r.get("review", {})
            if review and not review.get("passed", True):
                return 0.0  # 验收失败 = 0 分

        completion = tasks_done / max(tasks_expected, 1)
        review_scores = [
            r.get("review", {}).get("score", 1.0)
            for r in result.get("results", [])
            if r.get("review")
        ]
        avg_review = sum(review_scores) / len(review_scores) if review_scores else 1.0

        return max(0.0, min(1.0, completion * 0.5 + avg_review * 0.5))

    async def gate(self, runtime, cases: list[EvalCase]) -> bool:
        """门禁：score >= threshold 才允许进入生产模式。"""
        score = await self.run(runtime, cases)
        return score >= self.threshold

    def export_trainset(self, log: EventLog, out_path: str) -> int:
        """把 Event Log 里的成功/失败案例导出为 jsonl 训练集，返回条数。

        注意：此方法为同步封装，真实异步版本需在 async 上下文中使用。
        """
        raise NotImplementedError(
            "export_trainset requires async context; use export_trainset_async instead"
        )

    async def export_trainset_async(self, log: EventLog, out_path: str) -> int:
        """异步版本：导出训练集。"""
        events = await log.replay(since=0)

        # 按 task_id 分组
        task_events: dict[str, list] = {}
        for ev in events:
            task_id = ev.payload.get("task_id", "")
            if task_id:
                task_events.setdefault(task_id, []).append({
                    "type": ev.type,
                    "actor": ev.actor,
                    "payload": ev.payload,
                })

        # 构建训练样本
        samples: list[dict] = []
        for task_id, evs in task_events.items():
            is_success = any(e["type"] == "task.done" for e in evs)
            is_failure = any(e["type"] == "task.failed" for e in evs)
            if is_success or is_failure:
                samples.append({
                    "task_id": task_id,
                    "status": "success" if is_success else "failure",
                    "events": evs,
                })

        with open(out_path, "w", encoding="utf-8") as f:
            for sample in samples:
                f.write(json.dumps(sample, ensure_ascii=False) + "\n")

        return len(samples)

    def results_summary(self) -> dict:
        """返回当前 eval 运行结果摘要。"""
        return {
            case_id: {"score": data["score"]}
            for case_id, data in self._results.items()
        }
