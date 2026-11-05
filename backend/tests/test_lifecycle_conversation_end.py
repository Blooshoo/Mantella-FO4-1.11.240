"""R-04: a conversation is saved (log + summary) exactly once, however many times it is ended.

GameStateManager's error path (TTS / generation / voiceline error reply) calls Conversation.end() but keeps
the conversation; the official FO4 scripts then clean up locally without an end request, so the next
start_conversation ended it again - and an end request after the error ends it again too. Each end()
appended the whole conversation to every NPC's history file and re-ran the summary (an LLM call).
"""
from unittest.mock import MagicMock

import pytest

from src.conversation.conversation import Conversation
from src.game_manager import GameStateManager


@pytest.fixture
def saved_logs(monkeypatch):
    saved = MagicMock()
    monkeypatch.setattr("src.conversation.conversation.conversation_log.save_conversation_log", saved)
    return saved


def _conversation(config, npc):
    config.conversation_summary_enabled = True
    context = MagicMock()
    context.config = config
    context.npcs_in_conversation.contains_player_character.return_value = True
    context.npcs_in_conversation.get_non_player_characters.return_value = [npc]
    rememberer = MagicMock()
    return Conversation(context, MagicMock(), rememberer, MagicMock(), None, False, False), rememberer


def test_conversation_end_is_idempotent(default_config, saved_logs, example_skyrim_npc_character):
    conversation, rememberer = _conversation(default_config, example_skyrim_npc_character)
    conversation.end()
    conversation.end()
    conversation.end(end_timestamp=3.0)
    assert saved_logs.call_count == 1
    assert rememberer.save_conversation_state.call_count == 1
    assert conversation.has_already_ended


def test_every_end_still_stops_generation(default_config, saved_logs, example_skyrim_npc_character):
    """Only the save is deduplicated: a repeated end() must still stop a generation that was started after
    the first one (e.g. by a player input that reached the ended conversation)."""
    conversation, _ = _conversation(default_config, example_skyrim_npc_character)
    output_manager = conversation.output_manager
    conversation.end()
    conversation.end()
    assert output_manager.stop_generation.call_count == 2


def test_end_request_after_error_path_does_not_save_again(default_config, saved_logs, english_language_info,
                                                          example_skyrim_npc_character):
    conversation, rememberer = _conversation(default_config, example_skyrim_npc_character)
    gsm = GameStateManager(MagicMock(), MagicMock(), default_config, english_language_info, MagicMock())
    gsm._GameStateManager__talk = conversation
    conversation.end()              # what continue_conversation's error path does (conversation kept)
    gsm.end_conversation({})        # the game's end request afterwards
    assert saved_logs.call_count == 1
    assert rememberer.save_conversation_state.call_count == 1
