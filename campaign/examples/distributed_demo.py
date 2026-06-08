"""Minimal distributed demo: node A routes work over HTTP to node B.

Node A and node B use separate SqliteEventLog instances pointed at the same DB
file. HTTP is exercised in-process with httpx.ASGITransport, so no port is
opened and no dependency beyond httpx is required.

Usage:
    python -m campaign.examples.distributed_demo
"""
from __future__ import annotations

import asyncio
import os
import tempfile

import httpx

from campaign.app.config import Config
from campaign.app.runtime import Runtime
from campaign.core.events import SqliteEventLog
from campaign.core.models import AgentSpec, ExecutionOrder, Task
from campaign.core.state import derive_state
from campaign.roles.executor import Executor
from campaign.roles.reviewer import Reviewer
from campaign.transport import HttpJsonRpcTransport, JsonRpcAgentServer


async def main() -> None:
    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="campaign_dist_")
    os.close(fd)

    log_a = SqliteEventLog(db_path)
    log_b = SqliteEventLog(db_path)
    transport: HttpJsonRpcTransport | None = None

    try:
        exec_spec = AgentSpec(id="exec", role="executor", model_tier="value", skills=["code"])
        review_spec = AgentSpec(id="reviewer-a", role="reviewer", model_tier="flagship", skills=["review"])

        node_b_executor = Executor(exec_spec, log_b)
        server = JsonRpcAgentServer(
            {"exec": node_b_executor},
            known_senders={"coordinator", "system", "exec"},
            log=log_b,
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.asgi_app()),
            base_url="http://node-b",
        )

        transport = HttpJsonRpcTransport(base_url="http://node-b/rpc", client=client)

        runtime = Runtime(log_a, Config())
        runtime.set_transport(transport)
        runtime.register_remote(exec_spec)
        runtime.register_agent(Reviewer(review_spec, log_a))

        order = ExecutionOrder(
            objective="Distributed minimal viable run",
            tasks=[
                Task(
                    id="t1",
                    goal="Implement a small code change",
                    difficulty="simple",
                    required_skills=["code"],
                    acceptance="Executor returns an artifact",
                ),
                Task(
                    id="t2",
                    goal="Run the related checks",
                    difficulty="simple",
                    required_skills=["code"],
                    acceptance="Checks are reported",
                ),
            ],
        )

        result = await runtime.run(order)
        events = await log_a.replay()
        state = derive_state(events, run_id=result["run_id"])

        print("Distributed demo")
        print(f"DB: {db_path}")
        print(f"Run: {result['run_id']}")
        print(f"Tasks total: {result['tasks_total']}")
        print(f"Done: {state.tasks_done}")
        print(f"Failed: {state.tasks_failed}")
        print("Shared event stream:")
        for event in events:
            payload = event.payload
            task_id = payload.get("task_id", "")
            print(f"  {event.seq:>3} {event.type:<18} actor={event.actor:<12} task={task_id}")
    finally:
        if transport is not None:
            await transport.aclose()
        log_a.close()
        log_b.close()
        try:
            os.unlink(db_path)
        except OSError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
