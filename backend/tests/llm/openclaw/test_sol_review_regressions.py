"""Regressions for the Sol adversarial review findings B01-B05 (2026-11-03).

No network: the Gateway transport runs against in-process httpx.MockTransport handlers.
B02 drives the real ChatManager.stop_generation from a second thread.
"""
import asyncio
import json
import logging
import os
import threading
import time
import unittest
from unittest import mock

import pytest

from src.llm.openclaw.client import OpenClawCompanionClient
from src.llm.openclaw.transport import (GatewayChatCompletionsTransport, OpenClawTransportError,
                                        parse_sse_line)
from tests.llm.openclaw.test_openclaw_adapter import (THREAD, FakeFallback, _chunk, _sse, cfg, collect,
                                                      no_sleep, run)

SENTINEL = "tok-sentinel-8c1f"


def _gateway(handler, **kw):
    import httpx
    c = cfg(transport="gateway", agent_id="fallout-kalli", token_env="MANTELLA_TEST_TOKEN", **kw)
    tr = GatewayChatCompletionsTransport(c, client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return c, tr


import httpx  # noqa: E402  (dependency of the openai SDK Mantella already requires)


class _AsyncBody(httpx.AsyncByteStream):
    """httpx async byte stream from an async generator function."""

    def __init__(self, gen):
        self._gen = gen

    async def __aiter__(self):
        async for b in self._gen():
            yield b

    async def aclose(self):
        return None


# --------------------------------------------------------------------------- B01
class FirstTokenDeadlineTests(unittest.TestCase):
    def test_heartbeats_cannot_extend_first_token_deadline(self):
        import httpx

        async def body():
            end = time.monotonic() + 0.4
            while time.monotonic() < end:
                yield b": ping\n\n"
                await asyncio.sleep(0.001)
            yield _sse(_chunk("late"), "[DONE]")

        def handler(request):
            return httpx.Response(200, stream=_AsyncBody(body))
        c, tr = _gateway(handler, first_token_timeout_s=0.05, fallback_to_direct=False)
        client = OpenClawCompanionClient(c, tr, FakeFallback(), "Kalli", "4C50", "conv1", sleep=no_sleep)
        t0 = time.monotonic()
        items = run(collect(client.streaming_call(THREAD, False)))
        elapsed = time.monotonic() - t0
        self.assertEqual(items, [])                       # the late delta must not be accepted
        self.assertLess(elapsed, 0.3)
        self.assertIn("first token", client.last_error)

    def test_deadline_covers_response_headers(self):
        import httpx

        async def handler(request):
            await asyncio.sleep(0.5)
            return httpx.Response(200, content=_sse(_chunk("slow"), "[DONE]"))
        c, tr = _gateway(handler, first_token_timeout_s=0.05, fallback_to_direct=False)
        client = OpenClawCompanionClient(c, tr, FakeFallback(), "Kalli", "4C50", "conv1", sleep=no_sleep)
        t0 = time.monotonic()
        self.assertEqual(run(collect(client.streaming_call(THREAD, False))), [])
        self.assertLess(time.monotonic() - t0, 0.35)


# --------------------------------------------------------------------------- B03
class MalformedSseTests(unittest.TestCase):
    def test_parser_never_raises_on_wrong_shapes(self):
        for frame in ('{"choices":[{"delta":[1]}]}', '{"choices":[{"delta":"oops"}]}',
                      '{"choices":"x"}', '{"choices":[1]}', '{"choices":[{"delta":{"content":5}}]}',
                      '[1,2]', '"str"', '{"error":[1]}'):
            kind, _ = parse_sse_line("data: " + frame)        # must not raise AttributeError etc.
            self.assertIn(kind, ("skip", "protocol", "error"))
        self.assertEqual(parse_sse_line('data: {"choices":[{"delta":[1]}]}')[0], "protocol")
        self.assertEqual(parse_sse_line('data: {"choices":[]}'), ("skip", None))      # usage-only frame
        self.assertEqual(parse_sse_line('data: {"choices":[{"delta":null}]}'), ("skip", None))

    def _run_with(self, frames, **kw):
        import httpx

        def handler(request):
            return httpx.Response(200, content=_sse(*frames))
        c, tr = _gateway(handler, **kw)
        fb = FakeFallback(chunks=["direct."])
        client = OpenClawCompanionClient(c, tr, fb, "Kalli", "4C50", "conv1", sleep=no_sleep)
        return client, fb, run(collect(client.streaming_call(THREAD, False)))

    def test_malformed_before_content_uses_fallback_policy(self):
        client, fb, items = self._run_with(['{"choices":[{"delta":[1]}]}', "[DONE]"])
        self.assertEqual(items, [("content", "direct.")])
        self.assertEqual(client.last_route, "fallback")
        self.assertEqual(len(fb.calls), 1)

    def test_malformed_after_content_is_partial(self):
        client, fb, items = self._run_with([_chunk("Hel"), '{"choices":[{"delta":"oops"}]}', "[DONE]"])
        self.assertEqual(items, [("content", "Hel")])
        self.assertEqual(client.last_route, "openclaw-partial")
        self.assertEqual(fb.calls, [])


# --------------------------------------------------------------------------- B04
class SseErrorRedactionTests(unittest.TestCase):
    def test_peer_error_prose_never_reaches_logs_or_last_error(self):
        import httpx
        msg = "failed for " + SENTINEL + "\nFORGED LOG line"

        def handler(request):
            return httpx.Response(200, content=_sse(json.dumps({"error": {"message": msg, "type": "agent_error",
                                                                           "code": "run_failed"}}), "[DONE]"))
        c, tr = _gateway(handler)
        client = OpenClawCompanionClient(c, tr, FakeFallback(), "Kalli", "4C50", "conv1", sleep=no_sleep)
        with mock.patch.dict(os.environ, {"MANTELLA_TEST_TOKEN": SENTINEL}), \
                self.assertLogs("mantella.openclaw", level=logging.DEBUG) as logs:
            run(collect(client.streaming_call(THREAD, False)))
        text = "\n".join(logs.output) + "\n" + (client.last_error or "")
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn("FORGED LOG", text)
        self.assertIn("unspecified", client.last_error)            # unknown peer code -> local constant (N04)
        self.assertNotIn("\n", client.last_error)

    def test_hostile_error_code_is_not_trusted_either(self):
        kind, payload = parse_sse_line("data: " + json.dumps({"error": {"code": SENTINEL + "\nX", "message": "m"}}))
        self.assertEqual(kind, "error")
        self.assertNotIn(SENTINEL, payload)


# --------------------------------------------------------------------------- B05
class AmbiguousRetryTests(unittest.TestCase):
    def _stateful(self, fail):
        """Fake Gateway: records every accepted turn per session key, then fails the first one."""
        import httpx
        turns = {}

        def handler(request):
            key = request.headers["x-openclaw-session-key"]
            turns.setdefault(key, []).append(json.loads(request.content)["messages"][0]["content"])
            if len(sum(turns.values(), [])) == 1:
                return fail()
            return httpx.Response(200, content=_sse(_chunk("ok"), "[DONE]"))
        return turns, handler

    def test_read_error_after_submission_is_not_replayed_into_the_same_session(self):
        import httpx

        async def broken():
            raise httpx.ReadError("connection reset")
            yield b""  # pragma: no cover
        turns, handler = self._stateful(lambda: httpx.Response(200, stream=_AsyncBody(broken)))
        c, tr = _gateway(handler, max_retries=2)
        fb = FakeFallback(chunks=["direct."])
        client = OpenClawCompanionClient(c, tr, fb, "Kalli", "4C50", "conv1", sleep=no_sleep)
        first_key = client.state.session_key
        run(collect(client.streaming_call(THREAD, False)))
        self.assertEqual(sum(len(v) for v in turns.values()), 1)   # no blind replay
        self.assertEqual(len(turns[first_key]), 1)
        self.assertEqual(client.last_route, "fallback")
        # the next turn goes to a NEW session epoch with a bounded recap, never into the old transcript
        thread2 = THREAD + [{"role": "assistant", "content": "direct."}, {"role": "user", "content": "Player: well?"}]
        run(collect(client.streaming_call(thread2, False)))
        self.assertNotEqual(client.state.session_key, first_key)
        self.assertEqual(len(turns[first_key]), 1)
        self.assertEqual(len(turns[client.state.session_key]), 1)

    def test_stream_ended_without_content_is_ambiguous_too(self):
        import httpx
        turns, handler = self._stateful(lambda: httpx.Response(200, content=b""))
        c, tr = _gateway(handler, max_retries=2, fallback_to_direct=False)
        client = OpenClawCompanionClient(c, tr, FakeFallback(), "Kalli", "4C50", "conv1", sleep=no_sleep)
        run(collect(client.streaming_call(THREAD, False)))
        self.assertEqual(sum(len(v) for v in turns.values()), 1)

    def test_connect_error_is_pre_submission_and_still_retried(self):
        import httpx
        hits = []

        def handler(request):
            hits.append(1)
            if len(hits) == 1:
                raise httpx.ConnectError("refused")
            return httpx.Response(200, content=_sse(_chunk("ok"), "[DONE]"))
        c, tr = _gateway(handler, max_retries=2)
        client = OpenClawCompanionClient(c, tr, FakeFallback(), "Kalli", "4C50", "conv1", sleep=no_sleep)
        self.assertEqual(run(collect(client.streaming_call(THREAD, False))), [("content", "ok")])
        self.assertEqual(len(hits), 2)

    def test_character_scope_keeps_durable_key(self):
        import httpx

        async def broken():
            raise httpx.ReadError("reset")
            yield b""  # pragma: no cover
        turns, handler = self._stateful(lambda: httpx.Response(200, stream=_AsyncBody(broken)))
        c, tr = _gateway(handler, session_scope="character")
        client = OpenClawCompanionClient(c, tr, FakeFallback(), "Kalli", "4C50", "conv1", sleep=no_sleep)
        key = client.state.session_key
        run(collect(client.streaming_call(THREAD, False)))
        self.assertEqual(client.state.session_key, key)
        self.assertEqual(sum(len(v) for v in turns.values()), 1)


# --------------------------------------------------------------------------- B02 (ChatManager level)
class BlockingTransport:
    """First (or second) yield blocks until closed; records closes."""

    def __init__(self, chunks_before_block=0):
        self.before = chunks_before_block
        self.closed = threading.Event()
        self.requests = 0

    async def stream(self, request):
        self.requests += 1
        try:
            for _ in range(self.before):
                yield "First sentence here. "
            await asyncio.sleep(60)
            yield "never"
        finally:
            self.closed.set()

    async def aclose(self):
        return None


@pytest.mark.parametrize("before", [0, 1])
def test_chat_manager_stop_interrupts_blocked_stream(before, default_config, piper, mock_ai_client, monkeypatch,
                                                     example_characters_pc_to_npc):
    from unittest.mock import MagicMock
    from src.llm.message_thread import message_thread
    from src.llm.messages import UserMessage
    from src.llm.openclaw.config import OpenClawCompanionConfig
    from src.llm.openclaw.router import OpenClawRouter
    from src.llm.sentence_queue import SentenceQueue
    from src.output_manager import ChatManager

    piper.synthesize = MagicMock(return_value=("mock_audio_file.wav", False))
    monkeypatch.setattr('src.utils.get_audio_duration', lambda *a, **kw: 1.0)
    transport = BlockingTransport(before)
    ocfg = OpenClawCompanionConfig(enabled=True, transport="mock", characters=("Guard",), retry_backoff_s=0.0)
    manager = ChatManager(default_config, piper, mock_ai_client,
                          openclaw_router=OpenClawRouter(ocfg, transport_factory=lambda c: transport))
    t = message_thread(default_config, "You are Guard, a Whiterun guard.")
    t.add_message(UserMessage(default_config, "Hello there", "Player"))
    worker = threading.Thread(target=manager.generate_response,
                              args=(t, example_characters_pc_to_npc, SentenceQueue(), [], None), daemon=True)
    worker.start()
    deadline = time.monotonic() + 5
    while transport.requests == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)                                     # now blocked inside the stream
    t0 = time.monotonic()
    stopper = threading.Thread(target=manager.stop_generation, daemon=True)
    stopper.start()
    stopper.join(timeout=5)
    assert not stopper.is_alive(), "stop_generation did not return while the stream was blocked"
    assert time.monotonic() - t0 < 2.0
    assert transport.closed.wait(2.0), "HTTP stream was not closed on stop"
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert transport.requests == 1                      # stop never triggers a retry/replay
    assert mock_ai_client.call_count == 0               # nor a direct-provider fallback


# =========================================================================== Sol re-review N03-N06
class ErrorBodyDeadlineTests(unittest.TestCase):
    """N03: a non-200 response body is never read, so it cannot trickle past the deadline."""

    def test_trickling_503_body_is_not_consumed(self):
        reads = []

        async def body():
            for _ in range(10_000):
                reads.append(1)
                yield b"x" * 1024
                await asyncio.sleep(0.001)

        def handler(request):
            return httpx.Response(503, stream=_AsyncBody(body))
        c, tr = _gateway(handler, first_token_timeout_s=0.05, max_retries=0, fallback_to_direct=False)
        client = OpenClawCompanionClient(c, tr, FakeFallback(), "Kalli", "4C50", "conv1", sleep=no_sleep)
        t0 = time.monotonic()
        run(collect(client.streaming_call(THREAD, False)))
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertLess(len(reads), 5)
        self.assertIn("503", client.last_error)


class ErrorLabelAllowlistTests(unittest.TestCase):
    """N04: regex-valid peer labels can still be secrets; only allowlisted categories survive."""

    def _err(self, **fields):
        def handler(request):
            return httpx.Response(200, content=_sse(json.dumps({"error": dict(message="m", **fields)}), "[DONE]"))
        c, tr = _gateway(handler)
        client = OpenClawCompanionClient(c, tr, FakeFallback(), "Kalli", "4C50", "conv1", sleep=no_sleep)
        with mock.patch.dict(os.environ, {"MANTELLA_TEST_TOKEN": SENTINEL}), \
                self.assertLogs("mantella.openclaw", level=logging.DEBUG) as logs:
            run(collect(client.streaming_call(THREAD, False)))
        return "\n".join(logs.output) + "\n" + (client.last_error or ""), client

    def test_sentinel_in_code_or_type_is_dropped(self):
        for f in ({"code": SENTINEL}, {"type": SENTINEL}):
            text, client = self._err(**f)
            self.assertNotIn(SENTINEL, text)
            self.assertIn("unspecified", client.last_error)

    def test_known_category_is_mapped_to_local_label(self):
        text, client = self._err(type="rate_limit_error", code=SENTINEL)
        self.assertIn("rate limited", client.last_error)
        self.assertNotIn(SENTINEL, text)


def test_until_stopped_outer_cancellation_leaves_no_orphan():
    """N05: cancelling the task that drives the wrapper must cancel+join the pending __anext__."""
    from src.output_manager import _until_stopped
    state = {"finally": False}

    async def blocked():
        try:
            await asyncio.sleep(60)
            yield "never"
        finally:
            state["finally"] = True

    async def go():
        w = _until_stopped(blocked(), lambda: False)
        task = asyncio.ensure_future(w.__anext__())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await w.aclose()
        await asyncio.sleep(0)
        others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        return others
    assert run(go()) == []
    assert state["finally"]


def test_until_stopped_consumer_exception_closes_stream():
    from src.output_manager import _until_stopped
    state = {"finally": False}

    async def gen():
        try:
            yield "a"
            await asyncio.sleep(60)
            yield "b"
        finally:
            state["finally"] = True

    async def go():
        w = _until_stopped(gen(), lambda: False)
        try:
            async for _ in w:
                raise ValueError("consumer failed")
        except ValueError:
            pass
        finally:
            await w.aclose()
    run(go())
    assert state["finally"]


class UnconfirmedTurnEpochTests(unittest.TestCase):
    """N06: every termination after possible acceptance rotates the conversation-scope session."""

    def _recording(self, first_frames):
        keys = []

        def handler(request):
            keys.append(request.headers["x-openclaw-session-key"])
            frames = first_frames if len(keys) == 1 else [_chunk("ok"), "[DONE]"]
            return httpx.Response(200, content=_sse(*frames))
        return keys, handler

    def _two_turns(self, frames, **kw):
        keys, handler = self._recording(frames)
        c, tr = _gateway(handler, **kw)
        client = OpenClawCompanionClient(c, tr, FakeFallback(), "Kalli", "4C50", "conv1", sleep=no_sleep)
        first = run(collect(client.streaming_call(THREAD, False)))
        thread2 = THREAD + [{"role": "assistant", "content": "x"}, {"role": "user", "content": "Player: again"}]
        run(collect(client.streaming_call(thread2, False)))
        return keys, first, client

    def test_partial_eof_rotates(self):
        keys, first, _ = self._two_turns([_chunk("Half")])          # no [DONE]
        self.assertEqual(first, [("content", "Half")])
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0], keys[1])

    def test_reply_cap_truncation_rotates(self):
        keys, _, _ = self._two_turns([_chunk("x" * 50), _chunk("y" * 50), "[DONE]"], max_reply_chars=40)
        self.assertNotEqual(keys[0], keys[1])

    def test_done_without_content_is_not_a_spoken_reply(self):
        keys, first, client = self._two_turns(["[DONE]"])
        self.assertEqual(first, [("content", "direct reply.")])     # fallback policy applies
        self.assertEqual(len(keys), 2)                              # not retried into the same session
        self.assertNotEqual(keys[0], keys[1])

    def test_consumer_stop_before_first_token_rotates(self):
        keys = []

        async def body():
            await asyncio.sleep(60)
            yield b""

        def handler(request):
            keys.append(request.headers["x-openclaw-session-key"])
            if len(keys) == 1:
                return httpx.Response(200, stream=_AsyncBody(body))
            return httpx.Response(200, content=_sse(_chunk("ok"), "[DONE]"))
        c, tr = _gateway(handler)
        client = OpenClawCompanionClient(c, tr, FakeFallback(), "Kalli", "4C50", "conv1", sleep=no_sleep)

        async def stop_early():
            from src.output_manager import _until_stopped
            flag = {"stop": False}
            w = _until_stopped(client.streaming_call(THREAD, False), lambda: flag["stop"])
            asyncio.get_running_loop().call_later(0.1, lambda: flag.__setitem__("stop", True))
            async for _ in w:
                pass
        run(stop_early())
        run(collect(client.streaming_call(THREAD + [{"role": "user", "content": "Player: hm"}], False)))
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0], keys[1])

    def test_explicit_rejection_does_not_rotate(self):
        hits = []

        def handler(request):
            hits.append(request.headers["x-openclaw-session-key"])
            return httpx.Response(401)
        c, tr = _gateway(handler)
        client = OpenClawCompanionClient(c, tr, FakeFallback(), "Kalli", "4C50", "conv1", sleep=no_sleep)
        key = client.state.session_key
        run(collect(client.streaming_call(THREAD, False)))
        self.assertEqual(client.state.session_key, key)


# =========================================================================== B05 acceptance contract
PLAYER_LINE = "Hey Kalli, ready to go?"


class _ContractGateway:
    """Stateful fake Gateway with a configurable (unverified) busy contract.

    ``persist_then_busy``: the turn is appended to the session transcript and THEN answered with
    the busy status - the adversarial reading the real Gateway has not been shown to exclude.
    ``reject_before_persist``: busy answers never touch the transcript. ``busy`` = how many
    requests (globally, in order) get the busy answer before the Gateway recovers."""

    def __init__(self, contract, busy, status=503):
        assert contract in ("persist_then_busy", "reject_before_persist")
        self.contract, self.busy, self.status = contract, busy, status
        self.transcripts: dict[str, list[str]] = {}
        self.order: list[str] = []

    def handler(self, request):
        key = request.headers["x-openclaw-session-key"]
        content = json.loads(request.content)["messages"][0]["content"]
        self.order.append(key)
        busy = len(self.order) <= self.busy
        if not busy or self.contract == "persist_then_busy":
            self.transcripts.setdefault(key, []).append(content)
        if busy:
            return httpx.Response(self.status, headers={"retry-after": "0"})
        return httpx.Response(200, content=_sse(_chunk("ok"), "[DONE]"))

    def copies(self, key, text=PLAYER_LINE):
        return sum(text in turn for turn in self.transcripts.get(key, []))

    def max_copies(self, text=PLAYER_LINE):
        return max((self.copies(k, text) for k in self.transcripts), default=0)


class BusyAcceptanceContractTests(unittest.TestCase):
    """Sol review B05: no transcript may ever hold the player's turn twice, whichever busy
    contract the Gateway really has (it is not verified)."""

    def _client(self, gw, **kw):
        c, tr = _gateway(gw.handler, **kw)
        return OpenClawCompanionClient(c, tr, FakeFallback(chunks=["direct."]), "Kalli", "4C50", "conv1",
                                       sleep=no_sleep)

    def test_conversation_scope_busy_retries_are_safe_under_either_contract(self):
        for contract in ("persist_then_busy", "reject_before_persist"):
            for status in (503, 429):
                with self.subTest(contract=contract, status=status):
                    gw = _ContractGateway(contract, busy=2, status=status)
                    client = self._client(gw, max_retries=2)
                    first_key = client.state.session_key
                    self.assertEqual(run(collect(client.streaming_call(THREAD, False))), [("content", "ok")])
                    self.assertEqual(client.last_route, "openclaw")
                    # each attempt went to a session that could not already hold the turn
                    self.assertEqual(len(gw.order), 3)
                    self.assertEqual(len(set(gw.order)), 3)
                    self.assertEqual(gw.order[0], first_key)
                    self.assertEqual(gw.max_copies(), 1)
                    final = gw.order[-1]
                    self.assertEqual(client.state.session_key, final)
                    self.assertEqual(gw.copies(final), 1)                       # recap carried the turn
                    self.assertIn("fox-eared wanderer", gw.transcripts[final][0])  # with scene context
                    # the conversation then continues in that session with a delta, not a recap
                    thread2 = THREAD + [{"role": "assistant", "content": "ok"},
                                        {"role": "user", "content": "Player: well?"}]
                    run(collect(client.streaming_call(thread2, False)))
                    self.assertEqual(gw.order[-1], final)
                    self.assertNotIn(PLAYER_LINE, gw.transcripts[final][-1])
                    self.assertIn("well?", gw.transcripts[final][-1])
                    self.assertEqual(gw.max_copies(), 1)

    def test_exhausted_busy_retries_never_reuse_a_possibly_written_session(self):
        gw = _ContractGateway("persist_then_busy", busy=3)
        client = self._client(gw, max_retries=2)
        self.assertEqual(run(collect(client.streaming_call(THREAD, False))), [("content", "direct.")])
        self.assertEqual(client.last_route, "fallback")
        self.assertEqual(len(set(gw.order)), 3)
        # the next turn starts yet another epoch with a recap
        thread2 = THREAD + [{"role": "assistant", "content": "direct."}, {"role": "user", "content": "Player: well?"}]
        run(collect(client.streaming_call(thread2, False)))
        self.assertEqual(len(gw.order), 4)
        self.assertNotIn(gw.order[-1], gw.order[:3])
        self.assertEqual(gw.max_copies(), 1)

    def test_character_scope_does_not_retry_into_its_durable_session(self):
        for fallback in (True, False):
            with self.subTest(fallback=fallback):
                gw = _ContractGateway("persist_then_busy", busy=1)
                client = self._client(gw, session_scope="character", max_retries=2, fallback_to_direct=fallback)
                key = client.state.session_key
                items = run(collect(client.streaming_call(THREAD, False)))
                self.assertEqual(items, [("content", "direct.")] if fallback else [])
                self.assertEqual(client.last_route, "fallback" if fallback else "failed")
                self.assertEqual(gw.order, [key])                               # no retry
                self.assertEqual(client.state.session_key, key)                 # durable key kept
                self.assertEqual(gw.copies(key), 1)
                self.assertTrue(client.state.needs_resync)

    def test_owner_attested_contract_restores_same_session_retry(self):
        # Verified "busy is pre-acceptance": retry the identical request in the same session.
        for scope in ("conversation", "character"):
            with self.subTest(scope=scope):
                gw = _ContractGateway("reject_before_persist", busy=2)
                client = self._client(gw, session_scope=scope, max_retries=2, busy_status_is_pre_acceptance=True)
                key = client.state.session_key
                self.assertEqual(run(collect(client.streaming_call(THREAD, False))), [("content", "ok")])
                self.assertEqual(gw.order, [key] * 3)
                self.assertEqual(client.state.session_key, key)
                self.assertEqual(gw.copies(key), 1)

    def test_false_attestation_is_exactly_the_duplicate_hazard(self):
        # Pins what the opt-in trades away: if the attestation is wrong, the turn is duplicated.
        gw = _ContractGateway("persist_then_busy", busy=2)
        client = self._client(gw, max_retries=2, busy_status_is_pre_acceptance=True)
        run(collect(client.streaming_call(THREAD, False)))
        self.assertEqual(gw.copies(client.state.session_key), 3)

    def test_connect_failure_still_retries_in_place(self):
        hits = []

        def handler(request):
            hits.append(request.headers["x-openclaw-session-key"])
            if len(hits) == 1:
                raise httpx.ConnectError("refused")
            return httpx.Response(200, content=_sse(_chunk("ok"), "[DONE]"))
        c, tr = _gateway(handler, max_retries=2)
        client = OpenClawCompanionClient(c, tr, FakeFallback(), "Kalli", "4C50", "conv1", sleep=no_sleep)
        run(collect(client.streaming_call(THREAD, False)))
        self.assertEqual(len(set(hits)), 1)                                    # never reached: no rotation

    def test_attestation_must_be_a_real_boolean(self):
        from src.llm.openclaw.config import OpenClawCompanionConfig, OpenClawConfigError
        with self.assertRaises(OpenClawConfigError):
            OpenClawCompanionConfig.from_dict({"busy_status_is_pre_acceptance": "true"})
        self.assertFalse(OpenClawCompanionConfig().busy_status_is_pre_acceptance)
