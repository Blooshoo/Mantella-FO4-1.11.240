"""Transports for the OpenClaw companion adapter.

* ``MockTransport``: offline default and test double. No network.
* ``GatewayChatCompletionsTransport``: the Gateway's documented OpenAI-compatible
  ``POST /v1/chat/completions`` surface (docs/gateway/openai-http-api.md):
  ``model = "openclaw/<agentId>"`` selects the agent, ``x-openclaw-session-key``
  gives explicit application-owned session routing, ``stream: true`` returns SSE
  ending in ``data: [DONE]``, a streamed failure arrives as an ``error`` object, and
  closing the HTTP connection cancels the agent run on the Gateway.

The adapter never sends ``tools``/``tool_choice`` and never sends ``x-openclaw-model``
(that is an owner-level override).

Retry safety: a failure is retryable when the turn demonstrably never reached the
Gateway (connection refused / connect or pool timeout) or the Gateway answered "busy"
(429/503). Anything after the request may have been accepted (read errors, protocol
errors, missing content, first-token timeout, a streamed error frame) is *ambiguous*:
the Gateway may already have appended the player's turn to the stateful session, so
replaying it would duplicate the turn. Those errors carry ``ambiguous=True`` and are
never retried; the client then moves to a fresh session epoch (see client.py).

Busy statuses (Sol review B05): whether the Gateway can persist a turn and *then* answer
429/503 is not part of any verified contract. They therefore carry
``acceptance_unverified=True``: retryable, but the client only retries them into a session
that cannot already hold the turn (a fresh epoch), unless the owner has verified the
contract and set ``busy_status_is_pre_acceptance``.

Log safety: streamed error frames are peer-controlled text that can echo prompt data or
credentials, so only a validated stable error code/type is ever kept, never the message.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol

from src.llm.openclaw.config import OpenClawCompanionConfig


@dataclass(frozen=True)
class TurnRequest:
    session_key: str
    model: str
    user_content: str
    max_tokens: int
    message_channel: str


class OpenClawTransportError(Exception):
    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None,
                 retry_after: float | None = None, after_first_token: bool = False,
                 ambiguous: bool = False, acceptance_unverified: bool = False):
        super().__init__(message)
        self.retryable = retryable and not ambiguous
        self.status = status
        self.retry_after = retry_after
        self.after_first_token = after_first_token
        # True when the Gateway may already have accepted/persisted the turn.
        self.ambiguous = ambiguous
        # True for an explicit "busy" answer whose pre-acceptance is assumed, not verified (B05).
        self.acceptance_unverified = acceptance_unverified


class Transport(Protocol):
    def stream(self, request: TurnRequest) -> AsyncIterator[str]: ...
    async def aclose(self) -> None: ...


def build_body(req: TurnRequest) -> dict:
    body = {
        "model": req.model,
        "stream": True,
        "messages": [{"role": "user", "content": req.user_content}],
    }
    if req.max_tokens:
        body["max_tokens"] = int(req.max_tokens)
    return body


def build_headers(req: TurnRequest, token: str | None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "x-openclaw-session-key": req.session_key,
        "x-openclaw-message-channel": req.message_channel,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


# Known remote error categories mapped to LOCAL constant labels (Sol re-review N04: a peer can put a
# credential into a regex-valid code/type, so peer labels are never echoed, only matched).
_KNOWN_ERROR_CATEGORIES = {
    "server_error": "server error", "api_error": "server error", "internal_error": "server error",
    "invalid_request_error": "invalid request", "authentication_error": "authentication",
    "permission_error": "permission", "not_found_error": "not found", "rate_limit_error": "rate limited",
    "overloaded_error": "overloaded", "timeout": "timeout", "cancelled": "cancelled", "canceled": "cancelled",
    "context_length_exceeded": "context too long", "content_filter": "content filtered",
}


def _safe_error_category(err: object) -> str:
    """Stable, log-safe description of a streamed error object: an allowlisted local constant
    or 'unspecified'. Never includes any peer-supplied text."""
    if isinstance(err, dict):
        for field_name in ("code", "type"):
            v = err.get(field_name)
            if isinstance(v, str):
                label = _KNOWN_ERROR_CATEGORIES.get(v.strip().casefold())
                if label:
                    return label
    return "unspecified"


def parse_sse_line(line: str) -> tuple[str, str | None]:
    """Return (kind, payload): kind in {'skip','done','delta','error','protocol'}.

    'protocol' = a syntactically valid JSON frame with the wrong shape (the caller turns it
    into an OpenClawTransportError). Never raises for any input line."""
    line = line.strip()
    if not line or line.startswith(":") or not line.startswith("data:"):
        return "skip", None
    data = line[5:].strip()
    if data == "[DONE]":
        return "done", None
    try:
        obj = json.loads(data)
    except (json.JSONDecodeError, RecursionError):
        return "skip", None
    if not isinstance(obj, dict):
        return "protocol", "frame is not a JSON object"
    if obj.get("error"):
        return "error", _safe_error_category(obj["error"])
    choices = obj.get("choices")
    if choices is None or choices == []:
        return "skip", None                      # e.g. a usage-only final chunk
    if not isinstance(choices, list) or not isinstance(choices[0], dict):
        return "protocol", "choices has the wrong shape"
    delta = choices[0].get("delta")
    if delta is None:
        return "skip", None
    if not isinstance(delta, dict):
        return "protocol", "delta is not an object"
    # tool_calls deltas are deliberately ignored: no client tools are offered.
    content = delta.get("content")
    if content is None:
        return "skip", None
    if not isinstance(content, str):
        return "protocol", "delta.content is not a string"
    if content:
        return "delta", content
    return "skip", None


# ---------------------------------------------------------------------------
@dataclass
class MockTransport:
    """Scripted offline transport. Records requests without any credential value."""
    reply_chunks: list[str] = field(default_factory=lambda: ["I'm right here ", "with you."])
    fail_first: int = 0
    fail_status: int | None = 503
    fail_mid_stream_after: int | None = None
    chunk_delay_s: float = 0.0
    requests: list[TurnRequest] = field(default_factory=list)
    auth_present: list[bool] = field(default_factory=list)
    closed_streams: int = 0
    completed_streams: int = 0
    token_supplier: object = None

    async def stream(self, request: TurnRequest) -> AsyncIterator[str]:
        self.requests.append(request)
        token = self.token_supplier() if callable(self.token_supplier) else None
        self.auth_present.append(bool(token))
        if self.fail_first > 0:
            self.fail_first -= 1
            raise OpenClawTransportError("mock unavailable", retryable=True, status=self.fail_status,
                                         acceptance_unverified=self.fail_status in (429, 503))
        sent = 0
        try:
            for chunk in self.reply_chunks:
                if self.fail_mid_stream_after is not None and sent >= self.fail_mid_stream_after:
                    raise OpenClawTransportError("mock stream broke", after_first_token=True)
                if self.chunk_delay_s:
                    await asyncio.sleep(self.chunk_delay_s)
                sent += 1
                yield chunk
            self.completed_streams += 1
        finally:
            self.closed_streams += 1

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
class GatewayChatCompletionsTransport:
    """One httpx.AsyncClient per stream: Mantella runs each response under a fresh
    ``asyncio.run`` loop, and an httpx connection pool must not outlive its loop
    (upstream ClientBase likewise creates a new client per call)."""

    def __init__(self, cfg: OpenClawCompanionConfig, client_factory=None):
        import httpx  # dependency of the openai SDK Mantella already requires
        self._httpx = httpx
        self._cfg = cfg
        self._url = cfg.base_url.rstrip("/") + "/chat/completions"
        self._client_factory = client_factory or self._default_client

    def _default_client(self):
        httpx = self._httpx
        cfg = self._cfg
        return httpx.AsyncClient(
            timeout=httpx.Timeout(connect=cfg.connect_timeout_s, read=cfg.read_timeout_s,
                                  write=cfg.connect_timeout_s, pool=cfg.connect_timeout_s),
            follow_redirects=False,
            trust_env=False,  # never pick up proxy env vars for a loopback agent call
        )

    async def aclose(self) -> None:
        return None

    async def stream(self, request: TurnRequest) -> AsyncIterator[str]:
        httpx = self._httpx
        token = self._cfg.resolve_token()
        headers = build_headers(request, token)
        got_first = False
        # ONE absolute first-content deadline, started before connecting: it bounds connect,
        # response headers and every pre-content read together. Heartbeats/ignored frames
        # cannot extend it (each wait gets only the remaining budget, and none once it is spent).
        deadline = time.monotonic() + self._cfg.first_token_timeout_s

        def remaining() -> float:
            left = deadline - time.monotonic()
            if left <= 0:
                raise OpenClawTransportError("no first token before timeout", ambiguous=True)
            return left

        client = self._client_factory()
        cm = None
        try:
            cm = client.stream("POST", self._url, headers=headers, json=build_body(request))
            try:
                resp = await asyncio.wait_for(cm.__aenter__(), timeout=remaining())
            except asyncio.TimeoutError:
                cm = None  # wait_for cancelled __aenter__; client.aclose() below releases the connection
                raise OpenClawTransportError("no first token before timeout (no response headers)",
                                             ambiguous=True) from None
            if resp.status_code != 200:
                # Classify from status/headers only; the error body is never read (Sol re-review
                # N03: an unbounded/trickling body would bypass the deadline and grow memory).
                raise _status_error(resp.status_code, resp.headers.get("retry-after"))
            lines = resp.aiter_lines()
            while True:
                try:
                    if not got_first:
                        line = await asyncio.wait_for(lines.__anext__(), timeout=remaining())
                    else:
                        line = await lines.__anext__()
                except StopAsyncIteration:
                    if not got_first:
                        raise OpenClawTransportError("stream ended without content", ambiguous=True) from None
                    raise OpenClawTransportError("stream ended without [DONE]", after_first_token=True) from None
                except asyncio.TimeoutError:
                    raise OpenClawTransportError("no first token before timeout", ambiguous=True) from None
                kind, payload = parse_sse_line(line)
                if kind == "done":
                    if not got_first:
                        # [DONE] without any content: not a spoken reply (re-review N06).
                        raise OpenClawTransportError("stream finished without content", ambiguous=True)
                    return
                if kind == "error":
                    raise OpenClawTransportError(f"agent run failed ({payload})",
                                                 after_first_token=got_first, ambiguous=True)
                if kind == "protocol":
                    raise OpenClawTransportError(f"malformed stream frame: {payload}",
                                                 after_first_token=got_first, ambiguous=True)
                if kind == "delta":
                    got_first = True
                    yield payload
        except OpenClawTransportError:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
            # The request never reached the Gateway: safe to retry.
            raise OpenClawTransportError(f"gateway connection problem: {type(e).__name__}",
                                         retryable=not got_first, after_first_token=got_first) from None
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ReadTimeout, httpx.WriteError,
                httpx.WriteTimeout) as e:
            # The turn may already be accepted/persisted by the Gateway: never replay it.
            raise OpenClawTransportError(f"gateway connection problem: {type(e).__name__}",
                                         after_first_token=got_first, ambiguous=True) from None
        finally:
            if cm is not None:
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001 - closing a broken stream must not mask the real error
                    pass
            await client.aclose()


def _status_error(status: int, retry_after: str | None) -> OpenClawTransportError:
    ra = None
    if retry_after:
        try:
            ra = float(retry_after)
        except ValueError:
            ra = None
    if status in (401, 403):
        return OpenClawTransportError(
            f"gateway rejected credentials or scope (HTTP {status}); check the token env var / agent scope",
            status=status)
    if status == 404:
        return OpenClawTransportError("gateway endpoint or agent not found (HTTP 404); is chatCompletions "
                                      "enabled and the companion agent defined?", status=status)
    if status == 400:
        return OpenClawTransportError("gateway rejected the request (HTTP 400)", status=status)
    if status in (429, 503):
        # "Busy, try later" from the Gateway. That the turn was NOT persisted first is assumed,
        # not verified (B05), so the client never retries it into the same session by default.
        return OpenClawTransportError(f"gateway busy (HTTP {status})", retryable=True, status=status,
                                      retry_after=ra, acceptance_unverified=True)
    # 500/502/504 etc.: the run may have started before the failure -> ambiguous, not retried.
    return OpenClawTransportError(f"gateway error (HTTP {status})", status=status, ambiguous=status >= 500)
