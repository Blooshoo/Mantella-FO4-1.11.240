"""Opt-in bounded remote text route; DEEPSEEK_API_KEY is resolved lazily from environment only. No file fallback. See EXPERIMENTAL.md."""
import onnxruntime  # noqa: F401  (same import order as main.py)
import traceback

from src.http.http_server import http_server
from src.http.legacy_package_protocol import PACKAGE_PROTOCOL
from src.http.routes.routeable import routeable
from src.llm.remote_adapter import RemoteMantellaRoute as mantella_route
from src.setup import MantellaSetup
from src.ui.start_ui import StartUI
import src.utils as utils


def main():
    logger = None
    try:
        mantella_version = '0.14'
        config, language_info = MantellaSetup().initialise(
            config_file='config.ini',
            logging_file='logging.log',
            language_file='data/language_support.csv',
            mantella_version=mantella_version)
        logger = utils.get_logger()
        logger.log(24, f'\nMantella v{mantella_version} - EXPLICIT REMOTE TEXT ROUTE {PACKAGE_PROTOCOL} (restricted text mode)')
        conversation = mantella_route(config=config, language_info=language_info, package_protocol=PACKAGE_PROTOCOL)
        routes: list[routeable] = [conversation, StartUI(config)]
        http_server().start(int(config.port), routes, config.play_startup_sound, config.show_http_debug_messages)
    except Exception as e:
        (logger.error if logger else print)("".join(traceback.format_exception(e)))
        input("Press Enter to exit.")


if __name__ == '__main__':
    main()
