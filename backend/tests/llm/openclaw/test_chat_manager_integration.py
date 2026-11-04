"""ChatManager-level integration: the OpenClaw route feeds Mantella's real sentence/TTS pipeline.

Uses upstream conftest fixtures; tests are sync and drive the coroutine with asyncio.run so
they do not need pytest-asyncio.
"""
import asyncio
import pytest
from unittest.mock import MagicMock

from src.output_manager import ChatManager
from src.llm.sentence_queue import SentenceQueue
from src.llm.message_thread import message_thread
from src.llm.openclaw.config import OpenClawCompanionConfig
from src.llm.openclaw.router import OpenClawRouter
from src.llm.openclaw.transport import MockTransport
from src.llm.messages import UserMessage


def _drain(queue: SentenceQueue):
    out = []
    while True:
        s = queue.get_next_sentence()
        if not s:
            break
        out.append(s)
    return out


@pytest.fixture
def transport():
    return MockTransport(reply_chunks=["Right ", "behind ", "you. ", "Let's move."])


@pytest.fixture
def make_manager(default_config, piper, mock_ai_client, monkeypatch, transport):
    piper.synthesize = MagicMock(return_value=("mock_audio_file.wav", False))
    monkeypatch.setattr('src.utils.get_audio_duration', lambda *a, **kw: 1.0)

    def _make(characters=("Guard",), enabled=True):
        cfg = OpenClawCompanionConfig(enabled=enabled, transport="mock", characters=tuple(characters),
                                      retry_backoff_s=0.0)
        router = OpenClawRouter(cfg, transport_factory=lambda c: transport)
        return ChatManager(default_config, piper, mock_ai_client, openclaw_router=router)
    return _make


def _thread(default_config):
    t = message_thread(default_config, "You are Guard, a Whiterun guard.")
    t.add_message(UserMessage(default_config, "Hello there", "Player"))
    return t


def test_opted_in_character_speaks_through_openclaw(make_manager, transport, default_config,
                                                    example_skyrim_npc_character, example_characters_pc_to_npc,
                                                    mock_ai_client):
    manager = make_manager()
    queue = SentenceQueue()
    asyncio.run(manager.process_response(example_skyrim_npc_character, queue, _thread(default_config),
                                         example_characters_pc_to_npc, [], tools=None))
    text = " ".join(s.content.text.strip() for s in _drain(queue) if s.content.text.strip())
    assert "Right behind you." in text
    assert "Let's move." in text
    assert len(transport.requests) == 1
    assert mock_ai_client.call_count == 0          # direct provider not used
    sent = transport.requests[0].user_content
    assert "UNTRUSTED game data" in sent and "Hello there" in sent


def test_other_npcs_keep_direct_provider(make_manager, transport, default_config,
                                         example_skyrim_npc_character, example_characters_pc_to_npc,
                                         mock_ai_client):
    manager = make_manager(characters=("Kalli",))
    queue = SentenceQueue()
    asyncio.run(manager.process_response(example_skyrim_npc_character, queue, _thread(default_config),
                                         example_characters_pc_to_npc, [], tools=None))
    assert transport.requests == []
    assert mock_ai_client.call_count == 1


def test_conversation_start_rotates_openclaw_session(make_manager, transport, default_config,
                                                     example_skyrim_npc_character, example_characters_pc_to_npc):
    manager = make_manager()
    for _ in range(2):
        manager.clear_per_character_client_cache()   # what GameStateManager does on conversation start
        asyncio.run(manager.process_response(example_skyrim_npc_character, SentenceQueue(),
                                             _thread(default_config), example_characters_pc_to_npc, [],
                                             tools=None))
    keys = [r.session_key for r in transport.requests]
    assert len(keys) == 2 and keys[0] != keys[1]


def test_gateway_down_falls_back_to_direct_voice(make_manager, transport, default_config,
                                                 example_skyrim_npc_character, example_characters_pc_to_npc,
                                                 mock_ai_client):
    transport.fail_first = 99
    manager = make_manager()
    queue = SentenceQueue()
    asyncio.run(manager.process_response(example_skyrim_npc_character, queue, _thread(default_config),
                                         example_characters_pc_to_npc, [], tools=None))
    text = " ".join(s.content.text.strip() for s in _drain(queue))
    assert "Hello there." in text                    # MockAIClient's default direct reply
    assert mock_ai_client.call_count == 1


def test_invalid_config_file_does_not_break_chat_manager(default_config, piper, mock_ai_client, tmp_path,
                                                         monkeypatch):
    bad = tmp_path / "openclaw_companion.json"
    bad.write_text('{"enabled": true, "token": "nope"}')
    monkeypatch.setenv("MANTELLA_OPENCLAW_CONFIG", str(bad))
    manager = ChatManager(default_config, piper, mock_ai_client)
    assert manager._ChatManager__openclaw_router is None
