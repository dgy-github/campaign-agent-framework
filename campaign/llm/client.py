"""LLM 适配器（M1）。对应框架文档：6.1 角色×模型。

OpenAI 兼容协议（DeepSeek / Ollama / vLLM 通用）。按 model_tier 映射具体模型。
带超时与重试（重试耗尽 → 上抛供熔断/减员检测捕获）。

支持两种模式：
- 真实模式：通过 httpx 调用 OpenAI 兼容 /chat/completions
- Mock 模式：返回预设响应，用于无外部 API 的测试
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..core.models import ModelTier

logger = logging.getLogger(__name__)


@dataclass
class TierConfig:
    """档位 → 具体模型/endpoint 映射。在 app/config.py 装配。"""

    model: str
    base_url: str
    api_key: str | None = None
    local: bool = False  # 本地推理（隐私敏感任务只走 local=True）


class LLMError(Exception):
    """LLM 调用失败。携带重试历史供上层（熔断/减员）决策。"""

    def __init__(self, message: str, failures: list[str] | None = None) -> None:
        super().__init__(message)
        self.failures = failures or []


class LLMClient:
    """OpenAI 兼容 LLM 客户端。

    Args:
        tiers: 档位 → TierConfig 映射
        timeout: 单次请求超时（秒）
        retries: 最大重试次数
        mock_responses: 用于测试的预设响应 map: tier → dict 列表（FIFO）
    """

    def __init__(
        self,
        tiers: dict[ModelTier, TierConfig],
        timeout: float = 60.0,
        retries: int = 2,
        mock_responses: dict[str, list[dict]] | None = None,
        mock_embeddings: dict[str, list[list[float]]] | None = None,
        gate=None,                 # 深层#3：统一执行闸(PolicyGate)，每次调用前过治理
        gate_ctx: dict | None = None,  # 传给 gate 的上下文(budget/privacy 等)
        tracer=None,
    ) -> None:
        self.tiers = tiers
        self.timeout = timeout
        self.retries = retries
        self._mock: dict[str, list[dict]] = mock_responses or {}
        self._mock_embeddings: dict[str, list[list[float]]] = mock_embeddings or {}
        self._gate = gate
        self._gate_ctx = gate_ctx or {}
        self._tracer = tracer
        # 真实 HTTP 客户端（lazy init）
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                limits=httpx.Limits(max_keepalive_connections=10),
            )
        return self._client

    # ── 公共接口 ─────────────────────────────────────────

    async def complete(
        self, tier: ModelTier, messages: list[dict], **kw: Any
    ) -> dict:
        """聊天补全。返回 OpenAI 兼容响应 JSON（dict）。"""
        return await self._call_api(tier, messages, tools=None, **kw)

    async def tool_call(
        self, tier: ModelTier, messages: list[dict], tools: list[dict], **kw: Any
    ) -> dict:
        """function-calling。强制 tool_choice='auto'。"""
        return await self._call_api(tier, messages, tools=tools, **kw)

    async def embed(
        self,
        inputs: str | list[str],
        tier: ModelTier,
        **kw: Any,
    ) -> list[list[float]]:
        """POST {base_url}/v1/embeddings，返回 embedding 向量列表。

        Args:
            inputs: 单条文本或文本列表
            tier: 模型档位
            **kw: 透传给 API 的额外参数（如 encoding_format）

        Returns:
            list[list[float]]: 每条输入对应的 embedding 向量；空输入返回空列表。
        """
        # 空输入保护
        if isinstance(inputs, str):
            inputs_list = [inputs]
        else:
            inputs_list = list(inputs)
        if not inputs_list:
            return []

        # Mock 模式：该 tier 一旦注册为 mock，就只走 mock，绝不 fallthrough。
        if tier in self._mock_embeddings:
            if not self._mock_embeddings[tier]:
                raise LLMError(f"mock embeddings exhausted for tier: {tier}")
            return self._mock_embeddings[tier].pop(0)

        cfg = self.tiers.get(tier)
        if cfg is None:
            raise LLMError(f"未配置 tier: {tier}")

        client = await self._get_client()

        body: dict[str, Any] = {
            "model": cfg.model,
            "input": inputs_list,
            **kw,
        }

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"

        try:
            resp = await client.post(
                f"{cfg.base_url.rstrip('/')}/v1/embeddings",
                json=body,
                headers=headers,
            )
            resp.raise_for_status()
            response = resp.json()
            return extract_embeddings(response)
        except httpx.HTTPStatusError as e:
            raise LLMError(f"embeddings API error (tier={tier}): HTTP {e.response.status_code}") from e
        except Exception as e:
            raise LLMError(f"embeddings API error (tier={tier}): {e}") from e

    # ── 核心调用逻辑 ─────────────────────────────────────

    async def _call_api(
        self,
        tier: ModelTier,
        messages: list[dict],
        tools: list[dict] | None,
        **kw: Any,
    ) -> dict:
        cfg = self.tiers.get(tier)
        if cfg is None:
            raise LLMError(f"未配置 tier: {tier}")

        est_cost = float(kw.pop("est_cost", 1.0))
        sensitive = bool(kw.pop("sensitive", False))

        # 敏感数据出域仍在调用前拦截；token spend 在响应后优先使用真实 usage。
        if self._gate is not None:
            from ..governance.policy import Action

            if sensitive and not cfg.local:
                ctx = dict(self._gate_ctx)
                ctx.setdefault("actor_role", "executor")
                ctx["is_local"] = bool(cfg.local)
                action = Action(actor=f"llm:{tier}", kind="data_egress", payload={}, cost=0.0, sensitive=True)
                if not await self._gate.check(action, ctx):
                    raise LLMError(f"LLM call blocked by governor (tier={tier}, kind=data_egress)")

        # Mock 模式：该 tier 一旦注册为 mock，就只走 mock，绝不 fallthrough 去打真实 API。
        # 边界：mock 列表耗尽 → 显式报错，而非偷偷发真实 HTTP 请求。
        if tier in self._mock:
            if not self._mock[tier]:
                raise LLMError(f"mock responses exhausted for tier: {tier}")
            response = self._mock[tier].pop(0)
            await self._record_usage(tier, response, est_cost)
            return response

        # 真实 API 调用（带重试）
        failures: list[str] = []
        client = await self._get_client()

        body: dict[str, Any] = {
            "model": cfg.model,
            "messages": messages,
            "temperature": kw.pop("temperature", 0.0),
            **kw,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"

        for attempt in range(self.retries + 1):
            try:
                resp = await client.post(
                    f"{cfg.base_url.rstrip('/')}/v1/chat/completions",
                    json=body,
                    headers=headers,
                )
                resp.raise_for_status()
                response = resp.json()
                await self._record_usage(tier, response, est_cost)
                return response
            except httpx.TimeoutException as e:
                failures.append(f"timeout (attempt {attempt + 1}): {e}")
                if attempt < self.retries:
                    await asyncio.sleep(0.5 * (2**attempt))  # 指数退避
            except httpx.HTTPStatusError as e:
                failures.append(f"HTTP {e.response.status_code} (attempt {attempt + 1}): {e}")
                # 4xx 不重试（除非 429）
                if e.response.status_code == 429 and attempt < self.retries:
                    retry_after = int(e.response.headers.get("Retry-After", "2"))
                    await asyncio.sleep(retry_after)
                    continue
                if 400 <= e.response.status_code < 500 and e.response.status_code != 429:
                    break  # 客户端错误不重试
                if attempt < self.retries:
                    await asyncio.sleep(0.5 * (2**attempt))
            except Exception as e:
                failures.append(f"error (attempt {attempt + 1}): {e}")
                if attempt < self.retries:
                    await asyncio.sleep(0.5 * (2**attempt))

        raise LLMError(f"LLM 调用失败 (tier={tier})", failures=failures)

    async def _record_usage(self, tier: ModelTier, response: dict, est_cost: float) -> None:
        usage = extract_usage(response)
        tokens = usage.get("total_tokens")
        cost = float(tokens if tokens is not None else est_cost)

        if self._gate is not None:
            from ..governance.policy import Action

            ctx = dict(self._gate_ctx)
            ctx.setdefault("actor_role", "executor")
            action = Action(actor=f"llm:{tier}", kind="spend", payload={"usage": usage}, cost=cost)
            if not await self._gate.check(action, ctx):
                raise LLMError(f"LLM call blocked by governor (tier={tier}, kind=spend)")

        if self._tracer is not None and tokens is not None:
            self._tracer.meter(f"llm:{tier}", int(tokens), cost, 0.0)

    async def close(self) -> None:
        """关闭 HTTP 客户端。"""
        if self._client:
            await self._client.aclose()
            self._client = None


# ── 辅助提取函数 ─────────────────────────────────────────

def extract_text(response: dict) -> str:
    """从 OpenAI 兼容响应中提取纯文本内容。"""
    try:
        return response["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


def extract_usage(response: dict) -> dict:
    """Extract OpenAI-compatible token usage fields."""
    usage = response.get("usage", {}) if isinstance(response, dict) else {}
    if not isinstance(usage, dict):
        return {}
    return {
        key: usage[key]
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        if key in usage
    }


def extract_tool_calls(response: dict) -> list[dict]:
    """从 OpenAI 兼容响应中提取 tool_calls 列表。"""
    try:
        msg = response["choices"][0]["message"]
        return msg.get("tool_calls", [])
    except (KeyError, IndexError, TypeError):
        return []


def extract_embeddings(response: dict) -> list[list[float]]:
    """从 OpenAI 兼容 embeddings 响应中提取向量列表。

    API 返回格式: {"object": "list", "data": [{"embedding": [...], "index": 0}, ...]}
    """
    try:
        data = response["data"]
        return [item["embedding"] for item in data]
    except (KeyError, IndexError, TypeError):
        return []
