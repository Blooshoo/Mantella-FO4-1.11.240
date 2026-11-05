"""R-07: the NPC answers an action's in-game result, even when that result arrives while the response that
requested the action is still finishing.

An advanced action that needs the game's answer (requires_response, e.g. Inventory) is sent as an action-only
sentence as soon as the tool call is parsed (ChatManager.process_response), then generation stops - but the
generation thread is still alive: it drains the rest of the stream (the provider's finish / usage chunks), closes
it and queues the response terminator. The game executes the action and sends its result with the next
continue request. Conversation.resume_after_interrupting_action then adds the result as a user message, clears
the queue and calls __start_generating_npc_sentences, which only starts a thread when the previous one is no
longer alive. If the result came in during that tail, nothing was started (resume still reported success), the
old thread's terminator ended the response and the game was told to wait for player input: the action result was
never answered. Now the pre-action generation is stopped and joined before the continuation starts.

Deterministic: the mock LLM holds its stream open after the tool call, and the action result is delivered while
it does. Real Conversation, ChatManager and GameStateManager; no network, no TTS service.
"""
import asyncio
import json
import threading
import time
from unittest.mock import MagicMock

import pytest

from src.conversation.conversation import Conversation
from src.game_manager import GameStateManager
from src.http.communication_constants import communication_constants as comm_consts
from src.llm.messages import UserMessage

from tests.lifecycle_support import chat_manager_factory, wav_bytes, within  # noqa: F401

CONTINUE = {comm_consts.KEY_REQUESTTYPE: comm_consts.KEY_REQUESTTYPE_CONTINUECONVERSATION,
            comm_consts.KEY_CONTINUECONVERSATION_TOPICINFOFILE: 1}
ANSWER = "Here you go, take what you need."


class ToolCallThenTail:
    """First call: an Inventory tool call, then the stream stays open for tail_s. Second call: the answer."""

    def __init__(self, npc_name: str, tail_s: float):
        self.npc_name = npc_name
        self.tail_s = tail_s
        self.calls = 0
        self.stream_closed = threading.Event()

    async def streaming_call(self, messages=None, is_multi_npc=False, tools=None):
        self.calls += 1
        if self.calls == 1:
            try:
                yield ("tool_calls", [{"id": "call_1", "type": "function",
                                       "function": {"name": "Inventory",
                                                    "arguments": json.dumps({"source": self.npc_name})}}])
                await asyncio.sleep(self.tail_s)   # finish / usage chunks still to come
            finally:
                self.stream_closed.set()
            return
        yield ("content", ANSWER)

    def get_count_tokens(self, text):
        return len(str(text).split())

    def is_too_long(self, messages, token_limit_percent):
        return False


@pytest.fixture
def action_turn(chat_manager_factory, default_config, english_language_info, tmp_path,
                example_characters_pc_to_npc):
    def make(tail_s: float):
        npc = example_characters_pc_to_npc.last_added_character
        client = ToolCallThenTail(npc.name, tail_s)
        wav = tmp_path / "line.wav"
        wav.write_bytes(wav_bytes(0.05))
        manager, _ = chat_manager_factory(str(wav), client=client)
        context = MagicMock()
        context.config = default_config
        context.npcs_in_conversation = example_characters_pc_to_npc
        context.have_actors_changed = False
        context.get_context_ingame_events.return_value = []
        llm = MagicMock()
        llm.is_too_long.return_value = False
        talk = Conversation(context, manager, MagicMock(), llm, None, False, False)
        talk._Conversation__messages.add_message(UserMessage(default_config, "Show me what you've got.", "Player"))
        game = MagicMock()
        game.modify_sentence_text_for_game.side_effect = lambda text: text
        gsm = GameStateManager(game, manager, default_config, english_language_info, llm)
        gsm._GameStateManager__talk = talk
        talk._Conversation__start_generating_npc_sentences()
        return gsm, talk, context, client
    return make


def request(gsm):
    finished, reply = within(lambda: gsm.continue_conversation(dict(CONTINUE)))
    assert finished, "the continue request never got an answer"
    return reply


def deliver_action_result(context):
    context.get_context_ingame_events.return_value = ["Guard opened their inventory. The player took a stimpak."]


@pytest.mark.parametrize("result_arrives", ["while_the_response_is_finishing", "after_it_finished"])
def test_action_result_is_answered(action_turn, result_arrives):
    gsm, talk, context, client = action_turn(tail_s=1.5 if result_arrives == "while_the_response_is_finishing" else 0.0)
    action = request(gsm)
    assert action[comm_consts.KEY_REPLYTYPE] == comm_consts.KEY_REPLYTYPE_NPCACTION
    assert action[comm_consts.KEY_REPLYTYPE_NPCACTION][comm_consts.KEY_ACTOR_ACTIONS_REQUIRE_RESPONSE] is True
    if result_arrives == "after_it_finished":   # control: the case that already worked
        assert client.stream_closed.wait(5)
        deadline = time.time() + 5
        while talk._Conversation__generation_thread.is_alive() and time.time() < deadline:
            time.sleep(0.01)
    else:
        assert not client.stream_closed.is_set(), "precondition: the response must still be finishing"
    deliver_action_result(context)
    reply = request(gsm)                          # the game's continue request carrying the action result
    assert reply[comm_consts.KEY_REPLYTYPE] == comm_consts.KEY_REPLYTYPE_NPCTALK, \
        f"the action result was never answered (reply: {reply[comm_consts.KEY_REPLYTYPE]})"
    assert reply[comm_consts.KEY_REPLYTYPE_NPCTALK][comm_consts.KEY_ACTOR_LINETOSPEAK] == ANSWER
    assert client.calls == 2


def test_no_pre_action_sentence_leaks_into_the_continuation(action_turn):
    """Whatever the pre-action response still queued is discarded, as when it had finished first."""
    gsm, talk, context, client = action_turn(tail_s=1.5)
    request(gsm)
    deliver_action_result(context)
    first = request(gsm)
    assert first[comm_consts.KEY_REPLYTYPE_NPCTALK][comm_consts.KEY_ACTOR_LINETOSPEAK] == ANSWER
    assert client.stream_closed.is_set()          # the pre-action stream was closed, not left running
