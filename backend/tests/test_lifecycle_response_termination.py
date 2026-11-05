"""R-01: every generation ends the response the HTTP handler is waiting on (no backend wedge).

continue_conversation blocks in SentenceQueue.get_next_sentence on uvicorn's only event-loop thread
until a sentence arrives or is_more_to_come goes False. Paths that let the generation thread finish
without doing that left the request - and every later request (end, next start) - blocked forever,
and left ChatManager.is_generating True so stop_generation spun forever.
"""
from unittest.mock import MagicMock

from src.characters_manager import Characters
from src.conversation.conversation import Conversation
from src.game_manager import GameStateManager
from src.http.communication_constants import communication_constants as comm_consts
from src.llm.sentence_queue import SentenceQueue

from tests.lifecycle_support import (chat_manager_factory, drain, make_thread, start_generation,  # noqa: F401
                                     wav_bytes, within)


def test_generation_without_any_npc_ends_the_response(chat_manager_factory, default_config):
    """generate_response returned early (no NPC) without ending the queue."""
    manager, _ = chat_manager_factory()
    queue = start_generation(manager, default_config, Characters())
    finished, first = within(queue.get_next_sentence)
    assert finished, "continue_conversation would block forever on the uvicorn event-loop thread"
    assert first is not None and first.error_message  # actionable error reply, not a silent empty turn
    assert len(first.text) > 0                        # non-empty: GameStateManager turns it into an error reply
    assert not queue.is_more_to_come
    finished, _ = within(manager.stop_generation)
    assert finished, "stop_generation spun forever on is_generating"


def test_no_npc_conversation_request_returns_error_reply(chat_manager_factory, default_config,
                                                        english_language_info):
    """Production path: the uvicorn handler runs GameStateManager.continue_conversation on the event-loop
    thread. With no NPC loaded (every actor failed to load), start_conversation's radiant greeting
    started a generation that never ended the queue, and the next continue_conversation blocked forever,
    so the server never answered this or any later request (end, next start)."""
    manager, _ = chat_manager_factory()
    context = MagicMock()
    context.config = default_config
    context.npcs_in_conversation = Characters()          # no player, no NPC: radiant
    context.get_context_ingame_events.return_value = []
    context.have_actors_changed = False
    llm = MagicMock()
    llm.is_too_long.return_value = False
    talk = Conversation(context, manager, MagicMock(), llm, None, False, False)
    talk.start_conversation()                            # radiant start prompt -> generation thread
    gsm = GameStateManager(MagicMock(), manager, default_config, english_language_info, llm)
    gsm._GameStateManager__talk = talk
    finished, reply = within(lambda: gsm.continue_conversation(
        {comm_consts.KEY_REQUESTTYPE: comm_consts.KEY_REQUESTTYPE_CONTINUECONVERSATION}))
    assert finished, "continue_conversation never returned: the whole backend would stop answering"
    assert reply[comm_consts.KEY_REPLYTYPE] == "error"
    assert "no NPC" in reply["mantella_message"]
    assert talk.has_already_ended


def test_exception_escaping_process_response_ends_the_response(chat_manager_factory, default_config,
                                                               example_characters_pc_to_npc, monkeypatch):
    """Anything raised outside process_response's own try/finally (here: before it) killed the
    generation thread with the queue still open and is_generating stuck True."""
    manager, _ = chat_manager_factory()
    monkeypatch.setattr(example_characters_pc_to_npc, "contains_player_character",
                        MagicMock(side_effect=RuntimeError("unexpected")))
    queue = start_generation(manager, default_config, example_characters_pc_to_npc)
    sentences = drain(queue)
    assert len(sentences) == 1 and sentences[0].error_message and len(sentences[0].text) > 0
    assert not queue.is_more_to_come
    finished, _ = within(manager.stop_generation)
    assert finished


def test_client_routing_failure_ends_the_turn(chat_manager_factory, default_config, example_characters_pc_to_npc):
    """A failure while resolving the LLM client is caught by process_response's outer except, but its
    finally then logged with the unbound active_client (UnboundLocalError) and skipped the end of the
    response. Upstream's intent for such errors - log and end the turn - must actually happen."""
    manager, _ = chat_manager_factory()
    manager._get_per_character_client = MagicMock(side_effect=RuntimeError("routing failed"))
    queue = start_generation(manager, default_config, example_characters_pc_to_npc)
    sentences = drain(queue)
    assert len(sentences) == 1            # exactly one terminator
    assert not queue.is_more_to_come
    finished, _ = within(manager.stop_generation)
    assert finished


def test_normal_response_still_ends_exactly_once(chat_manager_factory, default_config, tmp_path,
                                                 example_characters_pc_to_npc):
    """No extra terminator when process_response ended the response itself (a second empty sentinel
    would be read as a second end of response by the next continue_conversation)."""
    wav = tmp_path / "line.wav"
    wav.write_bytes(wav_bytes(1.0))
    manager, _ = chat_manager_factory(str(wav))
    queue = start_generation(manager, default_config, example_characters_pc_to_npc)
    sentences = drain(queue)
    spoken = [s for s in sentences if s.text.strip()]
    sentinels = [s for s in sentences if not s.text.strip()]
    assert [s.text.strip() for s in spoken] == ["Hello there."]
    assert not any(s.error_message for s in sentences)
    assert len(sentinels) == 1
    assert queue.get_next_sentence() is None


def test_terminator_is_queued_before_no_more_to_come(chat_manager_factory, default_config, tmp_path,
                                                     example_characters_pc_to_npc):
    """A consumer that looks at the queue the instant the producer declares 'no more to come' must
    already find the terminating sentence; otherwise it returns None (same as an end of response) and
    the late sentinel is left in the queue, where the next continue_conversation reads it as a second
    end of response (radiant conversations then add an extra prompt)."""
    seen = []

    class ProbingQueue(SentenceQueue):
        @SentenceQueue.is_more_to_come.setter
        def is_more_to_come(self, value):
            SentenceQueue.is_more_to_come.fset(self, value)
            if value is False:
                seen.append(self.get_next_sentence())  # what a consumer checking right now would get

    wav = tmp_path / "line.wav"
    wav.write_bytes(wav_bytes(0.1))
    manager, _ = chat_manager_factory(str(wav))
    for characters in (Characters(), example_characters_pc_to_npc):  # error path, normal path
        queue = ProbingQueue()
        queue.is_more_to_come = True
        seen.clear()
        manager.generate_response(make_thread(default_config), characters, queue, [], None)
        assert seen and seen[0] is not None, "terminator was not queued when is_more_to_come went False"
