"""Checked Windows voice tools; Linux explicitly opts in to a Wine binary and isolated work root.
No shell, launcher scripts, game-prefix reuse, stale output or writes beside installed tools.
"""
import io
import wave
import os
import platform
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path


def validate_lip(path):
    data = Path(path).read_bytes()
    # Check the supported CK LIP version and nonempty payload, not a guessed native ABI.
    if len(data) < 12 or struct.unpack_from("<I", data)[0] != 1:
        raise ValueError("Missing or truncated LIP output")
    return data


def validate_fuz(path, lip, expected_audio=None):
    data = Path(path).read_bytes()
    if len(data) < 16 or data[:4] != b"FUZE":
        raise ValueError("Missing or invalid FUZ output")
    version, size = struct.unpack_from("<II", data, 4)
    if version != 1 or size != len(lip) or data[12:12 + size] != lip:
        raise ValueError("FUZ does not embed this line's LIP")
    audio = data[12 + size:]
    if len(audio) < 44 or audio[:4] != b"RIFF" or audio[8:12] not in (b"WAVE", b"XWMA"):
        raise ValueError("FUZ has no valid RIFF audio")
    if struct.unpack_from("<I", audio, 4)[0] + 8 != len(audio):
        raise ValueError("Truncated RIFF audio in FUZ")
    if expected_audio is not None and audio != expected_audio:
        raise ValueError("FUZ does not embed this line's WAV")
    if audio[8:12] == b"WAVE":
        with wave.open(io.BytesIO(audio), "rb") as stream:
            if stream.getnframes() <= 0 or len(stream.readframes(stream.getnframes())) != stream.getnframes() * stream.getnchannels() * stream.getsampwidth():
                raise ValueError("Invalid PCM frames in FUZ")
    return data


class WindowsVoiceTools:
    def __init__(self, lipgen, facefx=None, timeout=120):
        self.source = Path(lipgen).resolve()
        self.facefx = Path(facefx).resolve() if facefx else None
        self.timeout = timeout
        self.linux = platform.system() != "Windows"
        self.env = os.environ.copy()
        self.wine = None
        if self.linux:
            binary = os.environ.get("MANTELLA_VOICE_WINE")
            root = os.environ.get("MANTELLA_VOICE_WORK_ROOT")
            if not binary or not root:
                raise RuntimeError("Linux voice requires MANTELLA_VOICE_WINE and MANTELLA_VOICE_WORK_ROOT (isolated storage)")
            self.wine = Path(binary).resolve(strict=True)
            if not self.wine.is_file() or not os.access(self.wine, os.X_OK):
                raise ValueError("Selected Wine binary is not executable")
            self.root = Path(root).resolve()
            if "compatdata" in self.root.parts or self.root == self.source or self.source in self.root.parents:
                raise ValueError("Voice work root must not be a game/prefix/tool directory")
            self.root.mkdir(parents=True, exist_ok=True)
            self.env["WINEPREFIX"] = str(self.root / "disposable-voice-prefix")
            self.env["WINEDEBUG"] = "-all"
            self.env.pop("DISPLAY", None)
            self.env.pop("STEAM_COMPAT_DATA_PATH", None)
        else:
            self.root = Path(tempfile.gettempdir()) / "mantella-voice-tools"
            self.root.mkdir(parents=True, exist_ok=True)

    def path(self, path):
        absolute = str(Path(path).resolve())
        return "Z:" + absolute.replace("/", "\\") if self.linux else absolute

    def run(self, executable, args, cwd):
        cmd = ([str(self.wine)] if self.linux else []) + [str(executable)] + list(args)
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if not self.linux else {}
        return subprocess.run(cmd, cwd=cwd, env=self.env, timeout=self.timeout, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)

    def generate(self, wav, text, language="USEnglish", fallout=True, placeholder=None):
        wav = Path(wav).resolve(strict=True)
        lip_dest, fuz_dest = wav.with_suffix(".lip"), wav.with_suffix(".fuz")
        # Never leave stale deliverables eligible after a failed generation.
        lip_dest.unlink(missing_ok=True)
        fuz_dest.unlink(missing_ok=True)
        with tempfile.TemporaryDirectory(prefix="line-", dir=self.root) as temp:
            work = Path(temp)
            tools = work / "tools"
            shutil.copytree(self.source, tools)
            audio = work / "audio"
            audio.mkdir()
            local_wav = audio / "line.wav"
            shutil.copy2(wav, local_wav)
            local_lip = local_wav.with_suffix(".lip")
            generator = tools / "LipGenerator" / "LipGenerator.exe"
            if placeholder is not None:
                shutil.copyfile(placeholder, local_lip)
            elif generator.is_file():
                self.run(generator, [self.path(local_wav), text, "-Language:" + language, "-Automated"], audio)
            else:
                if self.facefx is None:
                    raise FileNotFoundError("Neither LipGenerator nor FaceFX available")
                fx = work / "facefx"
                shutil.copytree(self.facefx, fx)
                self.run(fx / "FaceFXWrapper.exe", ["Fallout4" if fallout else "Skyrim", language,
                    self.path(fx / "FonixData.cdf"), self.path(local_wav), self.path(audio / "line_r.wav"),
                    self.path(local_lip), text], fx)
            lip = validate_lip(local_lip)
            if fallout:
                # Actual CK filename is LIPFuzer.exe; Linux paths are case sensitive.
                fuzer = tools / "LipFuzer" / "LIPFuzer.exe"
                self.run(fuzer, ["-s", self.path(audio), "-d", self.path(audio), "-a", "wav", "--norec"], audio)
                fuz = validate_fuz(local_wav.with_suffix(".fuz"), lip, local_wav.read_bytes())
                # Atomic publication after BOTH outputs validate.
                staged = fuz_dest.with_suffix(".fuz.tmp")
                staged.write_bytes(fuz)
                staged.replace(fuz_dest)
            staged = lip_dest.with_suffix(".lip.tmp")
            staged.write_bytes(lip)
            staged.replace(lip_dest)
        return lip_dest, fuz_dest if fallout else None
