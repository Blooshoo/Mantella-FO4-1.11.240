"""Mantella AIClient-compatible client that voices an opted-in companion via OpenClaw.

It is a drop-in for the object ``ChatManager`` streams from (same duck-typed
interface as ``ClientBase``/``MockAIClient``: ``streaming_call`` yielding
``("content", str)`` / ``("tool_calls", list)`` tuples, ``get_count_tokens``,
``is_too_long``, ``request_call``). Everything not related to the companion's
spoken reply is delegated to the normal direct Mantella client (``fallback``):
token accounting, summaries (``request_call``), vision, and game-action
selection through Mantella's dedicated function-calling model.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncGenerator, Callable

from src.llm.openclaw.config import OpenClawCompanionConfig
from src.llm.openclaw.session import ConversationState, build_session_key
from src.llm.openclaw.transport import OpenClawTransportError, Transport, TurnRequest
from src.llm.openclaw import trust

logger = logging.getLogger("mantella.openclaw")


def _to_openai_messages(messages: Any) -> list[dict]:
    if hasattr(messages, "get_openai_messages"):
        return list(messages.get_openai_messages())
    if hasattr(messages, "get_openai_message"):
        return [messages.get_openai_message()]
    if isinstance(messages, list):
        return list(messages)
    raise TypeError("unsupported messages object")


class OpenClawCompanionClient:
    def __init__(self, cfg: OpenClawCompanionConfig, transport: Transport, fallback: Any,
                 character_name: str, character_ref: str | None, conversation_id: str,
                 sleep: Callable[[float], Any] = asyncio.sleep) -> None:
        self._cfg = cfg
        self._transport = transport
        self._fallback = fallback
        self._character = character_name
        self._sleep = sleep
        self.state = ConversationState(
            session_key=build_session_key(cfg, character_name, character_ref, conversation_id),
            conversation_id=conversation_id,
        )
        self._busy = False
        self._rotated_this_turn = False
        self._warned_actions = False
        self.last_route: str | None = None  # "openclaw" | "fallback" | "openclaw-partial" (for tests/logs)
        self.last_error: str | None = None

    # ---- delegated surface -------------------------------------------------
    @property
    def model_name(self) -> str:
        return self._cfg.model

    @property
    def is_local(self) -> bool:
        return True

    def get_count_tokens(self, messages) -> int:
        return self._fallback.get_count_tokens(messages)

    def is_too_long(self, messages, token_limit_percent: float) -> bool:
        return self._fallback.is_too_long(messages, token_limit_percent)

    def request_call(self, messages):
        # Summaries / one-off sync calls stay on the direct provider so long-term
        # Mantella memory generation does not flow through the companion session.
        return self._fallback.request_call(messages)

    def enable_vision_for_next_call(self) -> None:
        logger.info("OpenClaw companion route does not forward screenshots; vision request ignored for %s",
                    self._character)

    @property
    def _function_client(self):
        return getattr(self._fallback, "_function_client", None)

    @property
    def _image_client(self):
        return None

    # ---- main path ---------------------------------------------------------
    async def streaming_call(self, messages, is_multi_npc: bool, tools: list[dict] | None = None
                             ) -> AsyncGenerator[tuple[str, Any], None]:
        if is_multi_npc:
            # Group scenes keep Mantella's normal provider: one shared multi-speaker
            # prompt cannot be safely attributed to a single companion session.
            self.last_route = "fallback"
            async for item in self._fallback.streaming_call(messages=messages, is_multi_npc=True, tools=tools):
                yield item
            return
        if self._busy:
            raise RuntimeError("OpenClaw companion client is already streaming")
        self._busy = True
        inner = self._stream(messages, tools)
        try:
            async for item in inner:
                yield item
        finally:
            # Explicit close so a consumer `break` / aclose() promptly cancels the HTTP stream.
            await inner.aclose()
            self._busy = False

    async def _stream(self, messages, tools):
        self._rotated_this_turn = False
        tools_handled = False
        if tools:
            fc = self._function_client
            if fc is not None:
                pre = fc.check_for_actions(messages, tools)
                tools_handled = True
                if pre:
                    yield ("tool_calls", pre)
            elif not self._warned_actions:
                self._warned_actions = True
                logger.warning("Game actions for %s are unavailable on the OpenClaw route unless Mantella's "
                               "custom function-calling model is configured; speech continues without actions.",
                               self._character)

        openai_messages = _to_openai_messages(messages)
        scene, lines = trust.openai_messages_to_lines(openai_messages, self._character, self._cfg.max_message_chars)
        plan = self.state.plan(scene, lines, self._cfg.recap_messages, self._character)
        content = trust.build_turn(self._character, scene if plan.include_context else None,
                                   plan.lines, self._cfg.max_context_chars)
        request = TurnRequest(self.state.session_key, self._cfg.model, content, self._cfg.max_tokens,
                              self._cfg.message_channel)

        attempt = 0
        produced = 0
        completed = False
        while True:
            stream = self._transport.stream(request)
            # Until proven otherwise the Gateway may have accepted this turn (re-review N06).
            maybe_accepted = True
            try:
                async for chunk in stream:
                    cap = self._cfg.max_reply_chars
                    if cap:
                        remaining = cap - produced
                        if remaining <= 0:
                            logger.warning("OpenClaw reply for %s hit max_reply_chars; truncating", self._character)
                            break
                        chunk = trust.sanitise_reply(chunk, remaining)
                    else:
                        chunk = trust.sanitise_reply(chunk, 0)
                    if not chunk:
                        continue
                    produced += len(chunk)
                    yield ("content", chunk)
                else:
                    completed = True
                if produced and not completed:
                    self.last_route = "openclaw-partial"
                else:
                    self.last_route = "openclaw"
                break
            except OpenClawTransportError as e:
                self.last_error = self._redact(str(e))
                if not e.ambiguous and not produced and not e.after_first_token:
                    maybe_accepted = False  # pre-submission failure or explicit rejection
                if produced or e.after_first_token:
                    # Speech already reached TTS: never retry or fall back (would duplicate lines).
                    logger.error("OpenClaw stream for %s broke mid-reply: %s", self._character, self.last_error)
                    self.last_route = "openclaw-partial"
                    break
                if e.retryable and attempt < self._cfg.max_retries:
                    attempt += 1
                    delay = self._cfg.retry_backoff_s * (2 ** (attempt - 1))
                    if e.retry_after is not None:
                        delay = max(delay, min(e.retry_after, self._cfg.max_retry_after_s))
                    logger.warning("OpenClaw unavailable for %s (%s); retry %d/%d in %.2fs",
                                   self._character, self.last_error, attempt, self._cfg.max_retries, delay)
                    await self._sleep(delay)
                    continue
                logger.error("OpenClaw route failed for %s: %s", self._character, self.last_error)
                self.state.invalidate()
                if self._cfg.fallback_to_direct:
                    logger.warning("Falling back to the direct Mantella provider for this turn")
                    self.last_route = "fallback"
                    async for item in self._fallback.streaming_call(
                            messages=messages, is_multi_npc=False, tools=None if tools_handled else tools):
                        yield item
                else:
                    self.last_route = "failed"
                return
            finally:
                # Runs on normal end, error, and consumer cancellation (aclose/GeneratorExit):
                # closing the inner stream closes the HTTP response, which cancels the Gateway run.
                await stream.aclose()
                if not completed:
                    self.state.invalidate()
                    # Any termination that may follow acceptance - partial EOF, reply-cap truncation,
                    # consumer stop/cancel, ambiguous errors - moves to a fresh session epoch, so the
                    # next recap never lands in a transcript that may already hold this turn.
                    if maybe_accepted and not self._rotated_this_turn:
                        self._after_unconfirmed()
        if completed:
            self.state.commit(plan, scene)

    def _redact(self, text: str) -> str:
        """Defence in depth: never keep the configured credential in diagnostics."""
        try:
            token = self._cfg.resolve_token()
        except Exception:  # noqa: BLE001
            token = None
        if token and len(token) >= 4:
            text = text.replace(token, "[redacted]")
        return " ".join(text.split())[:300]

    def _after_unconfirmed(self) -> None:
        """After a turn the Gateway may have accepted but that did not complete, never continue the
        old transcript blindly. Conversation scope moves to a fresh session epoch (bounded recap);
        character scope is an owner-chosen durable transcript, so it keeps its key and resyncs
        (residual risk: the recap can repeat the failed turn there; documented in README)."""
        self._rotated_this_turn = True
        if self._cfg.session_scope == "conversation":
            key = self.state.rotate_epoch()
            logger.warning("OpenClaw turn for %s may have been accepted before failing; next turn uses a "
                           "fresh session epoch (%s)", self._character, key.rsplit(":", 1)[-1])
