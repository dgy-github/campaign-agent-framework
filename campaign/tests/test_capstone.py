import pytest

from campaign.examples.capstone_demo import main


@pytest.mark.asyncio
async def test_capstone_demo_main_returns_success_summary():
    summary = await main()

    assert isinstance(summary, dict)
    assert summary["run_id"]
    assert summary["tasks_total"] >= 3
    assert summary["tasks_done"] > 0
    assert summary["tasks_failed"] == 0
    assert summary["events_total"] > 0
    assert summary["eval_passed"] is True


@pytest.mark.asyncio
async def test_capstone_demo_emits_required_events():
    summary = await main()
    event_counts = summary["event_counts"]

    assert event_counts["run.completed"] >= 1
    assert event_counts["task.done"] >= 1
    assert event_counts["knowledge.injected"] >= 1
