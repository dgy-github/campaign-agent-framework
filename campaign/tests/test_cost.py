import os
import tempfile

import pytest

from campaign.core.events import SqliteEventLog
from campaign.governance.gate import PolicyGate
from campaign.governance.governor import Governor
from campaign.governance.policy import BudgetRule
from campaign.llm.client import LLMClient, LLMError, TierConfig, extract_usage
from campaign.observability.tracer import InMemoryTracer


@pytest.fixture
def event_log():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="cost_")
    os.close(fd)
    log = SqliteEventLog(db_path=path)
    yield log
    log.close()
    try:
        os.unlink(path)
    except OSError:
        pass


def test_extract_usage_reads_openai_usage_fields():
    response = {"usage": {"prompt_tokens": 7, "completion_tokens": 11, "total_tokens": 18, "extra": 99}}

    assert extract_usage(response) == {
        "prompt_tokens": 7,
        "completion_tokens": 11,
        "total_tokens": 18,
    }


@pytest.mark.asyncio
async def test_llm_client_tracer_meters_real_tokens(event_log):
    tracer = InMemoryTracer()
    client = LLMClient(
        tiers={"coder": TierConfig(model="m", base_url="http://localhost")},
        mock_responses={
            "coder": [
                {
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }
            ]
        },
        tracer=tracer,
    )

    await client.complete("coder", [{"role": "user", "content": "hi"}], est_cost=1)

    assert tracer.metrics[0]["actor"] == "llm:coder"
    assert tracer.metrics[0]["tokens"] == 15
    assert tracer.metrics[0]["cost"] == 15.0
    await client.close()


@pytest.mark.asyncio
async def test_llm_gate_uses_real_usage_for_budget(event_log):
    gate = PolicyGate(Governor(event_log, rules=[BudgetRule()]))
    client = LLMClient(
        tiers={"coder": TierConfig(model="m", base_url="http://localhost")},
        mock_responses={
            "coder": [
                {
                    "choices": [{"message": {"content": "too much"}}],
                    "usage": {"prompt_tokens": 80, "completion_tokens": 50, "total_tokens": 130},
                }
            ]
        },
        gate=gate,
        gate_ctx={"actor_role": "executor", "budget": {"token_limit": 100}},
    )

    with pytest.raises(LLMError, match="blocked by governor"):
        await client.complete("coder", [{"role": "user", "content": "hi"}], est_cost=1)

    events = await event_log.replay()
    violation = next(e for e in events if e.type == "governance.violation")
    assert "130.0" in violation.payload["violations"][0]
    await client.close()
