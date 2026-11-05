"""R-03b: a generated voiceline is only ever played for the synthesis that produced it.

Review of R-03 (cfcc5f0) found two ways stale audio still reached the Fallout 4 slot:

* Saved-file name collisions across requests. TTSable.synthesize saved each line as
  voicelines/save/<voice> <first 150 characters of the text>.wav/.lip/.fuz. Two syntheses with the same voice and
  the same (sanitized, truncated) text share those names, so
  - a later line whose .fuz generation failed (LipFuzer / FaceFX / xWMAEncode only log failures) kept the
    EARLIER line's .fuz under its own name, and Fallout4.prepare_sentence_for_game copied it into the slot;
  - a later line overwrote a queued line's files before the game asked for it (sentences are synthesized ahead
    of playback), so a different text sharing a 150-character prefix was voiced under the queued subtitle;
  - when the earlier saved .fuz could not be unlinked, the bare except only logged, left the new out.fuz
    unrenamed, and the earlier .fuz was played.
* Invalidation failures of the out.* intermediates were only logged ("Failed to remove spoken voicelines"), and
  out.xwm (xWMAEncode output, packed by Fuz_extractor) was never invalidated at all, so an intermediate from an
  earlier line was taken for this line's output.

Every saved name is now unique to its synthesis; an intermediate that cannot be invalidated, or a wav that cannot
be saved under its own name, fails the synthesis closed (a Text-to-Speech error for that line, no fallback to an
older file). The stub TTS and lip/fuz tools below write marker bytes so each test can tell whose audio reached the
slot; the real synthesize / prepare_sentence_for_game code runs unmodified.
"""
import os
from unittest.mock import MagicMock

import pytest

from src.config.definitions.game_definitions import GameEnum
from src.games.fallout4 import Fallout4
from src.llm.sentence import Sentence
from src.llm.sentence_content import SentenceContent, SentenceTypeEnum
from src.tts.synthesization_options import SynthesizationOptions
from src.tts.ttsable import TTSable

from tests.lifecycle_support import wav_bytes

VOICE = "maleboston"


class MarkerTTS(TTSable):
    """TTSable with the external tools replaced by marker writers.

    _synthesize_voiceline plays the TTS service (writes out.wav); _generate_voiceline_files plays the lip / fuz
    tool chain: xWMAEncode writes out.xwm, then Fuz_extractor packs whatever out.xwm is present into out.fuz.
    Each step can be told to fail silently, as the real tools do (they only log)."""

    def __init__(self, config):
        super().__init__(config)
        self.n = 0
        self.tts_writes = True
        self.xwm_writes = True
        self.fuz_writes = True

    def change_voice(self, *a, **kw):
        self._last_voice = VOICE

    def _synthesize_voiceline(self, voiceline, final_voiceline_file, synth_options):
        self.n += 1
        if self.tts_writes:
            with open(final_voiceline_file, "wb") as f:
                f.write(wav_bytes(0.05, trailing=f"WAV#{self.n}".encode()))

    def _generate_voiceline_files(self, wav_file, voiceline, skip_lip_generation=False):
        base = wav_file[:-len(".wav")]
        with open(base + ".lip", "wb") as f:
            f.write(f"LIP#{self.n}".encode())
        if self.xwm_writes:
            with open(base + ".xwm", "wb") as f:
                f.write(f"AUDIO#{self.n}".encode())
        if self.fuz_writes and os.path.exists(base + ".xwm"):
            with open(base + ".xwm", "rb") as src, open(base + ".fuz", "wb") as f:
                f.write(b"FUZ:" + src.read())


@pytest.fixture
def setup(default_config, tmp_path, monkeypatch):
    for var in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(var, str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    default_config.game = GameEnum.FALLOUT4
    default_config.lip_generation = "enabled"
    default_config.mod_path_base = str(tmp_path)
    default_config.save_folder = str(tmp_path)
    default_config.save_audio_data_to_character_folder = False
    default_config.mod_path = str(tmp_path / "Sound" / "Voice" / "Mantella.esp")
    return MarkerTTS(default_config), Fallout4(default_config), default_config


def synth(tts: MarkerTTS, text: str) -> str:
    wav, _ = tts.synthesize(VOICE, f" {text} ", "MaleBoston", "MaleBoston", "en", SynthesizationOptions(False, False))
    return wav


def play(game: Fallout4, config, npc, wav: str, text: str, topic: int = 1) -> bytes | None:
    """What the game's next line request does with this sentence; returns the slot's bytes (None: no audio)."""
    game.prepare_sentence_for_game(Sentence(SentenceContent(npc, text, SentenceTypeEnum.SPEECH), wav, 1.0),
                                   None, config, topic, False)
    name = game.DIALOGUELINE2_FILENAME if topic == 2 else game.DIALOGUELINE1_FILENAME
    slot = os.path.join(config.mod_path, game.MANTELLA_VOICE_FOLDER, f"{name}.fuz")
    if not os.path.exists(slot):
        return None
    with open(slot, "rb") as f:
        return f.read()


def failed_closed(fn) -> bool:
    try:
        fn()
    except Exception:
        return True
    return False


# --- name collisions across requests -------------------------------------------------------------------------

def test_failed_fuz_never_inherits_an_earlier_lines_saved_fuz(setup, example_fallout4_npc_character):
    tts, game, config = setup
    first = synth(tts, "Stay safe out there.")                  # request A: full tool chain works
    tts.fuz_writes = False                                     # request B, same voice + text: fuz generation fails
    second = synth(tts, "Stay safe out there.")
    slot = play(game, config, example_fallout4_npc_character, second, "Stay safe out there.")
    assert slot != b"FUZ:AUDIO#1", "the game voiced request A's synthesis for request B"
    assert slot is None                                        # fail closed: this line has no audio
    assert os.path.exists(first.replace(".wav", ".fuz"))      # A's own files are untouched


def test_queued_line_keeps_its_own_audio_when_a_later_line_shares_its_name(setup, example_fallout4_npc_character):
    """Sentences are synthesized ahead of playback: line 2 is ready before the game asks for line 1."""
    tts, game, config = setup
    prefix = "The Commonwealth was not always like this, you know, " * 3   # > 150 characters
    one, two = prefix + "and it never will be again.", prefix + "but maybe someday it will be."
    wav1 = synth(tts, one)
    wav2 = synth(tts, two)
    assert play(game, config, example_fallout4_npc_character, wav1, one, topic=1) == b"FUZ:AUDIO#1", \
        "line 1 was voiced with line 2's audio"
    assert play(game, config, example_fallout4_npc_character, wav2, two, topic=2) == b"FUZ:AUDIO#2"


def test_undeletable_earlier_saved_fuz_is_never_played(setup, example_fallout4_npc_character, monkeypatch):
    tts, game, config = setup
    first = synth(tts, "Hold it right there.")
    earlier_fuz = os.path.normpath(first.replace(".wav", ".fuz"))
    real_remove = os.remove

    def locked(path, *a, **kw):
        if os.path.normpath(path) == earlier_fuz:
            raise PermissionError("in use")
        return real_remove(path, *a, **kw)
    monkeypatch.setattr(os, "remove", locked)
    second = synth(tts, "Hold it right there.")
    slot = play(game, config, example_fallout4_npc_character, second, "Hold it right there.")
    assert slot == b"FUZ:AUDIO#2", f"expected this synthesis' audio, the slot holds {slot!r}"


def test_same_turn_repeated_line_still_plays(setup, example_fallout4_npc_character):
    """Control (passes before and after): a line repeated within one response keeps valid audio for both
    occurrences - provenance checks must not turn valid playback into silence."""
    tts, game, config = setup
    a = synth(tts, "Yeah.")
    b = synth(tts, "Yeah.")
    for wav, topic in ((a, 1), (b, 2)):
        slot = play(game, config, example_fallout4_npc_character, wav, "Yeah.", topic)
        assert slot is not None and slot.startswith(b"FUZ:AUDIO#")


# --- intermediates left by an earlier synthesis --------------------------------------------------------------

def _leave(tts: MarkerTTS, suffix: str, marker: bytes) -> str:
    path = os.path.join(tts._voiceline_folder, "out" + suffix)
    with open(path, "wb") as f:
        f.write(marker)
    return path


@pytest.mark.parametrize("suffix", [".wav", ".fuz", ".xwm"])
def test_leftover_intermediate_is_never_taken_for_this_lines_output(setup, example_fallout4_npc_character, suffix):
    """Deletable leftovers (interrupted run, failed rename). out.wav / out.fuz were already handled by R-03;
    out.xwm was not invalidated, and Fuz_extractor packed it into this line's .fuz."""
    tts, game, config = setup
    stale = wav_bytes(0.05, trailing=b"STALE") if suffix == ".wav" else b"STALE"
    _leave(tts, suffix, stale)
    # this synthesis' step that would have overwritten the leftover fails silently
    tts.tts_writes = suffix != ".wav"
    tts.xwm_writes = suffix != ".xwm"
    tts.fuz_writes = suffix != ".fuz"
    result = {}
    if failed_closed(lambda: result.setdefault("wav", synth(tts, "Need something?"))):
        return
    slot = play(game, config, example_fallout4_npc_character, result["wav"], "Need something?")
    assert slot is None or b"STALE" not in slot, "an earlier line's audio was packed into / renamed to this line"


@pytest.mark.parametrize("suffix", [".wav", ".fuz", ".xwm"])
def test_undeletable_intermediate_fails_closed(setup, example_fallout4_npc_character, monkeypatch, suffix):
    """The leftover cannot be invalidated (locked). Before: logged and ignored, then used as this line's output."""
    tts, game, config = setup
    stale = wav_bytes(0.05, trailing=b"STALE") if suffix == ".wav" else b"STALE"
    leftover = os.path.normpath(_leave(tts, suffix, stale))
    real_remove = os.remove

    def locked(path, *a, **kw):
        if os.path.normpath(path) == leftover:
            raise PermissionError("in use")
        return real_remove(path, *a, **kw)
    monkeypatch.setattr(os, "remove", locked)
    tts.tts_writes = suffix != ".wav"
    tts.xwm_writes = suffix != ".xwm"
    tts.fuz_writes = suffix != ".fuz"
    result = {}
    if failed_closed(lambda: result.setdefault("wav", synth(tts, "Need something?"))):
        return
    slot = play(game, config, example_fallout4_npc_character, result["wav"], "Need something?")
    assert slot is None or b"STALE" not in slot, "an un-invalidated leftover was played for this line"
    with open(result["wav"], "rb") as f:
        assert b"STALE" not in f.read(), "an un-invalidated out.wav was returned as this line's audio"


def test_provenance_failure_becomes_a_tts_error_without_fallback(setup, monkeypatch, example_fallout4_npc_character):
    """Through ChatManager.generate_sentence: the line carries an error and no voice file, so GameStateManager
    answers with an error reply; nothing older is substituted."""
    from src.output_manager import ChatManager
    from tests.conftest import MockAIClient
    tts, game, config = setup
    monkeypatch.setattr("src.utils.play_error_sound", lambda: None)
    monkeypatch.setattr("src.output_manager.OpenClawRouter.from_search_dirs", classmethod(lambda cls, dirs: None))
    leftover = os.path.normpath(_leave(tts, ".fuz", b"STALE"))
    real_remove = os.remove

    def locked(path, *a, **kw):
        if os.path.normpath(path) == leftover:
            raise PermissionError("in use")
        return real_remove(path, *a, **kw)
    monkeypatch.setattr(os, "remove", locked)
    manager = ChatManager(config, tts, MockAIClient(["x"]))
    sentence = manager.generate_sentence(SentenceContent(example_fallout4_npc_character, "Need something?",
                                                         SentenceTypeEnum.SPEECH))
    assert sentence.error_message and "Text-to-Speech Error" in sentence.error_message
    assert sentence.voice_file == ""


def test_wav_that_cannot_be_saved_is_not_returned(setup, monkeypatch):
    """Control (fails closed before and after; before only indirectly, via an unreadable path): a failed rename
    never yields out.wav (removed by the next synthesis) or an earlier file of the same name."""
    tts, _, _ = setup
    synth(tts, "Hello there.")
    real_rename = os.rename

    def broken(src, dst, *a, **kw):
        if os.path.basename(src) == "out.wav":
            raise PermissionError("locked")
        return real_rename(src, dst, *a, **kw)
    monkeypatch.setattr(os, "rename", broken)
    result = {}
    if failed_closed(lambda: result.setdefault("wav", synth(tts, "Hello there."))):
        return
    assert not os.path.exists(result["wav"]), "a file that is not this synthesis' output was returned"
