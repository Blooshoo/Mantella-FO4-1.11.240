"""R-02: unreadable or streamed TTS audio can neither wedge pacing nor escape generate_sentence.

The voiceline duration paces the conversation (continue_conversation busy-waits for it on the event-loop
thread before sending the next line) and is sent to the game. utils.get_audio_duration used the wave
module, which (Python 3.11) rejects float / WAVE_FORMAT_EXTENSIBLE files, believes the header of a
streamed or truncated file (0xFFFFFFFF data size at 24 kHz mono = 89478 s, i.e. the next line waits
~24.8 h), and raised from outside generate_sentence's try, which then failed the retry path the same way
and escaped process_response's finally.
"""
import pytest

from src import utils
from src.llm.sentence_content import SentenceContent, SentenceTypeEnum

from tests.lifecycle_support import chat_manager_factory, drain, start_generation, wav_bytes, within  # noqa: F401


@pytest.mark.parametrize("name,payload,expected", [
    ("pcm16", wav_bytes(1.0), 1.0),
    ("float32", wav_bytes(1.0, bits=32, fmt_tag=3), 1.0),          # wave (py3.11): unknown format: 3
    ("extensible", wav_bytes(0.5, bits=24, extensible=True), 0.5),  # wave (py3.11): unknown format: 65534
    ("streamed", wav_bytes(1.0, riff_size=0xFFFFFFFF, data_size=0xFFFFFFFF), 1.0),  # wave: 89478.5 s
    ("streamed_zero", wav_bytes(1.0, riff_size=0, data_size=0), 1.0),
    ("truncated", wav_bytes(2.0)[: 44 + 24000 * 2], 1.0),           # header claims 2 s, 1 s present
    ("trailing_chunk", wav_bytes(1.0, trailing=b"LIST\x04\x00\x00\x00INFO"), 1.0),
    ("stereo", wav_bytes(1.0, channels=2), 1.0),
])
def test_audio_duration_counts_only_audio_present(tmp_path, name, payload, expected):
    f = tmp_path / f"{name}.wav"
    f.write_bytes(payload)
    assert utils.get_audio_duration(str(f)) == pytest.approx(expected, abs=1e-6)


@pytest.mark.parametrize("payload", [
    b"",
    b"ID3\x04" + b"\x00" * 64,                                         # an mp3 saved as .wav
    wav_bytes(1.0)[:30],                                               # header cut inside fmt
    wav_bytes(1.0).replace(b"fmt ", b"junk"),                          # data before any fmt
    wav_bytes(1.0, rate=0),
    wav_bytes(1.0, fmt_tag=0x55),                                      # compressed (MP3-in-WAV)
    b"RIFF\x24\x00\x00\x00WAVEfmt \xff\xff\xff\xff" + b"\x00" * 16,  # absurd fmt size: no huge read
])
def test_audio_duration_rejects_unusable_files(tmp_path, payload):
    f = tmp_path / "bad.wav"
    f.write_bytes(payload)
    with pytest.raises(ValueError):
        utils.get_audio_duration(str(f))


def test_audio_duration_missing_file_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        utils.get_audio_duration(str(tmp_path / "missing.wav"))


@pytest.mark.parametrize("kind", ["missing", "not_wav"])
def test_unreadable_tts_audio_becomes_an_error_sentence(chat_manager_factory, default_config, tmp_path,
                                                        example_characters_pc_to_npc, kind):
    """Like any other TTS failure: an error sentence (which ends the conversation with an error reply),
    never an exception, never a hang."""
    path = tmp_path / "out.wav"
    if kind == "not_wav":
        path.write_bytes(b"ID3\x04" + b"\x00" * 64)
    manager, _ = chat_manager_factory(str(path))
    queue = start_generation(manager, default_config, example_characters_pc_to_npc)
    sentences = drain(queue)
    assert sentences[0].error_message and "Text-to-Speech" in sentences[0].error_message
    assert str(tmp_path) not in sentences[0].error_message  # shown in-game: no local paths
    assert not queue.is_more_to_come
    finished, _ = within(manager.stop_generation)
    assert finished


def test_streamed_wav_duration_paces_by_real_length(chat_manager_factory, tmp_path, example_characters_pc_to_npc):
    wav = tmp_path / "streamed.wav"
    wav.write_bytes(wav_bytes(1.0, riff_size=0xFFFFFFFF, data_size=0xFFFFFFFF))
    manager, _ = chat_manager_factory(str(wav))
    sentence = manager.generate_sentence(SentenceContent(example_characters_pc_to_npc.last_added_character,
                                                         "Hello there.", SentenceTypeEnum.SPEECH))
    assert sentence.error_message is None
    assert sentence.voice_line_duration == pytest.approx(1.0)  # was 89478.5 s: next line waited ~24.8 h
