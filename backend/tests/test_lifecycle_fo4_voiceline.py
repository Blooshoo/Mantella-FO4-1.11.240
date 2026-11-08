"""R-03: Fallout 4 never voices a previous line under a new subtitle.

The game plays whatever file sits in the topic's slot, Sound/Voice/Mantella.esp/MantellaVoice00/
<topic info>_1.fuz (two slots, alternating). prepare_sentence_for_game only copied a new .fuz there when
it existed and merely warned otherwise; a sentence without a usable .fuz (TTS skipped a too-short line,
LipFuzer/FaceFX failed - TTSable._generate_voiceline_files only logs that - or the copy failed) left the
PREVIOUS line in the slot, and the reply still told the game to play it. TTSable.synthesize also kept a
leftover out.fuz that a later failed fuz generation then renamed to the new line's name.
"""
import os
from unittest.mock import MagicMock

import pytest

from src.game_manager import GameStateManager
from src.games.fallout4 import Fallout4
from src.http.communication_constants import communication_constants as comm_consts
from src.llm.sentence import Sentence
from src.llm.sentence_content import SentenceContent, SentenceTypeEnum

from tests.lifecycle_support import wav_bytes


@pytest.fixture
def fo4(tmp_path, default_config):
    default_config.mod_path_base = str(tmp_path)
    default_config.save_folder = str(tmp_path)
    default_config.save_audio_data_to_character_folder = False
    default_config.mod_path = str(tmp_path / "Sound" / "Voice" / "Mantella.esp")
    return Fallout4(default_config)


def _slot(config, game: Fallout4, topic: int = 1) -> str:
    name = game.DIALOGUELINE2_FILENAME if topic == 2 else game.DIALOGUELINE1_FILENAME
    return os.path.join(config.mod_path, game.MANTELLA_VOICE_FOLDER, f"{name}.fuz")


def _seed_stale_slot(config, game: Fallout4, topic: int = 1) -> str:
    slot = _slot(config, game, topic)
    os.makedirs(os.path.dirname(slot), exist_ok=True)
    with open(slot, "wb") as f:
        f.write(b"OLD LINE")
    return slot


def _sentence(npc, voice_file: str, text: str = "Hello there.") -> Sentence:
    return Sentence(SentenceContent(npc, text, SentenceTypeEnum.SPEECH), voice_file, 1.0)


def test_new_voiceline_replaces_the_slot(fo4, default_config, tmp_path, example_fallout4_npc_character):
    slot = _seed_stale_slot(default_config, fo4, topic=2)
    wav = tmp_path / "save" / "line.wav"
    wav.parent.mkdir()
    wav.write_bytes(wav_bytes(0.1))
    wav.with_suffix(".fuz").write_bytes(b"NEW LINE")
    fo4.prepare_sentence_for_game(_sentence(example_fallout4_npc_character, str(wav)), None, default_config, 2, False)
    with open(slot, "rb") as f:
        assert f.read() == b"NEW LINE"


@pytest.mark.parametrize("case", ["fuz_missing", "tts_skipped_short_line", "wav_missing", "copy_fails"])
def test_without_a_new_fuz_the_slot_never_keeps_the_old_line(fo4, default_config, tmp_path, monkeypatch,
                                                             example_fallout4_npc_character, case):
    slot = _seed_stale_slot(default_config, fo4)
    wav = tmp_path / "save" / "line.wav"
    wav.parent.mkdir()
    voice_file = str(wav)
    if case == "fuz_missing":               # LipFuzer / FaceFX / xWMAEncode failed: only a warning upstream
        wav.write_bytes(wav_bytes(0.1))
    elif case == "tts_skipped_short_line":  # generate_sentence skips TTS below 3 characters: voice_file ""
        voice_file = ""
    elif case == "copy_fails":
        wav.write_bytes(wav_bytes(0.1))
        wav.with_suffix(".fuz").write_bytes(b"NEW LINE")

        def broken_copy(*a, **kw):
            raise OSError("disk full")
        monkeypatch.setattr("src.games.fallout4.shutil.copyfile", broken_copy)
    fo4.prepare_sentence_for_game(_sentence(example_fallout4_npc_character, voice_file), None, default_config, 1, False)
    assert not os.path.exists(slot) or open(slot, "rb").read() != b"OLD LINE", \
        "the game would voice the previous line under this sentence's subtitle"


def test_slot_that_cannot_be_cleared_is_refused(fo4, default_config, tmp_path, monkeypatch,
                                                example_fallout4_npc_character):
    from src.games.gameable import VoicelinePreparationError
    slot = _seed_stale_slot(default_config, fo4)
    wav = tmp_path / "save" / "line.wav"
    wav.parent.mkdir()
    wav.write_bytes(wav_bytes(0.1))  # no .fuz: the slot has to be cleared

    real_remove = os.remove

    def locked(path, *a, **kw):
        if os.path.normpath(path) == os.path.normpath(slot):
            raise PermissionError("in use by Fallout4.exe")
        return real_remove(path, *a, **kw)
    with monkeypatch.context() as m:
        m.setattr(os, "remove", locked)
        with pytest.raises(VoicelinePreparationError):
            fo4.prepare_sentence_for_game(_sentence(example_fallout4_npc_character, str(wav)), None, default_config, 1, False)
    with open(slot, "rb") as f:
        assert f.read() == b"OLD LINE"  # untouched - and reported instead of played


@pytest.mark.parametrize("request_type", ["continue", "player_input"])
def test_refusal_becomes_an_error_reply(default_config, english_language_info, example_fallout4_npc_character,
                                        request_type):
    from src.games.gameable import VoicelinePreparationError
    game = MagicMock()
    game.prepare_sentence_for_game.side_effect = VoicelinePreparationError("slot locked")
    game.modify_sentence_text_for_game.side_effect = lambda t: t
    gsm = GameStateManager(game, MagicMock(), default_config, english_language_info, MagicMock())
    talk = MagicMock()
    line = _sentence(example_fallout4_npc_character, "x.wav")
    talk.resume_after_interrupting_action.return_value = False
    talk.continue_conversation.return_value = (comm_consts.KEY_REPLYTYPE_NPCTALK, line)
    talk.process_player_input.return_value = ("Hi there", False, line)  # a voiced player line
    gsm._GameStateManager__talk = talk
    if request_type == "continue":
        reply = gsm.continue_conversation({comm_consts.KEY_REQUESTTYPE: comm_consts.KEY_REQUESTTYPE_CONTINUECONVERSATION,
                                           comm_consts.KEY_CONTINUECONVERSATION_TOPICINFOFILE: 1})
    else:
        reply = gsm.player_input({comm_consts.KEY_REQUESTTYPE: comm_consts.KEY_REQUESTTYPE_PLAYERINPUT,
                                  comm_consts.KEY_REQUESTTYPE_PLAYERINPUT: "Hi there"})
    assert reply[comm_consts.KEY_REPLYTYPE] == "error"
    assert "slot locked" in reply["mantella_message"]
    talk.end.assert_called_once()


def test_synthesize_never_reuses_a_leftover_fuz(default_config, tmp_path, monkeypatch):
    """TTSable.synthesize cleared out.wav/out.lip but not out.fuz, so when this line's lip/fuz generation
    failed, a fuz left over from an earlier line was renamed to this line's name and sent to the game."""
    from src.tts.ttsable import TTSable
    from src.tts.synthesization_options import SynthesizationOptions
    from src.config.definitions.game_definitions import GameEnum

    for var in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(var, str(tmp_path))
    default_config.game = GameEnum.FALLOUT4
    default_config.lip_generation = "disabled"  # FO4: placeholder lip + fuz, both unavailable here
    default_config.lipgen_path = str(tmp_path / "no-lipgen")
    default_config.facefx_path = str(tmp_path / "no-facefx")

    class StubTTS(TTSable):
        def change_voice(self, *a, **kw):
            self._last_voice = "maleboston"

        def _synthesize_voiceline(self, voiceline, final_voiceline_file, synth_options):
            with open(final_voiceline_file, "wb") as f:
                f.write(wav_bytes(0.1))

    tts = StubTTS(default_config)
    leftover = os.path.join(tts._voiceline_folder, "out.fuz")
    with open(leftover, "wb") as f:
        f.write(b"OLD LINE")  # an earlier rename failed, or the previous run was interrupted
    from src.tts.ttsable import VoicelineProvenanceError
    # Checked platform tools now fail the line closed rather than return a WAV with no valid FUZ.
    with pytest.raises(VoicelineProvenanceError):
        tts.synthesize("maleboston", " Hello there. ", "MaleBoston", "MaleBoston", "en",
                       SynthesizationOptions(False, False))
    assert not os.path.exists(leftover)
