import pytest

from campaign.core.events import SqliteEventLog
from campaign.memory import ScratchpadMemory, SessionMemory


@pytest.fixture
def sqlite_log(tmp_path):
    db_path = tmp_path / "memory.db"
    log = SqliteEventLog(db_path=str(db_path))
    try:
        yield log, db_path
    finally:
        log.close()


def test_scratchpad_memory_set_get_items_and_clear():
    memory = ScratchpadMemory()

    assert memory.get("missing") is None
    assert memory.get("missing", "fallback") == "fallback"

    value = {"step": 1}
    memory.set("draft", value)
    memory.set("count", 2)

    assert memory.get("draft") == value
    assert memory.items() == {"draft": value, "count": 2}

    snapshot = memory.items()
    snapshot["count"] = 99
    assert memory.get("count") == 2

    memory.clear()
    assert memory.items() == {}


@pytest.mark.asyncio
async def test_session_memory_recall_filters_run_summarizes_statuses_and_limits(sqlite_log):
    log, _ = sqlite_log
    memory = SessionMemory(log)

    await log.append("task.assigned", "coordinator", {"run_id": "run-a", "task_id": "t1", "agent": "exec-1"})
    await log.append(
        "task.done",
        "coordinator",
        {"run_id": "run-a", "task_id": "t1", "agent": "exec-1", "review_score": 0.9},
    )
    await log.append("task.assigned", "coordinator", {"run_id": "run-other", "task_id": "other"})
    await log.append(
        "task.done",
        "coordinator",
        {"run_id": "run-other", "task_id": "other", "agent": "exec-other", "review_score": 1.0},
    )
    await log.append("task.assigned", "coordinator", {"run_id": "run-a", "task_id": "t2", "agent": "exec-2"})
    await log.append("task.failed", "coordinator", {"run_id": "run-a", "task_id": "t2", "agent": "exec-2"})
    await log.append("task.assigned", "coordinator", {"run_id": "run-a", "task_id": "t3"})
    await log.append("task.frozen", "coordinator", {"run_id": "run-a", "task_id": "t3", "reason": "no_agent"})
    await log.append("task.assigned", "coordinator", {"run_id": "run-a", "task_id": "t4"})
    await log.append("task.skipped", "coordinator", {"run_id": "run-a", "task_id": "t4", "reason": "dependency"})
    await log.append("task.assigned", "coordinator", {"run_id": "run-a", "task_id": "t5"})
    await log.append("task.input_required", "coordinator", {"run_id": "run-a", "task_id": "t5", "prompt": "approve"})

    assert await memory.recall("run-a") == [
        {"task_id": "t1", "status": "done", "agent": "exec-1", "score": 0.9},
        {"task_id": "t2", "status": "failed", "agent": "exec-2", "score": None},
        {"task_id": "t3", "status": "frozen", "agent": None, "score": None},
        {"task_id": "t4", "status": "skipped", "agent": None, "score": None},
        {"task_id": "t5", "status": "awaiting", "agent": None, "score": None},
    ]

    assert await memory.recall("run-a", limit=2) == [
        {"task_id": "t4", "status": "skipped", "agent": None, "score": None},
        {"task_id": "t5", "status": "awaiting", "agent": None, "score": None},
    ]
    assert await memory.summary("run-a") == {
        "done": 1,
        "failed": 1,
        "frozen": 1,
        "awaiting": 1,
        "skipped": 1,
    }
    assert await memory.recall("run-other") == [
        {"task_id": "other", "status": "done", "agent": "exec-other", "score": 1.0},
    ]
    assert await memory.summary("run-other") == {
        "done": 1,
        "failed": 0,
        "frozen": 0,
        "awaiting": 0,
        "skipped": 0,
    }


@pytest.mark.asyncio
async def test_session_memory_recall_tracks_latest_task_outcome(sqlite_log):
    log, _ = sqlite_log
    memory = SessionMemory(log)

    await log.append("task.assigned", "coordinator", {"run_id": "run-a", "task_id": "t1"})
    await log.append("task.input_required", "coordinator", {"run_id": "run-a", "task_id": "t1"})
    await log.append(
        "task.done",
        "coordinator",
        {"run_id": "run-a", "task_id": "t1", "agent": "exec-1", "review_score": 0.8},
    )

    assert await memory.recall("run-a") == [
        {"task_id": "t1", "status": "done", "agent": "exec-1", "score": 0.8},
    ]
    assert await memory.summary("run-a") == {
        "done": 1,
        "failed": 0,
        "frozen": 0,
        "awaiting": 0,
        "skipped": 0,
    }


@pytest.mark.asyncio
async def test_session_memory_reads_same_sqlite_database_from_new_log(sqlite_log):
    log_a, db_path = sqlite_log

    await log_a.append("task.assigned", "coordinator", {"run_id": "run-a", "task_id": "t1"})
    await log_a.append(
        "task.done",
        "coordinator",
        {"run_id": "run-a", "task_id": "t1", "agent": "exec-1", "review_score": 0.7},
    )
    log_a.close()

    log_b = SqliteEventLog(db_path=str(db_path))
    try:
        memory = SessionMemory(log_b)

        assert await memory.recall("run-a") == [
            {"task_id": "t1", "status": "done", "agent": "exec-1", "score": 0.7},
        ]
    finally:
        log_b.close()
