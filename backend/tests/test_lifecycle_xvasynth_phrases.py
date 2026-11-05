"""Repair pass 2 (R-03d / O-11): a multi-phrase xVASynth line is only ever built from THIS synthesis' phrase audio.

xVASynth splits a long line into phrases, has the server synthesize each into its own file (one batch request for
non-xVAPitch models) and merges them into out.wav. The phrase files were named by their text in the voiceline
folder and never removed, the batch response was never checked, and _merge_audio_files only logged a missing or
unreadable phrase and wrote out.wav from whatever it could read. So after a batch request failed, the phrase files
an EARLIER synthesis of the same text had left were merged into this line's fresh-token out.wav, and a batch that
produced only some phrases was published with an older file in place of the missing ones.

Phrase files are now named by a token of the synthesis, a rejected batch response raises, the merge writes out.wav
once and only from a complete set of readable phrases (otherwise IncompletePhraseSynthesis -> a Text-to-Speech
error for the line), and the phrase files are removed afterwards.

The real xVASynth constructor, change_voice, _split_voiceline, _batch_synthesize, _merge_audio_files and
TTSable.synthesize run; only the network (requests.get / requests.post: a fake xVASynth server that writes the
phrase wavs it is asked for), the server process (Popen) and the voice-model json on disk are provided.
"""
import json
import os

import numpy as np
import pytest
import soundfile as sf

from src.config.definitions.game_definitions import GameEnum
from src.tts.synthesization_options import SynthesizationOptions
from src.tts import xvasynth as xvasynth_module
from src.tts.xvasynth import xVASynth

RATE = 22050
# > 150 characters, so _split_voiceline returns several phrases
LINE = ("I have walked these roads since before the war, and I have seen what the raiders do to travellers "
        "who stop for the night, so keep your weapon close and your fire small, or you will not see the morning.")


class Response:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise xvasynth_module.requests.exceptions.HTTPError(str(self.status_code))


class FakeXVASynthServer:
    """requests.get / requests.post for 127.0.0.1:8008. The batch endpoint writes each requested phrase file
    (a constant-valued wav whose value identifies the request) unless told to fail or to skip phrases."""

    def __init__(self):
        self.request_no = 0
        self.batch_status = 200
        self.write_phrases: set[int] | None = None   # None: all
        self.batches: list[list] = []

    def get(self, url, *a, **kw):
        return Response(200)

    def post(self, url, json=None, *a, **kw):
        if url.endswith("/synthesize_batch"):
            self.request_no += 1
            self.batches.append(json["linesBatch"])
            for i, line in enumerate(json["linesBatch"]):
                if self.batch_status == 200 and (self.write_phrases is None or i in self.write_phrases):
                    sf.write(line[4], np.full(1000 * (i + 1), self.request_no / 10.0), RATE)
            return Response(self.batch_status, "" if self.batch_status == 200 else "Internal Server Error")
        if url.endswith("/synthesize"):
            self.request_no += 1
            sf.write(json["outfile"], np.full(500, self.request_no / 10.0), RATE)
        return Response(200)


@pytest.fixture
def xva(default_config, tmp_path, monkeypatch):
    for var in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(var, str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    default_config.game = GameEnum.SKYRIM
    default_config.lip_generation = "disabled"       # no LipGen / FaceFX subprocess for Skyrim
    default_config.xvasynth_path = str(tmp_path / "xVASynth")
    default_config.facefx_path = str(tmp_path / "facefx")
    models = tmp_path / "xVASynth" / "resources" / "app" / "models" / GameEnum.SKYRIM.display_name
    models.mkdir(parents=True)
    (models / "sk_malenord.json").write_text(json.dumps({"modelType": "FastPitch1.1", "modelVersion": 2.0,
                                                         "games": [{"base_speaker_emb": [0.1, 0.2]}]}))
    server = FakeXVASynthServer()
    monkeypatch.setattr(xvasynth_module.requests, "get", server.get)
    monkeypatch.setattr(xvasynth_module.requests, "post", server.post)

    def no_server_process(*a, **kw):
        raise AssertionError("the xVASynth server process must not be started")
    monkeypatch.setattr(xvasynth_module, "Popen", no_server_process)
    return xVASynth(default_config), server


def synth(tts: xVASynth) -> str:
    wav, _ = tts.synthesize("MaleNord", LINE, "MaleNord", "MaleNord", "en", SynthesizationOptions(False, False))
    return wav


def saved(tts) -> list[str]:
    return sorted(os.listdir(f"{tts._voiceline_folder}/save"))


def phrase_leftovers(tts) -> list[str]:
    return [n for n in os.listdir(tts._voiceline_folder) if n.endswith(".wav") and n != "out.wav"]


def test_split_produces_several_phrases(xva):
    tts, _ = xva
    assert len(tts._split_voiceline(LINE)) >= 2


def test_successful_batch_merges_every_phrase_of_this_request(xva):
    """Control (passes before and after): a complete batch is merged in order into the saved line."""
    tts, server = xva
    wav = synth(tts)
    audio, rate = sf.read(wav)
    phrases = len(server.batches[-1])
    assert rate == RATE and len(audio) == sum(1000 * (i + 1) for i in range(phrases))
    assert np.allclose(audio, server.request_no / 10.0, atol=1e-3)


def test_phrase_files_do_not_outlive_their_synthesis(xva):
    """The leftover phrase files were the stale source; they are now removed (and could not be reused anyway)."""
    tts, _ = xva
    synth(tts)
    assert phrase_leftovers(tts) == []


@pytest.mark.parametrize("failure", ["http_500", "partial_phrases"])
def test_xvasynth_batch_failure_cannot_publish_previous_phrase_audio(xva, failure):
    tts, server = xva
    first = synth(tts)                                   # request 1 succeeds (phrase audio value 0.1)
    before = saved(tts)
    if failure == "http_500":
        server.batch_status = 500                        # request 2: rejected, nothing written
    else:
        server.write_phrases = {0}                       # request 2: only the first phrase written
    with pytest.raises(Exception):
        wav = synth(tts)
        audio, _ = sf.read(wav)
        old = np.isclose(audio, 0.1, atol=1e-3)
        pytest.fail(f"published a line built from the earlier request's phrase audio "
                    f"({int(old.sum())} of {len(audio)} samples from request 1)")
    assert saved(tts) == before, "a voiceline was saved for the failed synthesis"
    assert sf.read(first)[0].size > 0                    # the earlier line itself is untouched
    assert phrase_leftovers(tts) == []


def test_unreadable_phrase_fails_closed(xva, monkeypatch):
    """A phrase file that exists but cannot be read is not skipped: the line fails rather than being merged from
    the other phrases."""
    tts, server = xva
    real_write = sf.write

    def corrupt_second_phrase(path, data, rate, *a, **kw):
        if str(path).endswith(" 01.wav"):
            with open(path, "wb") as f:
                f.write(b"RIFF\x00not a wav")
            return
        real_write(path, data, rate, *a, **kw)
    monkeypatch.setattr(sf, "write", corrupt_second_phrase)
    with pytest.raises(xvasynth_module.IncompletePhraseSynthesis):
        synth(tts)
    assert saved(tts) == []
