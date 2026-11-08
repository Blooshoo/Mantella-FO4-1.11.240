import asyncio
import json
from typing import Any, Hashable

from fastapi import FastAPI, Request
from src.config.config_loader import ConfigLoader
from src.games.fallout4 import Fallout4
from src.games.gameable import Gameable
from src.games.skyrim import Skyrim
from src.output_manager import ChatManager
from src.llm.llm_client import LLMClient
from src.llm.summary_client import SummaryLLMClient
from src.game_manager import GameStateManager
from src.http.routes.routeable import routeable
from src.http.communication_constants import communication_constants as comm_consts
from src.tts.ttsable import TTSable
from src.tts.tts_factory import create_tts
from src import utils
from src.config.definitions.game_definitions import GameEnum
from src.config.definitions.tts_definitions import TTSEnum
from src.actions.function_manager import FunctionManager
from src.http.legacy_package_protocol import LegacyPackageProtocol

logger = utils.get_logger()


class mantella_route(routeable):
    """Main route for Mantella conversations

    Args:
        routeable (_type_): _description_
    """
    def __init__(self, config: ConfigLoader, language_info: dict[Hashable, str], *, package_protocol: str | None = None) -> None:
        super().__init__(config)
        self.__language_info: dict[Hashable, str] = language_info
        self.__game: GameStateManager | None = None
        # No auto-detection or global config/environment switch: modern is unchanged.
        self.__package_protocol = LegacyPackageProtocol(package_protocol) if package_protocol is not None else None

        # if not self._can_route_be_used():
        #     error_message = "MantellaSoftware settings faulty. Please check MantellaSoftware's window or log."
        #     logger.error(error_message)

    @utils.time_it
    def _setup_route(self):
        if self.__package_protocol:
            self.__package_protocol.check_config(self._config)
            self.__package_protocol.reset()
        if self.__game:
            self.__game.end_conversation({})

        game: Gameable
        game_enum = self._config.game
        if game_enum.base_game == GameEnum.FALLOUT4:
            game = Fallout4(self._config)
        else:
            game = Skyrim(self._config)

        tts: TTSable = create_tts(self._config.tts_service, self._config, game)

        llm_client = LLMClient(self._config)

        summary_client: SummaryLLMClient | None = None
        if self._config.summary_llm_enabled:
            summary_client = SummaryLLMClient(self._config)

        chat_manager = ChatManager(self._config, tts, llm_client, game)
        self.__game = GameStateManager(game, chat_manager, self._config, self.__language_info, llm_client, summary_client)

    @utils.time_it
    def add_route_to_server(self, app: FastAPI):
        @app.post("/mantella")
        async def mantella(request: Request):
            if self.__package_protocol:
                return await self.__handle_package_request(request)
            if not self._can_route_be_used():
                error_message = "MantellaSoftware settings faulty. Please check MantellaSoftware's window or log."
                logger.error(error_message)
                return self.error_message(error_message)
            if not self.__game:
                error_message = "Game manager setup failed. There is most likely an issue with the config.ini."
                logger.error(error_message)
                return self.error_message(error_message)
            reply = {}
            received_json: dict[str, Any] | None = await request.json()
            if received_json:
                logger.debug('Processing request...')
                if self._config.show_http_debug_messages:
                    logger.log(self._log_level_http_in, json.dumps(received_json, indent=4))
                request_type: str = received_json[comm_consts.KEY_REQUESTTYPE]
                match request_type:
                    case comm_consts.KEY_REQUESTTYPE_INIT:
                        # nothing needs to be done for this request aside from self._can_route_be_used() being triggered
                        logger.debug('Mantella settings initialized')
                        reply = {comm_consts.KEY_REPLYTYPE: comm_consts.KEY_REPLYTTYPE_INITCOMPLETED}
                    case comm_consts.KEY_REQUESTTYPE_STARTCONVERSATION:
                        reply = self.__game.start_conversation(received_json)
                    case comm_consts.KEY_REQUESTTYPE_CONTINUECONVERSATION:
                        reply = self.__game.continue_conversation(received_json)
                        # An end reply that follows a goodbye still playing is held until it has played, like
                        # every next line; awaited, so other requests are served meanwhile (the conversation is
                        # already saved and dropped)
                        hold_s = self.__game.pop_reply_hold_seconds()
                        if hold_s > 0:
                            logger.debug(f'Holding the end reply {round(hold_s, 1)} seconds for the goodbye to play')
                            await asyncio.sleep(hold_s)
                    case comm_consts.KEY_REQUESTTYPE_PLAYERINPUT:
                        reply = self.__game.player_input(received_json)
                    case comm_consts.KEY_REQUESTTYPE_ENDCONVERSATION:
                        reply = self.__game.end_conversation(received_json)
                    case _:
                        reply = self.error_message(f"Request type '{request_type}' was not recognized")
            else:
                reply = self.error_message(f"Request did not contain properly formatted json!")

            if self._config.show_http_debug_messages:
                logger.log(self._log_level_http_out, json.dumps(reply, indent=4))
            return reply

    async def __handle_package_request(self, request: Request):
        protocol = self.__package_protocol
        try:
            received_json = await request.json()
            # Admission happens before route setup can create model/TTS/STT objects.
            # Repeat in dispatch after config reload; preflight never invokes a game.
            protocol.prepare_request(received_json, self._config)
            request_type = received_json[comm_consts.KEY_REQUESTTYPE]
            if request_type == comm_consts.KEY_REQUESTTYPE_ENDCONVERSATION and self.__game is not None:
                # Do not reload/recreate the manager or lose the explicit end timestamp.
                reply = protocol.dispatch(received_json, self._config, self.__game)
            else:
                if not self._can_route_be_used():
                    raise ValueError("MantellaSoftware settings faulty")
                if self.__game is None:
                    raise ValueError("Game manager setup failed")
                reply = protocol.dispatch(received_json, self._config, self.__game)
            await self.__hold_package_end_ack(request_type, reply)
            return reply
        except Exception as error:
            return protocol.failure(error, self.__game)

    async def __hold_package_end_ack(self, request_type: str, reply: dict) -> None:
        """O-10 for the legacy package. The continue hold is consumed; the explicit-end deadline survives reads
        to cover concurrent duplicates. The async wait applies only to a successful end acknowledgement:
        * continue -> end: the action-blind line request after a goodbye (pop_reply_hold_seconds);
        * explicit end: the package raises the goodbye's end action BEFORE Say and sends its end request at once,
          so its acknowledgement waits for the goodbye captured before the conversation was dropped
          (pop_end_ack_hold_seconds). Errors/refusals and ordinary ends without a pending goodbye are not held."""
        reply_hold = self.__game.pop_reply_hold_seconds() if request_type == comm_consts.KEY_REQUESTTYPE_CONTINUECONVERSATION else 0.0
        end_hold = self.__game.pop_end_ack_hold_seconds()
        if request_type == comm_consts.KEY_REQUESTTYPE_ENDCONVERSATION:
            hold_s = end_hold
        elif request_type == comm_consts.KEY_REQUESTTYPE_CONTINUECONVERSATION:
            hold_s = reply_hold
        else:
            hold_s = 0.0
        if hold_s > 0 and isinstance(reply, dict) and reply.get(comm_consts.KEY_REPLYTYPE) == comm_consts.KEY_REPLYTYPE_ENDCONVERSATION:
            logger.debug(f'Holding the package end acknowledgement {round(hold_s, 1)} seconds for the goodbye to play')
            await asyncio.sleep(hold_s)
