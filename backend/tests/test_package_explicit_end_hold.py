"""Repair of Astra P1: the legacy 0.13.0 package ends a conversation with an EXPLICIT end request.

Compiled MantellaConversation (verified bytecode): ProcessNpcSpeak reads the goodbye's string actions and calls
RaiseActionEvent (:321) BEFORE NpcSpeak (:324); the end action calls EndConversation, which sends an explicit end
request at once; the end reply runs CleanupConversation. So the acknowledgement of that end request must wait for
the goodbye's remaining audio, captured BEFORE the manager drops the conversation, and only when the end
succeeded. Real mantella_route (package mode, FastAPI over ASGI), GameStateManager, Conversation and ChatManager;
stub TTS returns a real wav of known duration; rememberer/conversation_log are mocks so saves can be counted and
made to fail. No network, model or TTS service.
"""
import asyncio
import time
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI

import src.conversation.conversation as conversation_module
from src.conversation.conversation import Conversation
from src.game_manager import GameStateManager
from src.http.communication_constants import communication_constants as C
from src.http.legacy_package_protocol import PACKAGE_PROTOCOL
from src.http.routes.mantella_route import mantella_route

from tests.lifecycle_support import chat_manager_factory, wav_bytes  # noqa: F401
from tests.test_paired_candidate_interactions import strict_package_config

GOODBYE_S = 0.8
CONTINUE = {C.KEY_REQUESTTYPE: C.KEY_REQUESTTYPE_CONTINUECONVERSATION, C.KEY_CONTINUECONVERSATION_TOPICINFOFILE: 1}
INIT = {C.KEY_REQUESTTYPE: C.KEY_REQUESTTYPE_INIT}
END_TS = 123.25


def end_request(ts=END_TS):
    body = {C.KEY_REQUESTTYPE: C.KEY_REQUESTTYPE_ENDCONVERSATION}
    if ts is not None:
        body[C.KEY_ENDCONVERSATION_TIMESTAMP] = ts
    return body


@pytest.fixture
def rig(chat_manager_factory, default_config, english_language_info, tmp_path, monkeypatch,
        example_characters_pc_to_npc):
    saved = MagicMock()
    monkeypatch.setattr("src.conversation.conversation.conversation_log.save_conversation_log", saved)
    default_config.wait_time_buffer = 0.0
    wav = tmp_path / "goodbye.wav"
    wav.write_bytes(wav_bytes(GOODBYE_S))
    manager, _ = chat_manager_factory(str(wav))
    game = MagicMock()
    game.modify_sentence_text_for_game.side_effect = lambda text: text
    llm = MagicMock()
    llm.is_too_long.return_value = False
    gsm = GameStateManager(game, manager, default_config, english_language_info, llm)
    route = mantella_route(strict_package_config(), english_language_info, package_protocol=PACKAGE_PROTOCOL)
    route._mantella_route__game = gsm
    monkeypatch.setattr(route, "_can_route_be_used", lambda: True)
    protocol = route._mantella_route__package_protocol
    chars = example_characters_pc_to_npc
    roster = [{C.KEY_ACTOR_NAME: c.name, C.KEY_ACTOR_REFID: str(c.ref_id), C.KEY_ACTOR_ISPLAYER: c.is_player_character}
              for c in chars.get_all_characters()]
    app = FastAPI()
    route.add_route_to_server(app)

    def new_conversation(ending: bool = True):
        """A fresh real Conversation installed as the manager's current one and admitted by the adapter."""
        context = MagicMock()
        context.config = default_config
        context.npcs_in_conversation = chars
        context.have_actors_changed = False
        rememberer = MagicMock()
        talk = Conversation(context, manager, rememberer, llm, None, False, False)
        gsm._GameStateManager__talk = talk
        protocol._roster(roster)
        protocol.active = True
        if ending:
            talk.initiate_end_sequence()
        return NS(talk=talk, rememberer=rememberer)

    def client():
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mantella")
    return NS(gsm=gsm, protocol=protocol, saved=saved, new_conversation=new_conversation, client=client)


async def post(client, body):
    response = await client.post("/mantella", json=body)
    return response.json(), time.monotonic()


def saved_timestamps(rememberer):
    return [c.args[6] if len(c.args) > 6 else c.kwargs.get("end_timestamp")
            for c in rememberer.save_conversation_state.call_args_list]


def test_explicit_end_after_goodbye_is_held_saved_once_and_concurrent_requests_served(rig):
    conv = rig.new_conversation()

    async def scenario():
        async with rig.client() as client:
            goodbye, handed_out = await post(client, dict(CONTINUE))
            end_task = asyncio.create_task(post(client, end_request()))
            await asyncio.sleep(0.1)
            other, other_done = await post(client, dict(INIT))
            # saved and dropped before the acknowledgement is released
            dropped = rig.gsm._GameStateManager__talk is None
            saves = conv.rememberer.save_conversation_state.call_count
            end, end_done = await end_task
            return goodbye, handed_out, other, other_done, dropped, saves, end, end_done

    goodbye, handed_out, other, other_done, dropped, saves, end, end_done = asyncio.run(scenario())
    assert goodbye[C.KEY_REPLYTYPE_NPCTALK][C.KEY_ACTOR_ACTIONS] == [C.ACTION_ENDCONVERSATION]
    assert end == {C.KEY_REPLYTYPE: C.KEY_REPLYTYPE_ENDCONVERSATION}
    assert GOODBYE_S - 0.05 <= end_done - handed_out < GOODBYE_S + 1.0
    assert other == {C.KEY_REPLYTYPE: C.KEY_REPLYTTYPE_INITCOMPLETED} and other_done < end_done
    assert dropped and saves == 1 and rig.saved.call_count == 1
    assert saved_timestamps(conv.rememberer) == [END_TS]
    assert rig.gsm.pop_end_ack_hold_seconds() == 0.0  # consumed, nothing left for a later request


def test_explicit_end_after_goodbye_has_played_is_not_held(rig):
    rig.new_conversation()

    async def scenario():
        async with rig.client() as client:
            await post(client, dict(CONTINUE))
            await asyncio.sleep(GOODBYE_S + 0.05)
            start = time.monotonic()
            end, done = await post(client, end_request())
            return end, done - start

    end, took = asyncio.run(scenario())
    assert end == {C.KEY_REPLYTYPE: C.KEY_REPLYTYPE_ENDCONVERSATION} and took < 0.3


def test_ordinary_end_without_goodbye_is_not_held(rig):
    conv = rig.new_conversation(ending=False)

    async def scenario():
        async with rig.client() as client:
            start = time.monotonic()
            end, done = await post(client, end_request())
            return end, done - start

    end, took = asyncio.run(scenario())
    assert end == {C.KEY_REPLYTYPE: C.KEY_REPLYTYPE_ENDCONVERSATION} and took < 0.3
    assert saved_timestamps(conv.rememberer) == [END_TS]


def test_duplicate_end_is_prompt_and_saves_once(rig):
    conv = rig.new_conversation()

    async def scenario():
        async with rig.client() as client:
            _, handed_out = await post(client, dict(CONTINUE))
            first, first_done = await post(client, end_request())
            start = time.monotonic()
            second, second_done = await post(client, end_request(ts=999.0))
            return handed_out, first, first_done, second, second_done - start

    handed_out, first, first_done, second, second_took = asyncio.run(scenario())
    assert first == second == {C.KEY_REPLYTYPE: C.KEY_REPLYTYPE_ENDCONVERSATION}
    assert first_done - handed_out >= GOODBYE_S - 0.05
    assert second_took < 0.3
    assert conv.rememberer.save_conversation_state.call_count == 1
    assert saved_timestamps(conv.rememberer) == [END_TS]


def test_failed_save_error_is_prompt_and_retry_is_held_with_original_timestamp(rig):
    """Save fails twice (the explicit end and the adapter's cleanup attempt): error reply at once, conversation
    kept. The next end retries, succeeds, keeps the FIRST timestamp, and its acknowledgement still waits for the
    goodbye that is still playing."""
    conv = rig.new_conversation()
    conv.rememberer.save_conversation_state.side_effect = [OSError("disk full"), OSError("disk full"), None]

    async def scenario():
        async with rig.client() as client:
            _, handed_out = await post(client, dict(CONTINUE))
            t0 = time.monotonic()
            failed, failed_done = await post(client, end_request())
            kept = rig.gsm._GameStateManager__talk is conv.talk
            retry, retry_done = await post(client, end_request(ts=500.0))
            return handed_out, t0, failed, failed_done, kept, retry, retry_done

    handed_out, t0, failed, failed_done, kept, retry, retry_done = asyncio.run(scenario())
    assert failed[C.KEY_REPLYTYPE] == "error" and "cleanup failed" in failed["mantella_message"]
    assert failed_done - t0 < 0.3, "an error must not be held"
    assert kept
    assert retry == {C.KEY_REPLYTYPE: C.KEY_REPLYTYPE_ENDCONVERSATION}
    assert retry_done - handed_out >= GOODBYE_S - 0.05
    assert saved_timestamps(conv.rememberer) == [END_TS, END_TS, END_TS]
    assert rig.gsm._GameStateManager__talk is None


def test_failed_save_recovered_by_cleanup_still_reports_error_promptly(rig):
    conv = rig.new_conversation()
    conv.rememberer.save_conversation_state.side_effect = [OSError("disk full"), None]

    async def scenario():
        async with rig.client() as client:
            await post(client, dict(CONTINUE))
            t0 = time.monotonic()
            failed, done = await post(client, end_request())
            return failed, done - t0

    failed, took = asyncio.run(scenario())
    assert failed[C.KEY_REPLYTYPE] == "error" and "cleanup attempted" in failed["mantella_message"]
    assert took < 0.3
    assert conv.rememberer.save_conversation_state.call_count == 2
    assert rig.gsm._GameStateManager__talk is None
    assert rig.gsm.pop_end_ack_hold_seconds() <= GOODBYE_S  # deadline never delays error responses


def test_refused_continue_after_goodbye_is_not_held(rig, monkeypatch):
    """A non-end/refused package reply carrying a pending goodbye is never delayed."""
    rig.new_conversation()

    async def scenario():
        async with rig.client() as client:
            await post(client, dict(CONTINUE))
            t0 = time.monotonic()
            refused, done = await post(client, {**CONTINUE, C.KEY_INPUTTYPE: C.KEY_INPUTTYPE_MIC})
            return refused, done - t0

    refused, took = asyncio.run(scenario())
    assert refused[C.KEY_REPLYTYPE] == "error" and "Microphone" in refused["mantella_message"]
    assert took < 0.3


def test_second_conversation_has_its_own_goodbye_deadline(rig):
    rig.new_conversation()

    async def first():
        async with rig.client() as client:
            await post(client, dict(CONTINUE))
            await post(client, end_request())
    asyncio.run(first())
    second = rig.new_conversation(ending=False)

    async def ordinary_end():
        async with rig.client() as client:
            t0 = time.monotonic()
            end, done = await post(client, end_request())
            return end, done - t0
    end, took = asyncio.run(ordinary_end())
    assert end == {C.KEY_REPLYTYPE: C.KEY_REPLYTYPE_ENDCONVERSATION} and took < 0.3
    assert second.rememberer.save_conversation_state.call_count == 1

    third = rig.new_conversation()

    async def goodbye_end():
        async with rig.client() as client:
            _, handed_out = await post(client, dict(CONTINUE))
            end, done = await post(client, end_request())
            return end, done - handed_out
    end, held = asyncio.run(goodbye_end())
    assert end == {C.KEY_REPLYTYPE: C.KEY_REPLYTYPE_ENDCONVERSATION} and held >= GOODBYE_S - 0.05
    assert third.rememberer.save_conversation_state.call_count == 1


def test_goodbye_hold_is_bounded(rig, monkeypatch):
    monkeypatch.setattr(conversation_module, "MAX_GOODBYE_HOLD_S", 0.2)
    rig.new_conversation()

    async def scenario():
        async with rig.client() as client:
            _, handed_out = await post(client, dict(CONTINUE))
            end, done = await post(client, end_request())
            return end, done - handed_out
    end, held = asyncio.run(scenario())
    assert end == {C.KEY_REPLYTYPE: C.KEY_REPLYTYPE_ENDCONVERSATION}
    assert 0.15 <= held < 0.5


def test_modern_route_explicit_end_unchanged(chat_manager_factory, default_config, english_language_info, tmp_path,
                                             monkeypatch, example_characters_pc_to_npc):
    """The modern route (action-aware scripts) still answers an explicit end at once (pass2 behaviour); the
    captured hold is only consumed by the package route and is reset by the next end."""
    from tests.test_lifecycle_goodbye_audio import served  # noqa: F401
    monkeypatch.setattr("src.conversation.conversation.conversation_log.save_conversation_log", MagicMock())
    default_config.wait_time_buffer = 0.0
    wav = tmp_path / "goodbye.wav"
    wav.write_bytes(wav_bytes(GOODBYE_S))
    manager, _ = chat_manager_factory(str(wav))
    context = MagicMock(config=default_config, npcs_in_conversation=example_characters_pc_to_npc,
                        have_actors_changed=False)
    llm = MagicMock()
    llm.is_too_long.return_value = False
    talk = Conversation(context, manager, MagicMock(), llm, None, False, False)
    gsm = GameStateManager(MagicMock(), manager, default_config, english_language_info, llm)
    gsm._GameStateManager__talk = talk
    talk.initiate_end_sequence()
    route = mantella_route(default_config, english_language_info)
    route._mantella_route__game = gsm
    monkeypatch.setattr(route, "_can_route_be_used", lambda: True)
    app = FastAPI()
    route.add_route_to_server(app)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://m") as client:
            await post(client, dict(CONTINUE))
            t0 = time.monotonic()
            end, done = await post(client, end_request())
            return end, done - t0
    end, took = asyncio.run(scenario())
    assert end[C.KEY_REPLYTYPE] == C.KEY_REPLYTYPE_ENDCONVERSATION and took < 0.3
    # Modern replies were immediate; the legacy-only deadline remains available for duplicate requests.
    gsm.end_conversation({})
    assert 0.0 <= gsm.pop_end_ack_hold_seconds() <= GOODBYE_S


def test_concurrent_duplicate_end_cannot_acknowledge_before_goodbye(rig):
    conv = rig.new_conversation()
    async def scenario():
        async with rig.client() as client:
            _, handed = await post(client, dict(CONTINUE))
            first = asyncio.create_task(post(client, end_request()))
            await asyncio.sleep(0.05)
            second = asyncio.create_task(post(client, end_request(ts=999.0)))
            return handed, await first, await second
    handed, first, second = asyncio.run(scenario())
    for reply, done in (first, second):
        assert reply == {C.KEY_REPLYTYPE: C.KEY_REPLYTYPE_ENDCONVERSATION}
        assert done - handed >= GOODBYE_S - 0.05
    assert conv.rememberer.save_conversation_state.call_count == 1
