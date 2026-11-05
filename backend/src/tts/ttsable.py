from abc import ABC, abstractmethod
from src.config.config_loader import ConfigLoader
import src.utils as utils
import os
from pathlib import Path
from subprocess import DEVNULL
import subprocess
from src.tts.synthesization_options import SynthesizationOptions
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import shutil
import soundfile as sf
import numpy as np
import time
from queue import Queue
from threading import Thread, Event
from src.config.definitions.game_definitions import GameEnum
import platform
import uuid

if platform.system() == "Windows":
    from subprocess import STARTUPINFO, STARTF_USESHOWWINDOW

logger = utils.get_logger()


class VoicelineProvenanceError(Exception):
    """A synthesized voiceline could not be proven to belong to the line it was synthesized for: an intermediate
    left by an earlier synthesis could not be invalidated, or the result could not be saved under its own name.
    The synthesis fails (a Text-to-Speech error for that line) rather than risk the game voicing an earlier line."""


class TTSable(ABC):
    """Base class for different TTS services
    """
    supports_streaming: bool = False # whether the service can stream first-line audio for Streamed Fast Response

    @utils.time_it
    def __init__(self, config: ConfigLoader) -> None:
        super().__init__()
        self._config: ConfigLoader = config
        self._loglevel = 29
        self._lipgen_path = config.lipgen_path
        self.__has_lipgen_warning_happened = False
        self._facefx_path = config.facefx_path
        self._times_checked = 0
        self._tts_print = config.tts_print # to print output to console
        self._save_folder = config.save_folder
        self._output_path = utils.get_tmp_dir()
        self._voiceline_folder = f"{self._output_path}/voicelines"
        os.makedirs(f"{self._voiceline_folder}/save", exist_ok=True)
        self._language = config.language
        self._last_voice = '' # last active voice model
        self._lip_generation_enabled = config.lip_generation
        # determines whether the voiceline should play internally
        #self.debug_mode = config.debug_mode
        #self.play_audio_from_script = config.play_audio_from_script

        self._game = config.game

        # set to interrupt streamed/fallback external playback when the player talks over a line
        self._external_playback_stop = Event()
        self.__streaming_unsupported_warned = False

        # reuse pooled connections across requests to skip the TCP handshake on remote TTS servers
        # servers drop idle connections which surfaces as a read error on the next
        # request, so retries must cover read errors and non-idempotent methods (POST)
        self._session = requests.Session()
        retry_adapter = HTTPAdapter(max_retries=Retry(total=2, connect=2, read=1, backoff_factor=0.1, allowed_methods=None))
        self._session.mount('http://', retry_adapter)
        self._session.mount('https://', retry_adapter)

    @utils.time_it
    def stop_external_playback(self):
        """Stops any externally-played voiceline audio (streamed or fallback) currently playing"""
        self._external_playback_stop.set()
        try:
            import sounddevice
            sounddevice.stop()
        except Exception:
            pass

    @utils.time_it
    def synthesize(self, voice: str, voiceline: str, in_game_voice: str, csv_in_game_voice: str, voice_accent: str, synth_options: SynthesizationOptions, advanced_voice_model: str | None = None) -> tuple[str, bool]:
        """Synthesizes a given voiceline

        Returns:
            tuple[str, bool]: the path to the synthesized voiceline file, and whether the voiceline was already played externally during synthesis (streamed fast response)
        """
        logger.debug(f'last_voice: {self._last_voice}, voice: {voice}, in_game_voice: {in_game_voice}, csv_in_game_voice: {csv_in_game_voice}, advanced_voice_model: {advanced_voice_model}, voice_accent: {voice_accent}')
        if self._last_voice == '' or (isinstance(self._last_voice, str) and self._last_voice.lower() not in {isinstance(v, str) and v.lower() for v in {voice, in_game_voice, csv_in_game_voice, advanced_voice_model, f'fo4_{voice}'}}):
            self.change_voice(voice, in_game_voice, csv_in_game_voice, advanced_voice_model, voice_accent)

        logger.log(22, f'Synthesizing voiceline: {voiceline.strip()}')

        final_voiceline_file_name = 'out' # "out" is the file name used by XTTS
        final_voiceline_file =  f"{self._voiceline_folder}/{final_voiceline_file_name}.wav"

        # Every intermediate a previous synthesis can leave behind must be gone before this one starts: TTS writes
        # out.wav, lip generation out.lip (FaceFXWrapper also out_r.wav), xWMAEncode out.xwm and LipFuzer /
        # Fuz_extractor out.fuz. When a step of THIS synthesis fails, a leftover would otherwise be taken for its
        # output (a stale out.xwm is packed into this line's .fuz; a stale out.fuz is renamed to this line's name)
        # and the game would voice an earlier line. If one cannot be removed, its provenance cannot be told apart,
        # so the synthesis fails closed instead of guessing (generate_sentence turns this into a TTS error)
        self._invalidate_intermediate_voiceline_files(final_voiceline_file)

        played_externally = self.tts_synthesize(voiceline, final_voiceline_file, synth_options)
        if not os.path.exists(final_voiceline_file):
            logger.error(f'TTS failed to generate voiceline at: {Path(final_voiceline_file)}')
            raise FileNotFoundError()
        
        if (self._lip_generation_enabled == 'enabled') or (self._lip_generation_enabled == 'lazy' and not synth_options.is_first_line_of_response):
            self._generate_voiceline_files(final_voiceline_file, voiceline)
        elif (self._lip_generation_enabled in ['lazy', 'disabled'] and self._game.base_game == GameEnum.FALLOUT4):
            self._generate_voiceline_files(final_voiceline_file, voiceline, skip_lip_generation=True)
        
        #rename to unique name        
        if (os.path.exists(final_voiceline_file)):
            #timestamp: str = datetime.datetime.now().strftime("%Y_%m_%d_%H_%M_%S_%f_")
            #new_wav_file_name = f"{self._voiceline_folder}/save/{timestamp + final_voiceline_file_name}.wav" 

            # A sanitized version of the voice text keeps the name readable; the per-synthesis token makes it this
            # synthesis' own. The text alone collided (same voice + same first 150 characters, or texts differing only
            # in stripped characters): a later line overwrote a queued line's files before the game played it, and a
            # later line whose .fuz generation failed inherited the earlier line's .fuz under its own name
            readable_name: str = "".join(c for c in f'{voice} {voiceline.strip()}'[:150] if c not in r'\/:*?"<>|.').strip()
            new_wav_file_name = f'{self._voiceline_folder}/save/{self._saved_voiceline_stem(readable_name, self._new_voiceline_token())}.wav'

            new_lip_file_name = new_wav_file_name.replace(".wav", ".lip")
            new_fuz_file_name = new_wav_file_name.replace(".wav", ".fuz")
            for saved_file in (new_wav_file_name, new_lip_file_name, new_fuz_file_name):
                if os.path.lexists(saved_file):
                    raise VoicelineProvenanceError(f'{saved_file} already exists; refusing to reuse a voiceline file of another synthesis')

            try:
                os.rename(final_voiceline_file, new_wav_file_name)
            except Exception as ex:
                # Never hand back a name with no file behind it, nor out.wav (the next synthesis removes it first)
                raise VoicelineProvenanceError(f'Could not save the synthesized voiceline {final_voiceline_file} ({type(ex).__name__}: {ex})') from ex

            if (self._lip_generation_enabled == 'enabled') or (self._lip_generation_enabled == 'lazy' and not synth_options.is_first_line_of_response):
                try:
                    os.rename(final_voiceline_file.replace(".wav", ".lip"), new_lip_file_name)
                except:
                    logger.error(f'Could not rename {final_voiceline_file.replace(".wav", ".lip")}')
            try:
                fuz_file_name = final_voiceline_file.replace(".wav", ".fuz")
                if (os.path.exists(fuz_file_name)):
                    # out.fuz can only be this synthesis' output (intermediates were invalidated above). If the rename
                    # fails, this line simply has no .fuz under its fresh name: the game gets no audio, not an old line
                    os.rename(fuz_file_name, new_fuz_file_name)
            except:
                logger.error(f'Could not rename {final_voiceline_file.replace(".wav", ".fuz")}; this line will have no audio in Fallout 4')
            final_voiceline_file = new_wav_file_name

        # if Debug Mode is on, play the audio file
        # if (self.debug_mode == '1') & (self.play_audio_from_script == '1'):
        #     winsound.PlaySound(final_voiceline_file, winsound.SND_FILENAME)
        return final_voiceline_file, played_externally


    INTERMEDIATE_VOICELINE_SUFFIXES: tuple[str, ...] = (".wav", ".lip", ".fuz", ".xwm", "_r.wav")

    def _invalidate_intermediate_voiceline_files(self, final_voiceline_file: str):
        """Removes every intermediate a previous synthesis may have left next to final_voiceline_file (out.wav).

        Raises:
            VoicelineProvenanceError: an intermediate exists and cannot be removed, so this synthesis' outputs
                could not be told apart from an earlier line's
        """
        base = final_voiceline_file[:-len(".wav")]
        for suffix in self.INTERMEDIATE_VOICELINE_SUFFIXES:
            path = base + suffix
            try:
                os.remove(path)
            except FileNotFoundError:
                continue
            except OSError as e:
                raise VoicelineProvenanceError(
                    f'{path} is left over from an earlier voiceline and cannot be removed ({type(e).__name__}); '
                    'not risking playing that earlier line. Close any program using it, or restart MantellaSoftware.') from e

    def _new_voiceline_token(self) -> str:
        """A token unique to one synthesis, appended to its saved file names"""
        return uuid.uuid4().hex[:12]

    # Longest file name component (bytes) the file systems this runs on accept: 255 bytes on Linux ext4/btrfs/tmpfs
    # (NAME_MAX), 255 UTF-16 units on NTFS. A UTF-8 byte count of at most 255 satisfies both (no character takes
    # fewer UTF-8 bytes than UTF-16 units)
    MAX_FILE_NAME_BYTES: int = 255
    SAVED_VOICELINE_EXTENSIONS: tuple[str, ...] = (".wav", ".lip", ".fuz")

    def _saved_voiceline_stem(self, readable_name: str, token: str) -> str:
        """'<readable name> <token>', with the readable part cut so that the complete file name - token and the
        longest saved extension included - fits MAX_FILE_NAME_BYTES once UTF-8 encoded.

        The readable part is cut on a character boundary (never inside a multi-byte character), keeping as much
        of its beginning as fits; the token is always kept whole, so the name stays this synthesis' own. Before, a
        150-character text of multi-byte characters (or one that was already close to the limit) went over 255
        bytes once the token was added, and saving the line failed on Linux."""
        suffix = f' {token}'
        longest_extension = max(len(ext.encode('utf-8')) for ext in self.SAVED_VOICELINE_EXTENSIONS)
        budget = self.MAX_FILE_NAME_BYTES - len(suffix.encode('utf-8')) - longest_extension
        readable = readable_name.encode('utf-8', errors='ignore')[:max(budget, 0)].decode('utf-8', errors='ignore').rstrip()
        return f'{readable}{suffix}' if readable else token


    @abstractmethod
    @utils.time_it
    def change_voice(self, voice: str, in_game_voice: str | None = None, csv_in_game_voice: str | None = None, advanced_voice_model: str | None = None, voice_accent: str | None = None, voice_gender: int | None = None, voice_race: str | None = None):
        """Change the voice model
        """
        pass


    @utils.time_it
    def tts_synthesize(self, voiceline: str, final_voiceline_file: str, synth_options: SynthesizationOptions) -> bool:
        """Synthesizes the voiceline, streaming it from the TTS server and playing it externally as it arrives when requested and supported

        Returns:
            bool: whether the voiceline was already played externally during synthesis
        """
        if synth_options.stream_first_line and self.supports_streaming:
            url, data, headers = self._build_stream_request(voiceline)
            if self._stream_play_and_save(url, data, headers, final_voiceline_file):
                return True
            # streaming failed before any audio arrived, so fall back to a standard request
            # the game side skips external playback for lines marked as played externally, so play the fallback from here
            self._synthesize_voiceline(voiceline, final_voiceline_file, synth_options)
            if os.path.exists(final_voiceline_file):
                self._play_wav_async(final_voiceline_file)
                return True
            return False
        if synth_options.stream_first_line and not self.__streaming_unsupported_warned:
            logger.warning(f'{type(self).__name__} does not support audio streaming. The Streamed Fast Response setting will be ignored')
            self.__streaming_unsupported_warned = True
        self._synthesize_voiceline(voiceline, final_voiceline_file, synth_options)
        return False


    @abstractmethod
    @utils.time_it
    def _synthesize_voiceline(self, voiceline: str, final_voiceline_file: str, synth_options: SynthesizationOptions):
        """Synthesize the voiceline with the TTS service
        """
        pass


    def _build_stream_request(self, voiceline: str) -> tuple[str, dict, dict | None]:
        """The streaming endpoint, JSON payload, and optional headers for a streamed synthesis request (required for services where supports_streaming is True)
        """
        raise NotImplementedError(f'{type(self).__name__} sets supports_streaming but does not implement _build_stream_request')


    def _sanitize_voice_name(self, voice_name):
        """Sanitizes the voice name by removing spaces."""
        if isinstance(voice_name, str):
            return voice_name.replace(" ", "").lower()
        else:
            return ''


    @utils.time_it
    def _send_request(self, url, data):
        try:
            self._session.post(url, json=data)
        except requests.exceptions.RequestException as e:
            logger.warning(f'TTS request to {url} failed: {e}')


    @utils.time_it
    def _stream_play_and_save(self, url: str, request_data: dict, headers: dict | None, final_voiceline_file: str) -> bool:
        """Streams a voiceline from the TTS server, playing the audio externally as it arrives and saving the complete wav afterwards

        Args:
            url (str): the streaming endpoint to POST to
            request_data (dict): the JSON payload for the streaming request
            headers (dict | None): optional HTTP headers for the request
            final_voiceline_file (str): the path to save the complete wav to

        Returns:
            bool: whether audio was received and saved. If False, the caller should fall back to a standard request
        """
        try:
            import sounddevice
        except Exception:
            logger.warning('sounddevice not available, falling back to non-streamed synthesis')
            return False

        self._external_playback_stop.clear()
        volume = self._config.fast_response_mode_volume / 100
        pcm = bytearray()
        header = bytearray()
        header_parsed = False
        played_up_to = 0
        sample_rate = 24000
        channels = 1
        bytes_per_frame = 2
        playback_queue: Queue = Queue()
        playback_failed = Event()
        playback_thread: Thread | None = None
        start_time = time.perf_counter()

        def elapsed_s() -> float:
            return round((time.perf_counter() - start_time), 5)

        logger.debug(f'Streaming voiceline from {url}')

        def playback_worker():
            try:
                with sounddevice.OutputStream(samplerate=sample_rate, channels=channels, dtype='float32') as stream:
                    logger.log(self._loglevel, f"TTS took {elapsed_s()} seconds to start streaming")
                    while True:
                        samples = playback_queue.get()
                        if samples is None:
                            return
                        if self._external_playback_stop.is_set():
                            # Player interrupted: stop playing and empty the queue
                            while playback_queue.get() is not None:
                                pass
                            return
                        stream.write(samples)
            except Exception as e:
                playback_failed.set()
                logger.warning(f'Streamed voiceline playback failed: {e}')
                while playback_queue.get() is not None:
                    pass

        def enqueue_new_samples():
            nonlocal played_up_to
            aligned = len(pcm) - (len(pcm) % bytes_per_frame)
            if aligned > played_up_to:
                if played_up_to == 0:
                    logger.debug(f'First audio received {elapsed_s()} seconds after request start')
                samples = np.frombuffer(bytes(pcm[played_up_to:aligned]), dtype='<i2')
                samples = (samples.astype(np.float32) / 32768.0) * volume
                playback_queue.put(samples.reshape(-1, channels))
                played_up_to = aligned

        try:
            with self._session.post(url, json=request_data, headers=headers, stream=True, timeout=(5, 60)) as response:
                if response.status_code != 200:
                    logger.error(f'TTS streaming request failed. HTTP {response.status_code}: {response.text[:300]}')
                    return False

                for chunk in response.iter_content(chunk_size=4096):
                    if not header_parsed:
                        header += chunk
                        try:
                            parsed = self._parse_wav_header(bytes(header))
                        except ValueError as e:
                            logger.warning(f'{e}, falling back to non-streamed synthesis')
                            return False
                        if parsed is None:
                            if len(header) > 65536:
                                logger.warning('Could not find PCM data in streamed TTS response, falling back to non-streamed synthesis')
                                return False
                            continue
                        data_offset, sample_rate, channels, bits_per_sample = parsed
                        if bits_per_sample != 16:
                            logger.warning(f'Streamed TTS audio is {bits_per_sample}-bit, only 16-bit is supported. Falling back to non-streamed synthesis')
                            return False
                        logger.debug(f'Stream header received {elapsed_s()} seconds after request start ({sample_rate}Hz, {channels}ch)')
                        bytes_per_frame = 2 * channels
                        pcm += header[data_offset:]
                        header_parsed = True
                        playback_thread = Thread(target=playback_worker, daemon=True)
                        playback_thread.start()
                        enqueue_new_samples()
                    else:
                        pcm += chunk
                        enqueue_new_samples()
        except requests.exceptions.RequestException as e:
            logger.warning(f'Streamed synthesis was interrupted: {e}')
        finally:
            if playback_thread is not None:
                playback_queue.put(None)

        if not pcm:
            return False

        aligned = len(pcm) - (len(pcm) % bytes_per_frame)
        samples = np.frombuffer(bytes(pcm[:aligned]), dtype='<i2')
        if channels > 1:
            samples = samples.reshape(-1, channels)
        sf.write(final_voiceline_file, samples, sample_rate, subtype='PCM_16')
        audio_duration = aligned / bytes_per_frame / sample_rate
        logger.debug(f'Stream complete {elapsed_s()} seconds after request start: saved {audio_duration:.2f}s of audio ({aligned} bytes), playback continues in the background')

        if playback_failed.is_set():
            self._play_wav_async(final_voiceline_file)
        return True


    @staticmethod
    def _parse_wav_header(buffer: bytes) -> tuple[int, int, int, int] | None:
        """Parses a (possibly streaming) WAV header, tolerating invalid chunk sizes on the data chunk

        Returns:
            tuple: (data offset, sample rate, channels, bits per sample), or None if the header is incomplete

        Raises:
            ValueError: if the buffer is not a valid WAV stream
        """
        if len(buffer) < 12:
            return None
        if buffer[0:4] != b'RIFF' or buffer[8:12] != b'WAVE':
            raise ValueError('TTS response is not a WAV stream')
        pos = 12
        sample_rate = None
        channels = None
        bits_per_sample = None
        while pos + 8 <= len(buffer):
            chunk_id = buffer[pos:pos + 4]
            chunk_size = int.from_bytes(buffer[pos + 4:pos + 8], 'little')
            if chunk_id == b'data':
                if sample_rate is None:
                    raise ValueError('WAV stream is missing its fmt chunk')
                return pos + 8, sample_rate, channels, bits_per_sample
            if pos + 8 + chunk_size > len(buffer):
                return None
            if chunk_id == b'fmt ':
                if chunk_size < 16:
                    raise ValueError('WAV stream has an invalid fmt chunk')
                channels = int.from_bytes(buffer[pos + 10:pos + 12], 'little')
                sample_rate = int.from_bytes(buffer[pos + 12:pos + 16], 'little')
                bits_per_sample = int.from_bytes(buffer[pos + 22:pos + 24], 'little')
            pos += 8 + chunk_size + (chunk_size % 2)
        return None


    @utils.time_it
    def _play_wav_async(self, filename: str):
        """Plays a wav file asynchronously at the fast response mode volume"""
        utils.play_audio_async(filename, self._config.fast_response_mode_volume / 100)


    @utils.time_it
    def _convert_to_16bit(self, input_file, output_file=None):
        if output_file is None:
            output_file = input_file
        # Read the audio file
        data, samplerate = sf.read(input_file)

        # Directly convert to 16-bit if data is in float format and assumed to be in the -1.0 to 1.0 range
        if np.issubdtype(data.dtype, np.floating):
            data_16bit = np.int16(data * 32767)
        elif not np.issubdtype(data.dtype, np.int16):
            data_16bit = data.astype(np.int16)
        else:
            # data is already int16
            data_16bit = data

        # Write the 16-bit audio data back to a file
        sf.write(output_file, data_16bit, samplerate, subtype='PCM_16')


    @utils.time_it
    def _generate_voiceline_files(self, wav_file: str, voiceline: str, skip_lip_generation: bool = False):
        """Generates .lip files for voicelines using Bethesda's LipGen tool
        with FaceFXWrapper as fallback. Additionally generates a .fuz file which is required for Fallout 4
        Args:
            wav_file (str): The path to the input .wav file containing the voiceline audio
            voiceline (str): The corresponding text voiceline used to help lip sync generation
            skip_lip_generation (bool): Whether to skip the lip file generation process and use a placeholder .lip file instead (useful for when FaceFXWrapper is experiencing issues)
        """
        @utils.time_it
        def run_facefx_command(command, facefx_path) -> None:
            if platform.system() == "Windows":
                startupinfo = STARTUPINFO()
                startupinfo.dwFlags |= STARTF_USESHOWWINDOW
                creationflags = subprocess.CREATE_NO_WINDOW
                batch_file_path = Path(facefx_path) / "run_mantella_command.bat"
                with open(batch_file_path, 'w', encoding='utf-8') as file:
                    file.write(f"@echo off\n{command} >nul 2>&1")
                subprocess.run(batch_file_path, cwd=facefx_path, creationflags=creationflags)
            else:
                bash_file_path = Path(facefx_path) / "run_mantella_command.sh"
                user_shell = utils.get_user_shell()
                with open(bash_file_path, 'w', encoding='utf-8') as file:
                    file.write(f"#!{user_shell}\nwine {command} > /dev/null 2>&1")
                subprocess.run([user_shell, str(bash_file_path)], cwd=facefx_path)


        def copy_placeholder_lip_file(lip_file: str, game: str) -> None:
            """
            .lip files are needed to generate .fuz files (required by Fallout 4)
            If generating lip files via FaceFXWrapper fails, this function ensures a .lip file exists by copying from a placeholder file
            """
            lip_placeholder_path = Path(utils.resolve_path()) / "data" / game / "placeholder" / "placeholder.lip"
            if not lip_placeholder_path.exists():
                raise FileNotFoundError(f"The source file does not exist: {lip_placeholder_path}")
            else:
                shutil.copy(lip_placeholder_path, lip_file)


        def generate_facefx_lip_file(facefx_path: str, wav_file: str, lip_file: str, voiceline: str, game: str) -> None:
            # Bethesda's LipGen:
            LipGen_path = Path(self._lipgen_path) / "LipGenerator/LipGenerator.exe"
            languages = {
                "fr" : 'French',
                'es' : 'Spanish',
                'de' : 'German',
                'it' : 'Italian',
                'ko' : 'Korean',
                'jp' : 'Japanese'}

            if os.path.exists(LipGen_path):
                language_parm = languages.get(self._language, 'USEnglish')

                #Using subprocess.run to retrieve the exit code
                args: str = f'"{LipGen_path}" "{wav_file}" "{voiceline}" -Language:{language_parm} -Automated'
                run_result: subprocess.CompletedProcess = subprocess.run(args, cwd=self._voiceline_folder, stderr=DEVNULL, stdout=DEVNULL,
                                                                         creationflags=subprocess.CREATE_NO_WINDOW)
                if run_result.returncode != 0 and len(voiceline) > 11 :
                    #Very short sentences sometimes fail to generate a .lip file, so skip warning
                    logger.warning(f'Lipgen returned {run_result.returncode}')
            else:
                if not self.__has_lipgen_warning_happened:
                    logger.info(f'Could not find {LipGen_path}. (Optional) Install or update the Creation Kit from Steam for faster lip sync generation')
                    self.__has_lipgen_warning_happened = True
                # Fall back to using FaceFXWrapper if LipGen not detected
                face_wrapper_executable: Path = Path(self._facefx_path) / "FaceFXWrapper.exe"
                if not face_wrapper_executable.exists():
                    logger.error(f'Could not find FaceFXWrapper.exe in "{face_wrapper_executable.parent}" with which to create a lip sync file, download it from: https://github.com/Nukem9/FaceFXWrapper/releases')
                    raise FileNotFoundError()

                cdf_path = Path(facefx_path) / 'FonixData.cdf' 
                if not cdf_path.exists():
                    logger.error(f'Could not find FonixData.cdf in "{cdf_path.parent}" required by FaceFXWrapper.')
                    raise FileNotFoundError()
        
                # Run FaceFXWrapper.exe
                r_wav = wav_file.replace(".wav", "_r.wav")
                commands = [
                    face_wrapper_executable.name,
                    game,
                    "USEnglish",
                    cdf_path.name,
                    f'"{wav_file}"',
                    f'"{r_wav}"',
                    f'"{lip_file}"',
                    f'"{voiceline}"'
                ]
                command = " ".join(commands)
                run_facefx_command(command, facefx_path)

                # remove file created by FaceFXWrapper
                if os.path.exists(wav_file.replace(".wav", "_r.wav")):
                    os.remove(wav_file.replace(".wav", "_r.wav"))

        def generate_fuz_file(facefx_path: str, wav_file: str, lip_file: str) -> None:
            #Fuz files needed for Fallout only
            #LipFuzer is Bethesda's official fuz creator
            LipFuz_path = Path(self._lipgen_path) / "LipFuzer/LipFuzer.exe"

            if os.path.exists(LipFuz_path):
                args: str = f'"{LipFuz_path}" -s "{self._voiceline_folder}" -d "{self._voiceline_folder}" -a wav --norec'
                run_result: subprocess.CompletedProcess = subprocess.run(args, cwd=self._voiceline_folder, stdout=DEVNULL, stderr=DEVNULL,
                                                                         creationflags=subprocess.CREATE_NO_WINDOW)
                if run_result.returncode != 0:
                    logger.warning(f'LipFuzer returned {run_result.returncode}')
            else:
                #Fall back to using Fuz_extractor and xWMAencode if LipFuzer not found
                logger.warning(f'Could not find {LipFuz_path}: please install or update the creation kit from Steam')
                fuz_extractor_executable = Path(facefx_path) / "Fuz_extractor.exe"
                if not fuz_extractor_executable.exists():
                    logger.error(f'Could not find Fuz_extractor.exe in "{facefx_path}" with which to create a fuz file, download it from: https://www.nexusmods.com/skyrimspecialedition/mods/55605')
                    raise FileNotFoundError()
        
                xWMAEncode_executable = Path(facefx_path) / "xWMAEncode.exe"
                if not xWMAEncode_executable.exists():
                    logger.error(f'Could not find xWMAEncode.exe in "{facefx_path}" with which to create a fuz file, download it from: https://www.nexusmods.com/skyrimspecialedition/mods/55605')
                    raise FileNotFoundError()

                xwm_file = wav_file.replace(".wav", ".xwm")
                xwmcmds = [
                    xWMAEncode_executable.name,
                    f'"{wav_file}"',
                    f'"{xwm_file}"'
                    ]
                xwm_command = " ".join(xwmcmds)
                run_facefx_command(xwm_command, facefx_path)

                fuzfile = wav_file.replace(".wav", ".fuz")
                fuzcmds = [
                    fuz_extractor_executable.name,
                    "-c",
                    f'"{fuzfile}"',
                    f'"{lip_file}"',
                    f'"{xwm_file}"'
                    ]
                fuz_command = " ".join(fuzcmds)
                run_facefx_command(fuz_command, facefx_path)

        try:
            
            # path to store .lip file (next to voiceline .wav)
            lip_file: str = wav_file.replace(".wav", ".lip")
            
            if not skip_lip_generation:
                generate_facefx_lip_file(self._facefx_path, wav_file, lip_file, voiceline, self._game.base_game.display_name)
            else:
                copy_placeholder_lip_file(lip_file, self._game.base_game.display_name)
            
            # Fallout 4 requires voicelines in a .fuz format
            if self._game.base_game == GameEnum.FALLOUT4:    
                generate_fuz_file(self._facefx_path,wav_file, lip_file)
        
        except Exception as e:
            logger.warning(e)
