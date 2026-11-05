"""Shared helpers for the conversation-lifecycle reliability regressions (tests/test_lifecycle_*.py).

Everything is offline and deterministic: the LLM is conftest's MockAIClient, TTS is a stub, audio files
are synthesized in tmp_path, and every wait that a regression could turn into a hang is bounded (daemon
thread + join timeout), so a reintroduced defect fails its test instead of stalling the suite.
See docs/conversation-reliability/FINDINGS.md for the defects these pin.
"""
import struct
import threading
from unittest.mock import MagicMock

import pytest

from src.characters_manager import Characters
from src.llm.message_thread import message_thread
from src.llm.messages import UserMessage
from src.llm.sentence import Sentence
from src.llm.sentence_queue import SentenceQueue
from src.output_manager import ChatManager

from tests.conftest import MockAIClient

DEADLINE_S = 10.0  # generous: every passing case finishes in well under a second


def within(fn, timeout_s: float = DEADLINE_S):
    """Run fn on a daemon thread; return (finished, value). A hang leaves the thread behind, not pytest."""
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:  # re-raised below
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout_s)
    if "error" in box:
        raise box["error"]
    return (not t.is_alive()), box.get("value")


def wav_bytes(seconds: float = 1.0, rate: int = 24000, channels: int = 1, bits: int = 16,
              fmt_tag: int = 1, riff_size: int | None = None, data_size: int | None = None,
              trailing: bytes = b"", extensible: bool = False) -> bytes:
    """A RIFF/WAVE file with an explicit header (the samples are silence)."""
    block_align = channels * bits // 8
    data = b"\x00" * (int(seconds * rate) * block_align)
    if extensible:
        # WAVE_FORMAT_EXTENSIBLE: cbSize 22, valid bits, channel mask, KSDATAFORMAT_SUBTYPE_PCM GUID
        fmt = struct.pack("<HHIIHHHHI16s", 0xFFFE, channels, rate, rate * block_align, block_align, bits,
                          22, bits, 0x4, bytes.fromhex("0100000000001000800000aa00389b71"))
    else:
        fmt = struct.pack("<HHIIHH", fmt_tag, channels, rate, rate * block_align, block_align, bits)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(data) if data_size is None else data_size) + data + trailing
    return b"RIFF" + struct.pack("<I", len(body) if riff_size is None else riff_size) + body


@pytest.fixture
def chat_manager_factory(default_config, monkeypatch):
    """ChatManager with a stub TTS and no OpenClaw route (the user's real openclaw config is never read)."""
    monkeypatch.delenv("MANTELLA_OPENCLAW_CONFIG", raising=False)
    monkeypatch.setattr("src.output_manager.OpenClawRouter.from_search_dirs", classmethod(lambda cls, dirs: None))
    monkeypatch.setattr("src.utils.play_error_sound", lambda: None)

    def make(audio_path: str | None = None, client=None):
        tts = MagicMock()
        tts.synthesize.return_value = (audio_path or "", False)
        return ChatManager(default_config, tts, client or MockAIClient(["Hello there."])), tts
    return make


def make_thread(config) -> message_thread:
    t = message_thread(config, "You are Guard.")
    t.add_message(UserMessage(config, "Hello", "Player"))
    return t


def start_generation(manager: ChatManager, config, characters: Characters) -> SentenceQueue:
    """What Conversation.__start_generating_npc_sentences does: flag the queue, then start the producer."""
    queue = SentenceQueue()
    queue.is_more_to_come = True
    threading.Thread(target=manager.generate_response,
                     args=(make_thread(config), characters, queue, [], None), daemon=True).start()
    return queue


def drain(queue: SentenceQueue) -> list[Sentence]:
    out = []
    while True:
        finished, s = within(queue.get_next_sentence)
        assert finished, "consumer blocked: the response was never ended (is_more_to_come stuck True)"
        if s is None:
            return out
        out.append(s)
