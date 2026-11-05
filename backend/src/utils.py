import time
import logging
import re
import string
import sys
import os
import threading
from shutil import rmtree
from charset_normalizer import detect
import platform
from pathlib import Path
from urllib.parse import urlparse
from src.telemetry.telemetry import get_telemetry_manager, create_span_from_thread


if platform.system() == "Windows":
    import winsound
    import winreg

def get_logger():
    return logging.getLogger("Mantella")

logger = get_logger()

def time_it(func):
    def wrapper(*args, **kwargs):
        telemetryManager = get_telemetry_manager()
        if telemetryManager._context.telemetry_enabled:
            # Use create_span_from_thread to support cross-thread trace continuity
            with create_span_from_thread(f"{func.__module__}.{func.__name__}") as span:
                span.set_attribute("function_name", func.__name__)
                span.set_attribute("module_name", func.__module__)
                result = func(*args, **kwargs)
        else:
            start = time.time()
            result = func(*args, **kwargs)
            end = time.time()
            logger.debug(f"Function {func.__module__}.{func.__name__} took {round(end - start, 5)} seconds to execute")

        return result
    return wrapper


def clean_text(text):
    # Remove all punctuation from the sentence
    text_cleaned = text.translate(str.maketrans('', '', string.punctuation))
    # Remove any extra whitespace
    text_cleaned = remove_extra_whitespace(text_cleaned)
    text_cleaned = text_cleaned.lower()

    return text_cleaned


def remove_extra_whitespace(text):
    return re.sub('\\s+', ' ', text).strip()


def remove_trailing_number(s):
    try:
        return re.sub(r'\d+$', '', s).strip()
    except:
        return s


def parse_race_name(raw_race: str | None) -> str | None:
    """Extracts the readable race name from a raw game race string (eg "[Race <NordRace (00013746)>]" -> "Nord").
    Race strings without the raw game format are returned unchanged."""
    if raw_race and 'Race <' in raw_race:
        raw_race = raw_race.split('Race <', 1)[1].split(' ')[0]
        if raw_race.endswith('Race'):
            raw_race = raw_race[:raw_race.rfind('Race')].strip()
    return raw_race


def resolve_path():
    if getattr(sys, 'frozen', False):
        resolved_path = os.path.dirname(sys.executable)
    else:
        resolved_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    return resolved_path


def play_mantella_ready_sound():
    try:
        mantella_ready_wav_path = os.path.join(resolve_path(), 'data', 'mantella_ready.wav')
        if platform.system() == "Windows":
            winsound.PlaySound(mantella_ready_wav_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        else:
            from playsound import playsound
            playsound(mantella_ready_wav_path, block=False)
    except:
        pass


def play_no_mic_input_detected_sound():
    try:
        no_mic_input_detected_wav_path = os.path.join(resolve_path(),'data','no_mic_input_detected.wav')
        if platform.system() == "Windows":
            winsound.PlaySound(no_mic_input_detected_wav_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        else:
            from playsound import playsound
            playsound(no_mic_input_detected_wav_path, block=False)
    except:
        pass


def play_error_sound():
    try:
        if platform.system() == "Windows":
            winsound.PlaySound("SystemHand", winsound.SND_ALIAS | winsound.SND_ASYNC)
        else:
            from playsound import playsound
            playsound(os.path.join(resolve_path(), 'data', 'error.wav'), block=False)
    except:
        pass


def play_audio_async(filename: str, volume: float = 0.5):
    """Plays an audio file asynchronously with volume control

    Args:
        filename (str): Path to the audio file
        volume (float): Volume multiplier (0.0 to 1.0)
    """
    def playback():
        try:
            import sounddevice
            import soundfile as sf
            data, samplerate = sf.read(filename)
            sounddevice.play(data * volume, samplerate)
        except Exception as e:
            logger.warning(f'Could not play audio file {filename}: {e}')
    threading.Thread(target=playback, daemon=True).start()


@time_it
def get_file_encoding(file_path) -> str | None:
    with open(file_path,'rb') as f:
        data = f.read()
    encoding = detect(data).get("encoding")
    if isinstance(encoding, str):
        return encoding
    else:
        return None
    

# WAVE format tags whose data chunk is fixed-size frames (PCM, IEEE float, A-law, mu-law, extensible)
_FRAME_BASED_WAVE_FORMATS = {0x0001, 0x0003, 0x0006, 0x0007, 0xFFFE}
_WAVE_SIZE_UNKNOWN = {0, 0xFFFFFFFF}  # what streaming writers put in the data size when the length is unknown


@time_it
def get_audio_duration(audio_file: str) -> float:
    """Used to estimate when an external software has finished playing the audio file

    Returns the duration of the audio actually present in a RIFF/WAVE file. It paces the conversation (the
    next line waits this long) and is sent to the game, so it must never exceed the bytes that exist: a
    streamed TTS response declares an unknown size (0xFFFFFFFF, read as ~24.8 hours at 24 kHz mono 16-bit,
    or 0) and a truncated file claims more data than it holds. Only the fmt chunk's sample rate and block
    alignment are used, so float and WAVE_FORMAT_EXTENSIBLE files work too (Python 3.11's wave rejects them).

    Raises:
        OSError: the file cannot be read
        ValueError: not a RIFF/WAVE file with a usable fmt chunk followed by a data chunk
    """
    with open(audio_file, 'rb') as f:
        file_size = os.fstat(f.fileno()).st_size
        riff = f.read(12)
        if len(riff) < 12 or riff[0:4] != b'RIFF' or riff[8:12] != b'WAVE':
            raise ValueError('not a RIFF/WAVE file')
        sample_rate = block_align = 0
        while True:
            header = f.read(8)
            if len(header) < 8:
                raise ValueError('no data chunk')
            chunk_id = header[0:4]
            chunk_size = int.from_bytes(header[4:8], 'little')
            if chunk_id == b'fmt ':
                fmt = f.read(min(chunk_size, 40))  # never trust the size for the read itself
                if chunk_size < 16 or len(fmt) < 16:
                    raise ValueError('invalid fmt chunk')
                format_tag = int.from_bytes(fmt[0:2], 'little')
                if format_tag not in _FRAME_BASED_WAVE_FORMATS:
                    raise ValueError(f'unsupported WAVE format 0x{format_tag:04X}')
                sample_rate = int.from_bytes(fmt[4:8], 'little')
                block_align = int.from_bytes(fmt[12:14], 'little')
                if sample_rate <= 0 or block_align <= 0:
                    raise ValueError('fmt chunk has no sample rate or block alignment')
                f.seek(chunk_size - len(fmt) + (chunk_size % 2), os.SEEK_CUR)
            elif chunk_id == b'data':
                if not sample_rate:
                    raise ValueError('data chunk before fmt chunk')
                present = max(0, file_size - f.tell())
                declared = present if chunk_size in _WAVE_SIZE_UNKNOWN else chunk_size
                return (min(declared, present) // block_align) / sample_rate
            else:
                f.seek(chunk_size + (chunk_size % 2), os.SEEK_CUR)


def get_tmp_dir() -> str:
    if platform.system() == "Windows":
        return os.getenv("TMP")
    else:
        envvars = ["TMPDIR", "TEMP", "TMP"]
        for envvar in envvars:
            tmpdir = os.getenv(envvar)
            if tmpdir is not None:
                return tmpdir
        return "/tmp"


def get_user_shell() -> str:
    if platform.system() == "Windows":
        return None
    else:
        shell = os.getenv("SHELL")
        if shell is not None:
            return shell
    return "/bin/bash"


def cleanup_tmp(tmp_folder: str):
    if os.path.exists(tmp_folder):
        mei_bundle = getattr(sys, "_MEIPASS", False)

        for filename in os.listdir(tmp_folder):
            file_path = os.path.join(tmp_folder, filename)
            try:
                if os.path.isfile(file_path):
                    os.remove(file_path)
                elif (os.path.isdir(file_path)) and (file_path != mei_bundle):
                    rmtree(file_path)
            except Exception as e:
                logger.error(f"Failed to delete tmp folder: {e}")


def cleanup_mei(remove_mei_folders: bool):
    """
    Rudimentary workaround for https://github.com/pyinstaller/pyinstaller/issues/2379
    Only relevant for onefile mode where _MEIPASS is a temp _MEI* directory.
    In onedir mode _MEIPASS points to the app folder itself.
    """
    mei_bundle = getattr(sys, "_MEIPASS", False)

    if mei_bundle and "_MEI" in mei_bundle:
        dir_mei, current_mei = mei_bundle.split("_MEI")
        mei_files = []
        for file in os.listdir(dir_mei):
            if file.startswith("_MEI") and not file.endswith(current_mei):
                mei_files.append(file)
        
        if (len(mei_files) > 0):
            if (remove_mei_folders):
                file_removed = 0
                for file in mei_files:
                    try:
                        rmtree(os.path.join(dir_mei, file))
                        file_removed += 1
                    except PermissionError:  # mainly to allow simultaneous pyinstaller instances
                        pass
                logger.log(24, f'{file_removed} previous runtime folder(s) cleaned up from {dir_mei}')
            else:
                logger.warning(f"Warning: {len(mei_files)} previous Mantella.exe runtime folder(s) found in {dir_mei}. See the Startup->Advanced tab in the Mantella UI for more information.")
        

def get_my_games_directory(custom_user_folder='') -> str:
    documents_path = custom_user_folder
    if documents_path == "":
        if platform.system() == "Windows":
            reg_key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders")
            documents_path = winreg.QueryValueEx(reg_key, "Personal")[0]
            winreg.CloseKey(reg_key)
        else:
            homepath = os.getenv('HOME')
            if homepath:
                documents_path = os.path.realpath(homepath + '/Documents')
        if documents_path == "":
            print("ERROR: Could not find 'Documents' folder or equivalent!")
        save_dir = Path(os.path.join(documents_path, "My Games", "Mantella"))
    else:
        save_dir = Path(documents_path)
    save_dir.mkdir(parents=True, exist_ok=True)
    return str(save_dir)


def convert_to_skyrim_hex_format(identifier: str) -> str:
    intID = int(identifier)
    if intID < 0:
        intID += 2**32
    hex_format = f'{intID:x}'.upper()
    return hex_format.rjust(8,"0")

def get_time_group(in_game_time):
    in_game_time = int(in_game_time)

    if in_game_time <= 4:
        time_group = 'at night'
    elif in_game_time <= 7:
        # NPCs wake up between 6 and 8
        time_group = 'in the early morning'
    elif in_game_time <= 11:
        # shops open at 8
        time_group = 'in the morning'
    elif in_game_time <= 14:
        time_group = 'in the afternoon'
    elif in_game_time <= 19:
        time_group = 'in the early evening'
    elif in_game_time <= 21:
        # shops shut at 8
        time_group = 'in the late evening'
    elif in_game_time <= 24:
        # NPCs sleep between 8 and 10
        time_group = 'at night'
    
    return time_group


def safe_str(value) -> str:
    """Convert a value to string safely, returning empty string for None/NaN/NaT/NA.
    
    Useful when reading values from pandas DataFrames where cells may contain
    None, NaN, NaT, pd.NA, or string representations of these.
    """
    import pandas as pd
    if value is None:
        return ''
    try:
        if pd.isna(value):
            return ''
    except (TypeError, ValueError):
        pass
    result = str(value).strip()
    if result.lower() in ('nan', 'nat', '<na>'):
        return ''
    return result


def parse_keywords(keyword_string: str) -> list[str]:
    """
    Given a comma-delimited string of keywords, return a list of trimmed, lowercase keywords
    
    Args:
        keyword_string (str): A single keyword or comma-separated list of keywords
        
    Returns:
        list[str]: A list of keywords
    """
    # Split on commas if present, or just return the stripped keyword in a list
    if ',' in keyword_string:
        keywords = [name.strip().lower() for name in keyword_string.split(',')]
    else:
        keywords = [keyword_string.strip().lower()]
    return keywords


def format_context_size(num):
    if num < 100_000:
        return f"{num:,}"
    elif num < 1_000_000:
        return f"{num // 1_000}k"
    elif num < 1_00_0000_000:
        return f"{num // 1_000_000}m"
    else:
        return f"{num // 1_000_000_000}b"


def format_price(price):
    if price < 0:
        return 'unknown'
    elif price == 0:
        return 'free'
    elif price.is_integer():
        return f"${price:.0f}"
    else:
        return f"${price:.2f}"
    

def get_openai_model_list():
    '''Get list of OpenAI models in the same format passed by OpenRouter
    At the time of this function creation, OpenAI models do not pass the same level of information as OpenRouter
    such as cost and context length, so these details need to be maintained manually.
    Prices are not stored to avoid sharing outdated information.
    '''
    openai_models = [
        {
            "id": "gpt-4o-mini",
            "model_extra": {
                "context_length": 128_000,
                "pricing": {"prompt": -1, "completion": -1},
                "architecture": {"modality": "text+image->text"}
            }
        },
        {
            "id": "gpt-4o",
            "model_extra": {
                "context_length": 128_000,
                "pricing": {"prompt": -1, "completion": -1},
                "architecture": {"modality": "text+image->text"}
            }
        },
        {
            "id": "gpt-4-turbo",
            "model_extra": {
                "context_length": 128_000,
                "pricing": {"prompt": -1, "completion": -1},
                "architecture": {"modality": "text+image->text"}
            }
        },
    ]
    
    # Convert the dictionary to a structure similar to what OpenRouter returns
    class Model:
        def __init__(self, id, model_extra):
            self.id = id
            self.model_extra = model_extra

    class SyncPage:
        def __init__(self, data):
            self.data = data

    models = [Model(**model) for model in openai_models]

    return SyncPage(data=models)


_KNOWN_SERVICES: dict[str, str] = {
    'openai': 'https://api.openai.com/v1',

    'openrouter': 'https://openrouter.ai/api/v1',
    'or': 'https://openrouter.ai/api/v1',

    'nanogpt': 'https://nano-gpt.com/api/v1',
    'nano': 'https://nano-gpt.com/api/v1',

    'kobold': 'http://127.0.0.1:5001/v1',
    'koboldcpp': 'http://127.0.0.1:5001/v1',

    'textgenwebui': 'http://127.0.0.1:5000/v1',
    'text-gen-web-ui': 'http://127.0.0.1:5000/v1',
    'textgenerationwebui': 'http://127.0.0.1:5000/v1',
    'text-generation-web-ui': 'http://127.0.0.1:5000/v1',
}


def resolve_service_endpoint(value: str) -> str:
    """Resolve a service name or alias to an endpoint URL.

    Returns the normalized input as-is if not a known service (assumed to be a direct URL).
    """
    normalized = value.strip().lower().replace(' ', '')
    return _KNOWN_SERVICES.get(normalized, normalized)


def is_local_url(url: str) -> bool:
    domain = urlparse(url).netloc
    return domain.startswith("10.") or domain.startswith("127.") or domain.startswith("192.168") or domain.startswith("172.") or domain.startswith("localhost")


def get_model_token_limits():
    token_limit_dict = {
        'auto': 128_000,
        'nous-capybara-7b:free': 4096,
        'mistral-7b-instruct:free': 32_768,
        'mythomist-7b:free': 32_768,
        'toppy-m-7b:free': 4096,
        'cinematika-7b:free': 8000,
        'gemma-7b-it:free': 8192,
        'bagel-34b': 8000,
        'psyfighter-13b': 4096,
        'psyfighter-13b-2': 4096,
        'noromaid-mixtral-8x7b-instruct': 8000,
        'nous-hermes-llama2-13b': 4096,
        'codellama-34b-instruct': 8192,
        'phind-codellama-34b': 4096,
        'neural-chat-7b': 4096,
        'nous-hermes-2-mixtral-8x7b-dpo': 32_000,
        'nous-hermes-2-mixtral-8x7b-sft': 32_000,
        'llava-13b': 2048,
        'nous-hermes-2-vision-7b': 4096,
        'llama-2-13b-chat': 4096,
        'synthia-70b': 8192,
        'mythalion-13b': 8192,
        'mythomax-l2-13b': 4096,
        'xwin-lm-70b': 8192,
        'goliath-120b': 6144,
        'noromaid-20b': 8192,
        'mythomist-7b': 32768,
        'remm-slerp-l2-13b:extended': 6144,
        'mythomax-l2-13b:extended': 8192,
        'weaver': 8000,
        'nous-capybara-7b': 4096,
        'codellama-70b-instruct': 2048,
        'openhermes-2-mistral-7b': 4096,
        'openhermes-2.5-mistral-7b': 4096,
        'remm-slerp-l2-13b': 4096,
        'toppy-m-7b': 4096,
        'cinematika-7b': 8000,
        'yi-34b-chat': 4096,
        'yi-34b': 4096,
        'yi-6b': 4096,
        'stripedhyena-nous-7b': 32_768,
        'stripedhyena-hessian-7b': 32_768,
        'mixtral-8x7b': 32_768,
        'nous-hermes-yi-34b': 4096,
        'nous-hermes-2-mistral-7b-dpo': 8192,
        'mistral-7b-openorca': 8192,
        'zephyr-7b-beta': 4096,
        'gpt-3.5-turbo': 16_385,
        'gpt-3.5-turbo-0125': 16_385,
        'gpt-3.5-turbo-16k': 16_385,
        'gpt-4-turbo-preview': 128_000,
        'gpt-4': 8191,
        'gpt-4-turbo': 128_000,
        'gpt-4-32k': 32_767,
        'gpt-4-vision-preview': 128_000,
        'gpt-3.5-turbo-instruct': 4095,
        'gpt-4o': 128_000,
        'gpt-4o-2024-05-13': 128_000,
        'palm-2-chat-bison': 36_864,
        'palm-2-codechat-bison': 28_672,
        'palm-2-chat-bison-32k': 131_072,
        'palm-2-codechat-bison-32k': 131_072,
        'gemini-pro': 131_040,
        'gemini-pro-vision': 65_536,
        'pplx-70b-online': 4096,
        'pplx-7b-online': 4096,
        'pplx-7b-chat': 8192,
        'pplx-70b-chat': 4096,
        'sonar-small-chat': 16_384,
        'sonar-medium-chat': 16_384,
        'sonar-small-online': 12_000,
        'sonar-medium-online': 12_000,
        'claude-3-opus': 200_000,
        'claude-3-sonnet': 200_000,
        'claude-3-haiku': 200_000,
        'claude-3-opus:beta': 200_000,
        'claude-3-sonnet:beta': 200_000,
        'claude-3-haiku:beta': 200_000,
        'llama-2-70b-chat': 4096,
        'nous-capybara-34b': 32_768,
        'airoboros-l2-70b': 4096,
        'chronos-hermes-13b': 4096,
        'mistral-7b-instruct': 32_768,
        'openchat-7b': 8192,
        'lzlv-70b-fp16-hf': 4096,
        'mixtral-8x7b-instruct': 32_768,
        'dolphin-mixtral-8x7b': 32_000,
        'rwkv-5-world-3b': 10_000,
        'rwkv-5-3b-ai-town': 10_000,
        'eagle-7b': 10_000,
        'gemma-7b-it': 8192,
        'claude-2': 200_000,
        'claude-2.1': 200_000,
        'claude-2.0': 100_000,
        'claude-instant-1': 100_000,
        'claude-instant-1.2': 100_000,
        'claude-2:beta': 200_000,
        'claude-2.1:beta': 200_000,
        'claude-2.0:beta': 100_000,
        'claude-instant-1:beta': 100_000,
        'zephyr-7b-beta:free': 4096,
        'openchat-7b:free': 8192,
        'mixtral-8x7b-instruct:nitro': 32_768,
        'llama-2-70b-chat:nitro': 4096,
        'mythomax-l2-13b:nitro': 4096,
        'mistral-7b-instruct:nitro': 32_768,
        'gemma-7b-it:nitro': 8192,
        'mistral-tiny': 32_000,
        'mistral-small': 32_000,
        'mistral-medium': 32_000,
        'mistral-large': 32_000,
        'command': 4096,
        'command-r': 128_000,
        'llama-3-8b-instruct:free': 8_192,
        'llama-3-lumimaid-8b': 24_576,
        'llama-3-8b-instruct:extended': 16_384,
        'llama-3-lumimaid-8b:extended': 24_576,
        'llama-3-8b': 8_192,
        'llama-3-70b': 8_192,
        'llama-3-sonar-small-32k-chat': 32_768,
        'llama-3-sonar-small-32k-online': 28_000,
        'llama-3-sonar-large-32k-chat': 32_768,
        'llama-3-sonar-large-32k-online': 28_000,
        'llama-3-8b-instruct': 8_192,
        'llama-3-70b-instruct': 8_192,
        'soliloquy-l3': 24_576,
        'llama-3-8b-instruct:nitro': 8_192,
        'llama-3-70b-instruct:nitro': 8_192,
    }
    return token_limit_dict
