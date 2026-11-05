"""Repair pass 2 (R-03c): the per-synthesis token never pushes a saved voiceline name over the file-name limit.

R-03b appended ' <12-hex token>' to the readable '<voice> <first 150 characters of the text>' name. The readable
part was only bounded in characters, so a name that was valid before (e.g. 253 bytes of UTF-8 with its extension)
became 266 bytes with the token, over the 255-byte component limit of Linux file systems, and saving the line
failed (VoicelineProvenanceError -> a Text-to-Speech error instead of the line). The readable part is now cut, on a
character boundary, so the whole component (readable part + token + extension) fits 255 UTF-8 bytes; the token is
always kept whole, so names stay unique to their synthesis and an existing name is still refused.

Real TTSable.synthesize (MarkerTTS from test_lifecycle_voiceline_provenance: only the TTS service and the
lip / fuz tools are stubbed), real files in tmp_path (a 255-byte NAME_MAX file system on Linux).
"""
import os

import pytest

from src.tts.ttsable import VoicelineProvenanceError

from tests.test_lifecycle_voiceline_provenance import VOICE, setup, synth  # noqa: F401

LIMIT = 255
TOKEN_LEN = 12


def readable(text: str) -> str:
    """The readable part the base (pre-token) implementation used for this text."""
    return "".join(c for c in f'{VOICE} {text.strip()}'[:150] if c not in r'\/:*?"<>|.').strip()


def text_for_old_name_bytes(total: int) -> str:
    """A text whose base-implementation name '<readable>.wav' is exactly `total` UTF-8 bytes (2-byte characters
    plus ASCII padding, within the 150-character cut)."""
    prefix = len(f"{VOICE} ".encode())
    budget = total - len(".wav") - prefix
    chars = 150 - len(f"{VOICE} ")
    two_byte = budget - chars              # x two-byte + (chars - x) one-byte = budget
    assert 0 <= two_byte <= chars
    text = "é" * two_byte + "a" * (chars - two_byte)
    assert len((readable(text) + ".wav").encode()) == total
    return text


def saved_names(wav: str) -> list[str]:
    return [os.path.basename(wav)[:-4] + ext for ext in (".wav", ".lip", ".fuz")]


def check_name(wav: str, text: str):
    for name in saved_names(wav):
        size = len(name.encode("utf-8"))
        assert size <= LIMIT, f"{name!r} is {size} bytes"
    stem = os.path.basename(wav)[:-len(".wav")]
    kept, token = stem.rsplit(" ", 1)
    assert len(token) == TOKEN_LEN and all(c in "0123456789abcdef" for c in token)
    assert kept and readable(text).startswith(kept), "the readable prefix was not kept"
    assert kept.startswith(VOICE)
    return kept


def test_unique_token_preserves_previously_valid_utf8_filename(setup):
    tts, _, _ = setup
    text = text_for_old_name_bytes(253)    # a valid name before the token was added
    wav = synth(tts, text)                 # base pass 1: ENAMETOOLONG on rename -> VoicelineProvenanceError
    assert os.path.exists(wav)
    check_name(wav, text)


@pytest.mark.parametrize("char", ["\u00e9", "\u8a9e", "\U0001f409", "e\u0301"])   # 2, 3, 4-byte, combining pair
def test_multibyte_text_is_cut_on_a_character_boundary(setup, char):
    tts, _, _ = setup
    text = char * 200
    wav = synth(tts, text)
    assert os.path.exists(wav)
    kept = check_name(wav, text)
    kept.encode("utf-8").decode("utf-8")   # valid UTF-8: no character was split


def test_long_names_stay_unique_and_collisions_are_still_refused(setup):
    tts, _, _ = setup
    text = text_for_old_name_bytes(253)
    first, second = synth(tts, text), synth(tts, text)
    assert first != second and os.path.exists(first) and os.path.exists(second)
    tts._new_voiceline_token = lambda: os.path.basename(first)[:-4].rsplit(" ", 1)[1]
    with pytest.raises(VoicelineProvenanceError):
        synth(tts, text)                   # same token -> same (truncated) name -> refused, not overwritten


def test_short_ascii_names_are_unchanged(setup):
    """Control: an ordinary line keeps the full '<voice> <text> <token>' name."""
    tts, _, _ = setup
    wav = synth(tts, "Stay safe out there.")
    stem = os.path.basename(wav)[:-4]
    assert stem.rsplit(" ", 1)[0] == f"{VOICE} Stay safe out there"
