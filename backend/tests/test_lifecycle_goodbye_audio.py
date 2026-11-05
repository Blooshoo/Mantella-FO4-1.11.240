"""Repair pass 2 (O-10): the end reply that follows a goodbye is held until the goodbye has played - without
blocking the server.

R-06 answers the line request an action-blind script sends right after the goodbye's Say with
mantella_end_conversation, which the script handles by cleaning the conversation up. That reply went out ~1 ms
after the goodbye was handed to the game, while every OTHER next line is held by the backend until the previous
line's estimated duration (+ wait_time_buffer) has elapsed (Conversation.continue_conversation), because the game
requests the next line right after Say. So the backend declared the end while the goodbye was still being spoken.

The end reply is now held for the goodbye's remaining duration - the same hold, for the same request, as every
other next line, so it relies on no script behaviour beyond what every line already relies on. The conversation is
saved and dropped first, and the hold is an asyncio.sleep in the HTTP route, so other requests are served
meanwhile (the existing next-line hold, by contrast, runs synchronously). An end REQUEST (action-aware scripts) and
an error end are not held. Whether the engine actually keeps playing the goodbye through CleanupConversation
cannot be observed here: that remains an in-game acceptance check (ACCEPTANCE-CHECKLIST.md).

Real mantella_route (FastAPI, driven over ASGI), GameStateManager, Conversation and ChatManager; the TTS is a stub
returning a real wav of known duration, the LLM is not called, conversation_log is mocked.
"""
import asyncio
import time
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI

from src.conversation.conversation import Conversation
from src.game_manager import GameStateManager
from src.http.communication_constants import communication_constants as comm_consts
from src.http.routes.mantella_route import mantella_route

from tests.lifecycle_support import chat_manager_factory, wav_bytes  # noqa: F401

GOODBYE_S = 0.8
CONTINUE = {comm_consts.KEY_REQUESTTYPE: comm_consts.KEY_REQUESTTYPE_CONTINUECONVERSATION,
            comm_consts.KEY_CONTINUECONVERSATION_TOPICINFOFILE: 1}
INIT = {comm_consts.KEY_REQUESTTYPE: comm_consts.KEY_REQUESTTYPE_INIT}


@pytest.fixture
def served(chat_manager_factory, default_config, english_language_info, tmp_path, monkeypatch,
           example_characters_pc_to_npc):
    """A conversation whose end sequence has started, behind the real /mantella route."""
    saved = MagicMock()
    monkeypatch.setattr("src.conversation.conversation.conversation_log.save_conversation_log", saved)
    default_config.wait_time_buffer = 0.0
    wav = tmp_path / "goodbye.wav"
    wav.write_bytes(wav_bytes(GOODBYE_S))
    manager, _ = chat_manager_factory(str(wav))
    context = MagicMock()
    context.config = default_config
    context.npcs_in_conversation = example_characters_pc_to_npc
    context.have_actors_changed = False
    llm = MagicMock()
    llm.is_too_long.return_value = False
    rememberer = MagicMock()
    talk = Conversation(context, manager, rememberer, llm, None, False, False)
    gsm = GameStateManager(MagicMock(), manager, default_config, english_language_info, llm)
    gsm._GameStateManager__talk = talk
    talk.initiate_end_sequence()

    route = mantella_route(default_config, english_language_info)
    route._mantella_route__game = gsm
    monkeypatch.setattr(route, "_can_route_be_used", lambda: True)
    app = FastAPI()
    route.add_route_to_server(app)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mantella")
    return client, gsm, rememberer, saved


async def post(client, body):
    response = await client.post("/mantella", json=body)
    return response.json(), time.monotonic()


def test_goodbye_end_reply_waits_for_outstanding_audio(served):
    client, gsm, rememberer, saved = served

    async def scenario():
        goodbye, handed_out = await post(client, dict(CONTINUE))
        assert goodbye[comm_consts.KEY_REPLYTYPE] == comm_consts.KEY_REPLYTYPE_NPCTALK
        assert goodbye[comm_consts.KEY_REPLYTYPE_NPCTALK][comm_consts.KEY_ACTOR_DURATION] == pytest.approx(GOODBYE_S, abs=0.05)
        # the action-blind script asks for the next line right after Say
        end_task = asyncio.create_task(post(client, dict(CONTINUE)))
        await asyncio.sleep(0.1)
        # meanwhile the server answers other requests (the hold does not block the event loop) and the
        # conversation is already saved and dropped
        other, other_done = await post(client, dict(INIT))
        assert other[comm_consts.KEY_REPLYTYPE] == comm_consts.KEY_REPLYTTYPE_INITCOMPLETED
        assert gsm._GameStateManager__talk is None
        assert rememberer.save_conversation_state.call_count == 1 and saved.call_count == 1
        end, end_done = await end_task
        await client.aclose()
        return goodbye, handed_out, other_done, end, end_done

    goodbye, handed_out, other_done, end, end_done = asyncio.run(scenario())
    assert end[comm_consts.KEY_REPLYTYPE] == comm_consts.KEY_REPLYTYPE_ENDCONVERSATION
    held = end_done - handed_out
    assert held >= GOODBYE_S - 0.05, f"end reply sent {held:.3f} s after a {GOODBYE_S} s goodbye was handed out"
    assert held < GOODBYE_S + 1.0
    assert other_done < end_done, "the held end reply blocked another request"


def test_end_request_after_goodbye_is_not_held(served):
    """Control (passes before and after): an action-aware script's end request is answered at once."""
    client, gsm, rememberer, _ = served

    async def scenario():
        await post(client, dict(CONTINUE))
        start = time.monotonic()
        end, done = await post(client, {comm_consts.KEY_REQUESTTYPE: comm_consts.KEY_REQUESTTYPE_ENDCONVERSATION})
        await client.aclose()
        return end, done - start

    end, took = asyncio.run(scenario())
    assert end[comm_consts.KEY_REPLYTYPE] == comm_consts.KEY_REPLYTYPE_ENDCONVERSATION
    assert took < 0.3


def test_no_hold_once_the_goodbye_has_played(served):
    """Control (passes before and after): a line request that comes after the goodbye's duration gets the end
    reply without delay."""
    client, *_ = served

    async def scenario():
        await post(client, dict(CONTINUE))
        await asyncio.sleep(GOODBYE_S + 0.05)
        start = time.monotonic()
        end, done = await post(client, dict(CONTINUE))
        await client.aclose()
        return end, done - start

    end, took = asyncio.run(scenario())
    assert end[comm_consts.KEY_REPLYTYPE] == comm_consts.KEY_REPLYTYPE_ENDCONVERSATION
    assert took < 0.3
