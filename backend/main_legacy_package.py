"""OPT-IN entry point: serve the legacy Fallout 4 Mantella 0.13.0 package (archive SHA-256 573c5a89...) through the
restricted, fail-closed package adapter. main.py (the modern default) is unchanged and does NOT select it.

Restricted text/basic-action mode only. The adapter refuses (with an explanatory in-game notification, then
cleanup) microphone/PTT/interruption, vision/screenshots, advanced/parameterized/response-required actions,
extra request actions (including reload) and speakerless/ambiguous action targets. config.ini must explicitly
set those capabilities off; nothing is rewritten automatically. Binds uvicorn's default 127.0.0.1 only.
"""
import onnxruntime  # noqa: F401  (same import order as main.py)
import traceback

from src.http.http_server import http_server
from src.http.legacy_package_protocol import PACKAGE_PROTOCOL
from src.http.routes.routeable import routeable
from src.http.routes.mantella_route import mantella_route
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
        logger.log(24, f'\nMantella v{mantella_version} - LEGACY PACKAGE ADAPTER {PACKAGE_PROTOCOL} (restricted text mode)')
        conversation = mantella_route(config=config, language_info=language_info, package_protocol=PACKAGE_PROTOCOL)
        routes: list[routeable] = [conversation, StartUI(config)]
        http_server().start(int(config.port), routes, config.play_startup_sound, config.show_http_debug_messages)
    except Exception as e:
        (logger.error if logger else print)("".join(traceback.format_exception(e)))
        input("Press Enter to exit.")


if __name__ == '__main__':
    main()
