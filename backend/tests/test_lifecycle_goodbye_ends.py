"""R-06: a conversation whose goodbye has been delivered ends, even if the game ignores the end action.

The backend ends a conversation by sending a goodbye line carrying the action
{'identifier': 'mantella_end_conversation'}; the game is expected to answer with an end request. The public
Fallout 4 scripts (YetAnotherModder/fallout-4-mantella-mod b9d7298, MantellaConversation.psc ProcessNpcSpeak)
read actions with getStringArray, which never matches this backend's dictionaries (the FO4 maintainer's
backend fork sends plain strings), so they request the next line instead - and were told to wait for player
input, in a conversation that had already said goodbye (mic input is already off). They do handle a
mantella_end_conversation reply (ContinueConversation -> CleanupConversation), so that is what they now get.
Action-aware scripts stop requesting lines after the end action (_hasBeenStopped) and are unaffected.
"""
from unittest.mock import MagicMock

import pytest

from src.conversation.conversation import Conversation
from src.game_manager import GameStateManager
from src.http.communication_constants import communication_constants as comm_consts

from tests.lifecycle_support import chat_manager_factory, wav_bytes  # noqa: F401

CONTINUE = {comm_consts.KEY_REQUESTTYPE: comm_consts.KEY_REQUESTTYPE_CONTINUECONVERSATION,
            comm_consts.KEY_CONTINUECONVERSATION_TOPICINFOFILE: 1}


@pytest.fixture
def ending_conversation(chat_manager_factory, default_config, english_language_info, tmp_path, monkeypatch,
                        example_characters_pc_to_npc):
    """A pc_to_npc conversation inside a GameStateManager whose end sequence was just started (the player
    said goodbye / the LLM called the end tool / a radiant conversation reached its last turn)."""
    saved = MagicMock()
    monkeypatch.setattr("src.conversation.conversation.conversation_log.save_conversation_log", saved)
    default_config.conversation_summary_enabled = True
    wav = tmp_path / "goodbye.wav"
    wav.write_bytes(wav_bytes(0.1))
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
    return gsm, talk, rememberer, saved


def test_goodbye_then_next_line_request_ends_the_conversation(ending_conversation):
    gsm, talk, rememberer, saved = ending_conversation
    goodbye = gsm.continue_conversation(dict(CONTINUE))
    assert goodbye[comm_consts.KEY_REPLYTYPE] == comm_consts.KEY_REPLYTYPE_NPCTALK
    actions = goodbye[comm_consts.KEY_REPLYTYPE_NPCTALK][comm_consts.KEY_ACTOR_ACTIONS]
    assert {'identifier': comm_consts.ACTION_ENDCONVERSATION} in actions
    # an action-blind game asks for the next line instead of sending an end request
    after = gsm.continue_conversation(dict(CONTINUE))
    assert after[comm_consts.KEY_REPLYTYPE] == comm_consts.KEY_REPLYTYPE_ENDCONVERSATION, \
        "the game was told to wait for player input after the goodbye"
    assert talk.has_already_ended
    assert gsm._GameStateManager__talk is None          # dropped, like an end request
    assert rememberer.save_conversation_state.call_count == 1 and saved.call_count == 1


def test_radiant_conversation_says_goodbye_once_and_ends(chat_manager_factory, default_config, english_language_info,
                                                         tmp_path, monkeypatch, example_skyrim_npc_character,
                                                         another_example_skyrim_npc_character):
    """Radiant (NPC-NPC): after the last turn should_end stays True, so each further line request started
    the end sequence again - a new goodbye (TTS) per request, forever, for an action-blind game."""
    from src.characters_manager import Characters
    from src.llm.messages import AssistantMessage, UserMessage
    monkeypatch.setattr("src.conversation.conversation.conversation_log.save_conversation_log", MagicMock())
    wav = tmp_path / "goodbye.wav"
    wav.write_bytes(wav_bytes(0.1))
    manager, tts = chat_manager_factory(str(wav))
    npcs = Characters()
    npcs.add_or_update_character(example_skyrim_npc_character)
    npcs.add_or_update_character(another_example_skyrim_npc_character)
    default_config.radiant_max_turns = 1
    context = MagicMock()
    context.config = default_config
    context.npcs_in_conversation = npcs
    context.have_actors_changed = False
    llm = MagicMock()
    llm.is_too_long.return_value = False
    talk = Conversation(context, manager, MagicMock(), llm, None, False, False)
    thread = talk._Conversation__messages
    thread.add_message(UserMessage(default_config, "Start talking.", "", True))
    thread.add_message(AssistantMessage(default_config))           # the one radiant turn is done
    gsm = GameStateManager(MagicMock(), manager, default_config, english_language_info, llm)
    gsm._GameStateManager__talk = talk
    replies = []
    for _ in range(6):
        reply = gsm.continue_conversation(dict(CONTINUE))
        replies.append(reply[comm_consts.KEY_REPLYTYPE])
        if replies[-1] == comm_consts.KEY_REPLYTYPE_ENDCONVERSATION:
            break
    assert replies[-1] == comm_consts.KEY_REPLYTYPE_ENDCONVERSATION, f"never ended: {replies}"
    assert tts.synthesize.call_count == 1          # one goodbye, not one per request


def test_end_request_after_goodbye_unchanged(ending_conversation):
    """Action-aware game: the end action makes it send an end request; nothing else changes."""
    gsm, talk, rememberer, saved = ending_conversation
    gsm.continue_conversation(dict(CONTINUE))                       # goodbye
    reply = gsm.end_conversation({comm_consts.KEY_ENDCONVERSATION_TIMESTAMP: 12.5})
    assert reply[comm_consts.KEY_REPLYTYPE] == comm_consts.KEY_REPLYTYPE_ENDCONVERSATION
    assert gsm._GameStateManager__talk is None
    assert rememberer.save_conversation_state.call_count == 1 and saved.call_count == 1
    assert rememberer.save_conversation_state.call_args.args[6] == 12.5  # the game's timestamp is kept
