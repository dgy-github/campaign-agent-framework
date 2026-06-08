"""A2A protocol models for agent-to-agent communication."""
from __future__ import annotations

import uuid
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from .core.models import AgentSpec, Task


class Part(BaseModel):
    kind: Literal["text", "data"]
    text: str | None = None
    data: dict | None = None
    untrusted: bool = False


class Message(BaseModel):
    message_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    role: Literal["agent", "system"] = "agent"
    from_agent: str
    to_agent: str
    run_id: str = ""
    task_id: str = ""
    correlation_id: str = ""
    kind: Literal["request", "response", "event", "query"] = "request"
    parts: list[Part] = Field(default_factory=list)
    error: str | None = None

    @classmethod
    def request(cls, from_agent: str, to_agent: str, task: Task, run_id: str) -> "Message":
        return cls(
            from_agent=from_agent,
            to_agent=to_agent,
            run_id=run_id,
            task_id=task.id,
            kind="request",
            parts=[Part(kind="data", data=task.model_dump())],
        )

    def reply(self, result: dict, error: str | None = None) -> "Message":
        return Message(
            from_agent=self.to_agent,
            to_agent=self.from_agent,
            run_id=self.run_id,
            task_id=self.task_id,
            correlation_id=self.message_id,
            kind="response",
            parts=[Part(kind="data", data=result)],
            error=error,
        )

    @property
    def result(self) -> dict:
        if not self.parts:
            return {}
        data = self.parts[0].data
        return data if isinstance(data, dict) else {}


class TaskState(str, Enum):
    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class AgentCard(BaseModel):
    id: str
    role: str
    skills: list[str] = Field(default_factory=list)
    model_tier: str
    endpoint: str | None = None
    transport: Literal["in_process", "http"] = "in_process"

    @classmethod
    def from_spec(cls, spec: AgentSpec) -> "AgentCard":
        return cls(
            id=spec.id,
            role=spec.role,
            skills=list(spec.skills),
            model_tier=spec.model_tier,
        )
