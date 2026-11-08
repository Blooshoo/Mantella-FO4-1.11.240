import struct
import subprocess
from pathlib import Path
import pytest
from src.tts.windows_voice_tools import WindowsVoiceTools, validate_lip, validate_fuz

@pytest.fixture
def tools(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    (source / 'LipGenerator').mkdir(parents=True)
    (source / 'LipFuzer').mkdir()
    (source / 'LipGenerator/LipGenerator.exe').write_bytes(b'fake')
    (source / 'LipFuzer/LIPFuzer.exe').write_bytes(b'fake')
    monkeypatch.setenv('MANTELLA_VOICE_WINE', '/usr/bin/true')
    monkeypatch.setenv('MANTELLA_VOICE_WORK_ROOT', str(tmp_path / 'isolated'))
    return WindowsVoiceTools(source, timeout=0.1)

@pytest.mark.parametrize('outcome', ['none', 'nonzero', 'timeout'])
def test_failure_rejects_stale_outputs(tools, tmp_path, monkeypatch, outcome):
    wav = tmp_path / 'line.wav'
    wav.write_bytes(b'wav')
    wav.with_suffix('.lip').write_bytes(b'old lip')
    wav.with_suffix('.fuz').write_bytes(b'old fuz')
    def run(*args):
        if outcome == 'nonzero':
            raise subprocess.CalledProcessError(2, ['tool'])
        if outcome == 'timeout':
            raise subprocess.TimeoutExpired(['tool'], 0.1)
    monkeypatch.setattr(tools, 'run', run)
    with pytest.raises((FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired)):
        tools.generate(wav, 'fresh text')
    assert not wav.with_suffix('.lip').exists()
    assert not wav.with_suffix('.fuz').exists()


def test_linux_dispatch_is_array_checked_bounded_translated(tools, monkeypatch, tmp_path):
    captured = {}
    def run(cmd, **kwargs):
        captured.update(cmd=cmd, **kwargs)
    monkeypatch.setattr(subprocess, 'run', run)
    text = 'quoted " text; not a shell command'
    tools.run(tools.source / 'LipGenerator/LipGenerator.exe', [tools.path(tmp_path / 'a b.wav'), text], tmp_path)
    assert isinstance(captured['cmd'], list)
    assert captured['cmd'][0] == '/usr/bin/true'
    assert captured['cmd'][-1] == text
    assert captured['cmd'][-2].startswith('Z:')
    assert captured['check'] is True and captured['timeout'] == 0.1
    assert 'creationflags' not in captured and 'shell' not in captured
    assert captured['env']['WINEPREFIX'].endswith('/isolated/disposable-voice-prefix')


def test_linux_requires_explicit_selection(tmp_path, monkeypatch):
    monkeypatch.delenv('MANTELLA_VOICE_WINE', raising=False)
    monkeypatch.delenv('MANTELLA_VOICE_WORK_ROOT', raising=False)
    with pytest.raises(RuntimeError, match='Linux voice requires'):
        WindowsVoiceTools(tmp_path)


def test_game_prefix_work_root_refused(tmp_path, monkeypatch):
    monkeypatch.setenv('MANTELLA_VOICE_WINE', '/usr/bin/true')
    monkeypatch.setenv('MANTELLA_VOICE_WORK_ROOT', str(tmp_path / 'compatdata/377160/pfx'))
    with pytest.raises(ValueError):
        WindowsVoiceTools(tmp_path)

@pytest.mark.parametrize('bad', [b'', b'FUZE', b'not a fuz file' * 10])
def test_invalid_fuz_rejected(tmp_path, bad):
    p = tmp_path / 'bad.fuz'
    p.write_bytes(bad)
    with pytest.raises(ValueError):
        validate_fuz(p, b'lip')


def test_fuz_wrong_lip_rejected(tmp_path):
    p = tmp_path / 'bad.fuz'
    p.write_bytes(b'FUZE' + struct.pack('<II', 1, 3) + b'old' + b'RIFF' + bytes(100))
    with pytest.raises(ValueError, match='this line'):
        validate_fuz(p, b'new')


def test_truncated_lip_rejected(tmp_path):
    p = tmp_path / 'bad.lip'
    p.write_bytes(b'bad')
    with pytest.raises(ValueError):
        validate_lip(p)
