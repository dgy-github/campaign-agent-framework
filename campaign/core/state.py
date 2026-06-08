"""由事件回放派生的 State 视图（M0）。对应框架文档：运行时 2.2。

State 不可变存储——总是 replay(events) 计算得到。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from .events import Event


class State(BaseModel):
    """运行态势板的派生视图（只读快照）。"""

    tasks_pending: list[str] = Field(default_factory=list)
    tasks_done: list[str] = Field(default_factory=list)
    tasks_failed: list[str] = Field(default_factory=list)  # 失败任务（边界：之前会凭空消失）
    tasks_frozen: list[str] = Field(default_factory=list)  # 高难无人接 → 冻结排队
    tasks_awaiting_input: list[str] = Field(default_factory=list)
    tasks_skipped: list[str] = Field(default_factory=list)
    incidents: list[dict] = Field(default_factory=list)


def derive_state(events: list[Event], run_id: str | None = None) -> State:
    """从事件流回放出当前 State。

    按 event.type 折叠：assigned → pending; done → done; failed → failed;
    frozen → frozen; incident → incidents。**纯函数**：相同事件序列 → 相同 State。

    run_id 不为空时只折叠该 run 的事件（边界#1：多次运行互不混流）。
    """
    pending: set[str] = set()
    done: set[str] = set()
    failed: set[str] = set()
    frozen: set[str] = set()
    awaiting: set[str] = set()
    skipped: set[str] = set()
    incidents: list[dict] = []

    for event in events:
        if run_id is not None and event.payload.get("run_id") != run_id:
            continue  # 边界#1：只看本 run 的事件
        task_id = event.payload.get("task_id", "")
        match event.type:
            case "task.assigned":
                # 从其它状态移除（重试/再派），加入 pending
                frozen.discard(task_id)
                failed.discard(task_id)
                awaiting.discard(task_id)
                skipped.discard(task_id)
                pending.add(task_id)
            case "task.done":
                pending.discard(task_id)
                frozen.discard(task_id)
                failed.discard(task_id)
                awaiting.discard(task_id)
                skipped.discard(task_id)
                done.add(task_id)
            case "task.failed":
                pending.discard(task_id)
                frozen.discard(task_id)
                awaiting.discard(task_id)
                skipped.discard(task_id)
                failed.add(task_id)  # 边界：失败任务进 failed 集，不再凭空消失
            case "task.frozen":
                pending.discard(task_id)
                awaiting.discard(task_id)
                skipped.discard(task_id)
                frozen.add(task_id)
            case "task.input_required":
                pending.discard(task_id)
                awaiting.add(task_id)
            case "task.resumed":
                awaiting.discard(task_id)
                pending.add(task_id)
            case "task.skipped":
                pending.discard(task_id)
                awaiting.discard(task_id)
                skipped.add(task_id)
            case "incident":
                incidents.append(dict(event.payload))
            case _:
                # 未知事件类型：忽略或记录
                pass

    return State(
        tasks_pending=sorted(pending),
        tasks_done=sorted(done),
        tasks_failed=sorted(failed),
        tasks_frozen=sorted(frozen),
        tasks_awaiting_input=sorted(awaiting),
        tasks_skipped=sorted(skipped),
        incidents=incidents,
    )
