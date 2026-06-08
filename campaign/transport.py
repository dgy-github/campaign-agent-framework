"""Transport implementations for A2A message exchange."""
from __future__ import annotations

import abc
import asyncio
import json
import uuid
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, AsyncIterator

import httpx

from .core.events import EventLog
from .core.models import AgentSpec, Task
from .protocol import AgentCard, Message
from .roles.base import Agent

if TYPE_CHECKING:
    pass


class Transport(abc.ABC):
    @abc.abstractmethod
    async def send(self, msg: Message) -> Message:
        """Send a request message and return its response."""
        raise NotImplementedError

    async def discover(self) -> list[AgentCard]:
        """Discover agent cards from this transport. Default: empty list."""
        return []

    async def send_stream(self, msg: Message) -> AsyncIterator[dict[str, Any]]:
        """Send a streaming request (message/stream). Default: not supported."""
        yield {"event": "error", "data": {"error": "streaming not supported by this transport"}}


class RemoteAgentProxy(Agent):
    """Runtime-side selector for an agent hosted behind a transport."""

    def __init__(self, spec_or_card: AgentSpec | AgentCard, log: EventLog) -> None:
        if isinstance(spec_or_card, AgentCard):
            spec = AgentSpec(
                id=spec_or_card.id,
                role=spec_or_card.role,
                model_tier=spec_or_card.model_tier,
                skills=list(spec_or_card.skills),
            )
        else:
            spec = spec_or_card
        super().__init__(spec, log)

    async def handle(self, task: Task) -> dict:
        raise RuntimeError("RemoteAgentProxy.handle should not run locally; routed via transport")


class InProcessTransport(Transport):
    def __init__(
        self,
        agents: dict[str, "Agent"] | None = None,
        log: EventLog | None = None,
        known_senders: set[str] | None = None,
        gate=None,
    ) -> None:
        self._agents: dict[str, "Agent"] = dict(agents or {})
        self._log = log
        self._known_senders = set(known_senders) if known_senders is not None else None
        self._gate = gate

    def register(self, agent: "Agent") -> None:
        self._agents[agent.spec.id] = agent
        if self._known_senders is not None:
            self._known_senders.add(agent.spec.id)

    def cards(self) -> list[AgentCard]:
        return [AgentCard.from_spec(agent.spec) for agent in self._agents.values()]

    async def send(self, msg: Message) -> Message:
        if self._known_senders is not None and msg.from_agent not in self._known_senders:
            return await self._reject(msg, f"untrusted sender: {msg.from_agent}")

        if self._gate is not None:
            reason = await self._scan_untrusted(msg)
            if reason:
                return await self._reject(msg, reason)

        await self._audit(msg)
        agent = self._agents.get(msg.to_agent)
        if agent is None:
            resp = msg.reply({}, error=f"target agent not found: {msg.to_agent}")
            await self._audit(resp)
            return resp

        resp = await agent.on_message(msg)
        await self._audit(resp)
        return resp

    async def _reject(self, msg: Message, reason: str) -> Message:
        resp = msg.reply({}, error=reason)
        if self._log is not None:
            await self._log.append(
                "a2a.rejected",
                "transport",
                {
                    "run_id": msg.run_id,
                    "task_id": msg.task_id,
                    "from": msg.from_agent,
                    "to": msg.to_agent,
                    "kind": msg.kind,
                    "message_id": msg.message_id,
                    "reason": reason,
                },
            )
        return resp

    async def _scan_untrusted(self, msg: Message) -> str | None:
        from .governance.policy import Action

        for part in msg.parts:
            if not part.untrusted:
                continue
            text = part.text or ""
            if part.data is not None:
                text = f"{text}\n{part.data}"
            action = Action(actor=msg.from_agent, kind="ingest", payload={"text": text})
            if not await self._gate.check(action, {"run_id": msg.run_id}):
                return "untrusted content blocked by policy"
        return None

    async def _audit(self, msg: Message) -> None:
        if self._log is None:
            return
        await self._log.append(
            "a2a.message",
            msg.from_agent,
            {
                "run_id": msg.run_id,
                "task_id": msg.task_id,
                "from": msg.from_agent,
                "to": msg.to_agent,
                "kind": msg.kind,
                "message_id": msg.message_id,
                "correlation_id": msg.correlation_id,
                "error": msg.error,
            },
        )


class HttpJsonRpcTransport(Transport):
    """HTTP JSON-RPC 2.0 transport with optional retry + auth.

    Args:
        base_url: Agent server endpoint URL.
        client: Optional pre-configured httpx.AsyncClient (caller manages lifecycle).
        retries: Maximum retry attempts on transient errors (default 0 = no retry).
        backoff: Base backoff in seconds (exponential: backoff * 2**attempt).
        auth_token: Optional Bearer token sent as Authorization header.
    """

    def __init__(
        self,
        base_url: str = "",
        client: httpx.AsyncClient | None = None,
        retries: int = 0,
        backoff: float = 0.5,
        auth_token: str | None = None,
    ) -> None:
        self.base_url = base_url
        self._client = client
        self._owns_client = client is None
        self._retries = retries
        self._backoff = backoff
        self._auth_token = auth_token

    # ── Core: _post_rpc (DRY backbone for send + discover) ──────────────

    async def _post_rpc(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Post a JSON-RPC request with auth headers and retry/backoff.

        Returns the parsed JSON response dict on success, or an error
        sentinel dict ``{"_transport_error": "<message>"}`` on failure.
        Never raises.

        Transient errors eligible for retry:
        - httpx.TransportError (connection failures)
        - httpx.TimeoutException
        - HTTP 502 / 503 / 504 / 429

        Non-retryable (returned immediately as error sentinel):
        - HTTP 4xx (except 429)
        - JSON parse errors
        """
        headers: dict[str, str] = {}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"

        failures: list[str] = []

        for attempt in range(self._retries + 1):
            try:
                client = self._client
                if client is None:
                    client = httpx.AsyncClient()
                    self._client = client

                resp = await client.post(self.base_url, json=payload, headers=headers)

                # Transient HTTP errors — retry with exponential backoff
                if resp.status_code in (502, 503, 504, 429):
                    failures.append(f"HTTP {resp.status_code} (attempt {attempt + 1})")
                    if attempt < self._retries:
                        await asyncio.sleep(self._backoff * (2 ** attempt))
                        continue
                    break  # exhausted retries

                resp.raise_for_status()
                return resp.json()

            except httpx.TimeoutException as e:
                failures.append(f"timeout (attempt {attempt + 1}): {e}")
                if attempt < self._retries:
                    await asyncio.sleep(self._backoff * (2 ** attempt))
            except httpx.TransportError as e:
                failures.append(f"transport error (attempt {attempt + 1}): {e}")
                if attempt < self._retries:
                    await asyncio.sleep(self._backoff * (2 ** attempt))
            except httpx.HTTPStatusError as e:
                # 4xx (non-429, handled above) — do not retry
                return {"_transport_error": f"http transport error: HTTP {e.response.status_code}"}
            except Exception as exc:
                # Parse / validation errors — do not retry
                return {"_transport_error": f"http transport error: {exc}"}

        return {
            "_transport_error": f"http transport error after {self._retries + 1} attempts: {'; '.join(failures)}"
        }

    # ── Public API: send / send_stream / discover ───────────────────────

    async def send(self, msg: Message) -> Message:
        """Send a JSON-RPC message/send request.

        Delegates HTTP+retry+auth to _post_rpc, then parses the JSON-RPC
        response into a Message (or error Message on failure).  Outward
        behaviour is identical to the pre-DRY implementation.
        """
        request = {
            "jsonrpc": "2.0",
            "id": msg.message_id,
            "method": "message/send",
            "params": msg.model_dump(mode="json"),
        }

        payload = await self._post_rpc(request)

        if "_transport_error" in payload:
            return msg.reply({}, error=payload["_transport_error"])
        if "error" in payload:
            return msg.reply({}, error=str(payload["error"]))
        if "result" not in payload:
            return msg.reply({}, error="http transport error: missing JSON-RPC result")
        return Message.model_validate(payload["result"])

    async def send_stream(self, msg: Message) -> AsyncIterator[dict[str, Any]]:
        """Send a message/stream request and yield A2A SSE event dicts.

        Uses httpx streaming (client.stream POST) so the caller can
        consume events as they arrive.  Reuses the auth header but
        intentionally *does not retry* — streaming requests are not
        idempotent.  On any network/HTTP error yields a single error
        event and stops (never raises).
        """
        headers: dict[str, str] = {}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"

        params = msg.model_dump(mode="json")

        try:
            client = self._client
            if client is None:
                client = httpx.AsyncClient()
                self._client = client

            async with client.stream(
                "POST",
                self.base_url,
                json={
                    "jsonrpc": "2.0",
                    "id": msg.message_id,
                    "method": "message/stream",
                    "params": params,
                },
                headers=headers,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:]  # strip "data: " prefix
                        try:
                            event = json.loads(data_str)
                            yield event
                        except json.JSONDecodeError:
                            continue
        except Exception as exc:
            yield {"event": "error", "data": {"error": str(exc)}}

    async def discover(self) -> list[AgentCard]:
        """Discover remote agent cards via JSON-RPC agent/cards method.

        Shares auth + retry/backoff with send via _post_rpc.
        On any error (network, JSON-RPC error, missing data) returns [].
        """
        request = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": "agent/cards",
            "params": {},
        }

        payload = await self._post_rpc(request)

        if "_transport_error" in payload:
            return []
        if "error" in payload:
            return []

        result = payload.get("result", {})
        cards_raw = result.get("cards") if isinstance(result, dict) else None
        if not isinstance(cards_raw, list):
            return []

        try:
            return [AgentCard.model_validate(c) for c in cards_raw]
        except Exception:
            return []

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


class JsonRpcAgentServer:
    """Minimal JSON-RPC 2.0 endpoint for A2A message/send and message/stream.

    Args:
        agents: Agent registry (id -> Agent).
        known_senders: Optional trusted sender allowlist.
        gate: Optional PolicyGate for content scanning.
        log: Optional EventLog for audit trail.
        auth_token: Optional Bearer token; when set, requests must carry
            a matching Authorization header or receive -32001.
        idempotency_cache_size: Max cached message_id -> response entries
            for deduplication (FIFO eviction, default 256).  Only applies
            to message/send; streaming requests skip the cache.
    """

    def __init__(
        self,
        agents: dict[str, "Agent"],
        known_senders: set[str] | None = None,
        gate=None,
        log: EventLog | None = None,
        auth_token: str | None = None,
        idempotency_cache_size: int = 256,
    ) -> None:
        self._transport = InProcessTransport(
            agents,
            log,
            known_senders=known_senders,
            gate=gate,
        )
        self._auth_token = auth_token
        self._idempotency_cache_size = idempotency_cache_size
        self._idempotency_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()

    # ── Auth helper ─────────────────────────────────────────────────────

    def _check_auth(self, headers: dict[str, str] | None) -> bool:
        """Return True if auth passes (or not configured)."""
        if self._auth_token is None:
            return True
        auth_header: str | None = None
        for key, value in (headers or {}).items():
            if key.lower() == "authorization":
                auth_header = value
                break
        expected = f"Bearer {self._auth_token}"
        return auth_header == expected

    # ── RPC handlers ────────────────────────────────────────────────────

    async def handle_rpc(
        self, request: dict[str, Any], headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Process a JSON-RPC 2.0 request (message/send or agent/cards).

        Args:
            request: Parsed JSON-RPC request dict.
            headers: Optional HTTP request headers (used for auth when
                auth_token is configured).

        Returns:
            JSON-RPC response dict.
        """
        # Auth check when auth_token is configured.
        if not self._check_auth(headers):
            return self._rpc_error(request.get("id"), -32001, "unauthorized")

        request_id = request.get("id")
        if request.get("jsonrpc") != "2.0":
            return self._rpc_error(request_id, -32600, "invalid JSON-RPC request")

        method = request.get("method")

        if method == "agent/cards":
            cards = [card.model_dump(mode="json") for card in self._transport.cards()]
            return {"jsonrpc": "2.0", "id": request_id, "result": {"cards": cards}}

        if method != "message/send":
            return self._rpc_error(request_id, -32601, f"method not found: {method}")
        if "params" not in request:
            return self._rpc_error(request_id, -32602, "missing params")

        try:
            msg = Message.model_validate(request["params"])
        except Exception as exc:
            return self._rpc_error(request_id, -32602, f"invalid message params: {exc}")

        # ── Idempotency: dedup by message_id ──
        message_id = msg.message_id
        if message_id in self._idempotency_cache:
            return self._idempotency_cache[message_id]

        resp = await self._transport.send(msg)
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": resp.model_dump(mode="json"),
        }

        # Cache the response; FIFO evict when at capacity
        if len(self._idempotency_cache) >= self._idempotency_cache_size:
            self._idempotency_cache.popitem(last=False)
        self._idempotency_cache[message_id] = response

        return response

    async def handle_stream(
        self, request: dict[str, Any], headers: dict[str, str] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Handle A2A message/stream request, yielding SSE event dicts.

        Events yielded (in order):
          - {"event": "error", "data": {"error": "..."}} on auth / parse /
            routing failure (stops immediately after).
          - {"event": "status", "data": {"task_id": ..., "state": "working"}}
          - {"event": "result", "data": <Message.model_dump(mode="json")>}
            on success.
        """
        # (1) Auth check
        if not self._check_auth(headers):
            yield {"event": "error", "data": {"error": "unauthorized"}}
            return

        # (2) Parse params into Message
        if "params" not in request:
            yield {"event": "error", "data": {"error": "missing params"}}
            return

        try:
            msg = Message.model_validate(request["params"])
        except Exception as exc:
            yield {"event": "error", "data": {"error": f"invalid message params: {exc}"}}
            return

        # (3) Route via InProcessTransport (same trust/gate/audit as message/send)
        yield {"event": "status", "data": {"task_id": msg.task_id, "state": "working"}}

        resp = await self._transport.send(msg)

        if resp.error:
            yield {"event": "error", "data": {"error": resp.error}}
        else:
            yield {"event": "result", "data": resp.model_dump(mode="json")}

    # ── ASGI app ────────────────────────────────────────────────────────

    def asgi_app(self):
        """Return an ASGI 3 callable serving the JSON-RPC endpoint.

        - ``message/send`` and ``agent/cards``: single JSON response
          (Content-Type: application/json).
        - ``message/stream``: SSE streaming response
          (Content-Type: text/event-stream).
        """

        async def app(scope, receive, send) -> None:
            if scope["type"] != "http":
                return

            body = b""
            while True:
                event = await receive()
                if event["type"] == "http.request":
                    body += event.get("body", b"")
                    if not event.get("more_body", False):
                        break

            # Extract HTTP headers for auth
            headers_dict: dict[str, str] = {}
            for key, value in scope.get("headers", []):
                headers_dict[key.decode("latin-1")] = value.decode("latin-1")

            # Parse body as JSON
            try:
                request = json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                response = self._rpc_error(None, -32700, "parse error")
                payload = json.dumps(response).encode("utf-8")
                await send({
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                })
                await send({"type": "http.response.body", "body": payload})
                return

            if not isinstance(request, dict):
                response = self._rpc_error(None, -32600, "invalid JSON-RPC request")
                payload = json.dumps(response).encode("utf-8")
                await send({
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                })
                await send({"type": "http.response.body", "body": payload})
                return

            method = request.get("method")

            # ── SSE streaming for message/stream ──
            if method == "message/stream":
                await send({
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")],
                })
                try:
                    async for sse_event in self.handle_stream(
                        request, headers=headers_dict
                    ):
                        line = f"data: {json.dumps(sse_event)}\n\n"
                        await send({
                            "type": "http.response.body",
                            "body": line.encode("utf-8"),
                            "more_body": True,
                        })
                except Exception:
                    pass  # client disconnected; stream ends
                await send({
                    "type": "http.response.body",
                    "body": b"",
                    "more_body": False,
                })
                return

            # ── Standard JSON-RPC (message/send, agent/cards, …) ──
            try:
                response = await self.handle_rpc(request, headers=headers_dict)
            except Exception as exc:
                response = self._rpc_error(None, -32603, f"internal error: {exc}")

            payload = json.dumps(response).encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({"type": "http.response.body", "body": payload})

        return app

    @staticmethod
    def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
