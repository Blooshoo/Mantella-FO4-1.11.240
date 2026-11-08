"""Offline contracts through actual adapter, route and GameStateManager methods.

No ConfigLoader/default_config fixture (those read user files). Conversation,
external character loading, model/TTS and persistence boundaries are fakes. The
strict string-array assertion below models a schema requirement, NOT the native
DLL or PEX implementation; those remain separate acceptance gates.
"""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock
import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.game_manager as GM
from src.http.communication_constants import communication_constants as C
from src.http.legacy_package_protocol import (
    BASIC_ACTIONS, PACKAGE_PROTOCOL, LegacyPackageProtocol, PackageProtocolError,
)
from src.http.routes.mantella_route import mantella_route
from src.config.definitions.game_definitions import GameEnum
from src.config.definitions.llm_definitions import NarrationHandlingEnum
from src.conversation.action import Action
from src.llm.sentence import Sentence
from src.llm.sentence_content import SentenceContent, SentenceTypeEnum
from src.actions.function_manager import FunctionManager

FOLLOW = "mantella_npc_follow"


def basic_action(identifier=FOLLOW, requires_response=False):
    return Action(identifier, "Follow", "follow", "", "", requires_response, False, True, True, True)


def actor(name="Piper", ref="100", player=False):
    return {C.KEY_ACTOR_NAME: name, C.KEY_ACTOR_REFID: ref, C.KEY_ACTOR_ISPLAYER: player}


def start_request(actors=None):
    return {C.KEY_REQUESTTYPE: C.KEY_REQUESTTYPE_STARTCONVERSATION,
            C.KEY_INPUTTYPE: C.KEY_INPUTTYPE_TEXT,
            C.KEY_ACTORS: actors or [actor("Player", "14", True), actor()]}


def request(kind=C.KEY_REQUESTTYPE_CONTINUECONVERSATION, **fields):
    return {C.KEY_REQUESTTYPE: kind, **fields}


def line(name="Piper", text="Hello.", actions=None):
    return Sentence(SentenceContent(NS(name=name), text, SentenceTypeEnum.SPEECH,
                                    actions=actions or []), "fixture-real.wav", 0.25)


def assert_error(reply, fragment=None):
    assert reply[C.KEY_REPLYTYPE] == "error", reply
    assert isinstance(reply["mantella_message"], str) and reply["mantella_message"]
    if fragment:
        assert fragment.lower() in reply["mantella_message"].lower(), reply


def strict_legacy_string_array(payload):
    value = payload[C.KEY_ACTOR_ACTIONS]
    return value if type(value) is list and all(type(item) is str for item in value) else None


@pytest.fixture
def harness(monkeypatch):
    config = NS(game=GameEnum.FALLOUT4, automatic_greeting=False,
                narration_handling=NarrationHandlingEnum.CUT_NARRATIONS,
                vision_enabled=False, use_game_screenshots=False,
                advanced_actions_enabled=False, allow_interruption=False,
                random_llm_enabled=False, actions=[basic_action()],
                show_http_debug_messages=False, has_any_config_value_changed=False,
                have_all_config_values_loaded_correctly=True)
    game, chat, client, rememberer, talk = (MagicMock() for _ in range(5))
    game.modify_sentence_text_for_game.side_effect = lambda text: text
    talk.context.npcs_in_conversation.last_added_character = NS(name="Piper")
    talk.context.npcs_in_conversation.contains_multiple_npcs.return_value = False
    talk.resume_after_interrupting_action.return_value = False
    talk.continue_conversation.return_value = (C.KEY_REPLYTYPE_PLAYERTALK, None)
    talk.goodbye_hold_remaining_seconds.return_value = 0.0  # repair: modelled API (no pending goodbye)
    talk.process_player_input.return_value = ("hello", False, None)
    monkeypatch.setattr(GM, "Summaries", lambda *a, **k: rememberer)
    monkeypatch.setattr(GM, "Context", lambda *a, **k: talk.context)
    monkeypatch.setattr(GM, "Conversation", lambda *a, **k: talk)
    monkeypatch.setattr(GM, "RandomLLMSelector", lambda: MagicMock())
    monkeypatch.setattr(FunctionManager, "_actions", {key: {"requires_response": False} for key in BASIC_ACTIONS})
    manager = GM.GameStateManager(game, chat, config, {"language": "English"}, client)
    # External character loading is not the protocol being tested. The actual
    # update_context, start, continue, player_input, serializer and end still run.
    monkeypatch.setattr(manager, "load_character", lambda data: NS(name=data[C.KEY_ACTOR_NAME]))
    protocol = LegacyPackageProtocol(PACKAGE_PROTOCOL)
    return NS(config=config, game=game, chat=chat, client=client, talk=talk,
              manager=manager, protocol=protocol)


def admit(h):
    response = h.protocol.dispatch(start_request(), h.config, h.manager)
    assert response == {C.KEY_REPLYTYPE: C.KEY_REPLYTTYPE_STARTCONVERSATIONCOMPLETED,
                        C.KEY_STARTCONVERSATION_USENARRATOR: False}


def route_client(h, modern=False):
    route = mantella_route(h.config, {"language": "English"},
                           **({} if modern else {"package_protocol": PACKAGE_PROTOCOL}))
    route._mantella_route__game = h.manager
    route._has_route_been_initialized = True
    app = FastAPI()
    route.add_route_to_server(app)
    return route, TestClient(app)


def test_import_provenance():
    root = Path(__file__).resolve().parents[1]
    assert Path(GM.__file__).resolve() == root / "src/game_manager.py"
    module = importlib.import_module("src.http.legacy_package_protocol")
    assert Path(module.__file__).resolve() == root / "src/http/legacy_package_protocol.py"
    route = importlib.import_module("src.http.routes.mantella_route")
    assert Path(route.__file__).resolve() == root / "src/http/routes/mantella_route.py"


def test_selection_is_explicit(harness):
    assert mantella_route(harness.config, {})._mantella_route__package_protocol is None
    with pytest.raises(PackageProtocolError, match="Unknown package"):
        mantella_route(harness.config, {}, package_protocol="latest")


@pytest.mark.parametrize("identifier", sorted(BASIC_ACTIONS))
def test_actual_serializer_conversion_and_baseline_negative(harness, identifier):
    h = harness
    admit(h)
    sentence = line(actions=[{"identifier": identifier}])
    before = deepcopy(sentence.actions)
    original = h.manager.sentence_to_json(sentence, 1)
    assert strict_legacy_string_array(original) is None  # real unchanged modern serializer mismatch
    adapted = h.protocol.adapt_reply({C.KEY_REPLYTYPE: C.KEY_REPLYTYPE_NPCTALK,
                                     C.KEY_REPLYTYPE_NPCTALK: original}, C.KEY_REQUESTTYPE_CONTINUECONVERSATION)
    assert strict_legacy_string_array(adapted[C.KEY_REPLYTYPE_NPCTALK]) == [identifier]
    assert sentence.actions == before == original[C.KEY_ACTOR_ACTIONS]
    for key, value in original.items():
        if key != C.KEY_ACTOR_ACTIONS:
            assert adapted[C.KEY_REPLYTYPE_NPCTALK][key] == value


def test_multiple_actions_order_and_input_immutability(harness):
    h = harness
    admit(h)
    actions = [{"identifier": key} for key in sorted(BASIC_ACTIONS)]
    sentence = line(actions=actions)
    before = deepcopy(actions)
    h.talk.continue_conversation.return_value = (C.KEY_REPLYTYPE_NPCTALK, sentence)
    reply = h.protocol.dispatch(request(), h.config, h.manager)
    assert reply[C.KEY_REPLYTYPE_NPCTALK][C.KEY_ACTOR_ACTIONS] == sorted(BASIC_ACTIONS)
    assert sentence.actions == before
    h.game.prepare_sentence_for_game.assert_called_once()


def test_actual_route_init_start_spoken_player_transcript_ack_end(harness):
    h = harness
    route, client = route_client(h)
    post = lambda value: client.post("/mantella", json=value).json()
    assert post(request(C.KEY_REQUESTTYPE_INIT)) == {C.KEY_REPLYTYPE: C.KEY_REPLYTTYPE_INITCOMPLETED}
    assert post(start_request())[C.KEY_REPLYTYPE] == C.KEY_REPLYTTYPE_STARTCONVERSATIONCOMPLETED
    h.talk.start_conversation.assert_called_once()
    h.talk.continue_conversation.return_value = (C.KEY_REPLYTYPE_NPCTALK, line(actions=[{"identifier": FOLLOW}]))
    assert post(request())[C.KEY_REPLYTYPE_NPCTALK][C.KEY_ACTOR_ACTIONS] == [FOLLOW]
    h.talk.continue_conversation.return_value = (C.KEY_REPLYTYPE_PLAYERTALK, None)
    assert post(request()) == {C.KEY_REPLYTYPE: C.KEY_REPLYTYPE_PLAYERTALK}
    h.talk.process_player_input.return_value = ("events transcript", True, None)
    player = request(C.KEY_REQUESTTYPE_PLAYERINPUT, **{C.KEY_REQUESTTYPE_PLAYERINPUT: "hello"})
    assert post(player) == {C.KEY_REPLYTYPE: C.KEY_REQUESTTYPE_TTS, C.KEY_TRANSCRIBE: "events transcript"}
    h.talk.process_player_input.return_value = ("hello", False, None)
    assert post(player) == {C.KEY_REPLYTYPE: C.KEY_REPLYTYPE_NPCTALK}
    timestamp = "2026-11-04T21:00:00"
    assert post(request(C.KEY_REQUESTTYPE_ENDCONVERSATION, **{C.KEY_ENDCONVERSATION_TIMESTAMP: timestamp})) == {
        C.KEY_REPLYTYPE: C.KEY_REPLYTYPE_ENDCONVERSATION}
    h.talk.end.assert_called_once_with(timestamp)
    assert not route._mantella_route__package_protocol.active
    assert route._mantella_route__package_protocol.participants == {}


def test_actual_keyword_action_no_fake_voice_and_modern_negative(harness):
    h = harness
    _, modern = route_client(h, modern=True)
    assert modern.post("/mantella", json=start_request()).json()[C.KEY_REPLYTYPE] == C.KEY_REPLYTTYPE_STARTCONVERSATIONCOMPLETED
    h.talk.process_player_input.return_value = ("follow", False, None)
    command = request(C.KEY_REQUESTTYPE_PLAYERINPUT, **{C.KEY_REQUESTTYPE_PLAYERINPUT: "follow"})
    baseline = modern.post("/mantella", json=command).json()
    assert baseline[C.KEY_REPLYTYPE] == C.KEY_REPLYTYPE_NPCACTION  # actual missing legacy handler
    assert baseline[C.KEY_REPLYTYPE_NPCACTION][C.KEY_ACTOR_ACTIONS] == [{"identifier": FOLLOW}]
    _, legacy = route_client(h)
    legacy.post("/mantella", json=start_request())
    adapted = legacy.post("/mantella", json=command).json()
    assert adapted == {C.KEY_REPLYTYPE: C.KEY_REPLYTYPE_NPCTALK,
                       C.KEY_REPLYTYPE_NPCTALK: {C.KEY_ACTOR_SPEAKER: "Piper", C.KEY_ACTOR_ACTIONS: [FOLLOW]}}
    h.game.prepare_sentence_for_game.assert_not_called()
    h.chat.tts.synthesize.assert_not_called()


@pytest.mark.parametrize("after_speech", [False, True])
def test_actual_streamed_speakerless_action_is_refused_even_after_speech(harness, after_speech):
    h = harness
    admit(h)
    if after_speech:
        h.talk.continue_conversation.return_value = (C.KEY_REPLYTYPE_NPCTALK, line())
        assert h.protocol.dispatch(request(), h.config, h.manager)[C.KEY_REPLYTYPE] == C.KEY_REPLYTYPE_NPCTALK
    h.game.prepare_sentence_for_game.reset_mock()
    h.talk.continue_conversation.return_value = (C.KEY_REPLYTYPE_NPCACTION, line(text="", actions=[{"identifier": FOLLOW}]))
    result = h.protocol.dispatch(request(), h.config, h.manager)
    assert_error(result, "validated current speaker")
    h.game.prepare_sentence_for_game.assert_not_called()
    h.chat.tts.synthesize.assert_not_called()
    h.talk.end.assert_called_once()
    assert not h.protocol.active and not h.protocol.participants


def test_multi_npc_keyword_uses_explicit_target_not_last_speaker(harness):
    h = harness
    actors = [actor("Player", "14", True), actor(), actor("Nick", "200")]
    assert h.protocol.dispatch(start_request(actors), h.config, h.manager)[C.KEY_REPLYTYPE] != "error"
    h.talk.continue_conversation.return_value = (C.KEY_REPLYTYPE_NPCTALK, line("Piper"))
    h.protocol.dispatch(request(), h.config, h.manager)
    h.talk.context.npcs_in_conversation.last_added_character = NS(name="Nick")
    h.talk.process_player_input.return_value = ("follow", False, None)
    result = h.protocol.dispatch(request(C.KEY_REQUESTTYPE_PLAYERINPUT), h.config, h.manager)
    assert result[C.KEY_REPLYTYPE_NPCTALK][C.KEY_ACTOR_SPEAKER] == "Nick"


def test_removed_keyword_target_is_refused(harness):
    h = harness
    admit(h)
    h.talk.process_player_input.return_value = ("follow", False, None)
    actors = [actor("Player", "14", True), actor("Nick", "200")]
    result = h.protocol.dispatch(request(C.KEY_REQUESTTYPE_PLAYERINPUT, **{C.KEY_ACTORS: actors}), h.config, h.manager)
    assert_error(result, "validated current speaker")
    h.talk.end.assert_called_once()


@pytest.mark.parametrize("actions", [
    [{"identifier": "unknown"}], [{"identifier": "mantella_npc_attack_other_npc"}],
    [{"identifier": FOLLOW, "arguments": {"source": ["Piper"]}}],
    [{"identifier": FOLLOW, "arguments": {}}], [{"identifier": FOLLOW, "requires_response": True}],
    [FOLLOW], None, {}, [None], [{"identifier": FOLLOW, "metadata": "not discardable"}],
])
def test_actual_continue_rejects_unsupported_or_malformed_actions(harness, actions):
    h = harness
    admit(h)
    sentence = line()
    sentence.content.actions = actions
    h.talk.continue_conversation.return_value = (C.KEY_REPLYTYPE_NPCTALK, sentence)
    assert_error(h.protocol.dispatch(request(), h.config, h.manager))
    h.talk.end.assert_called_once()
    assert sentence.actions == actions


def test_actual_continue_response_required_and_interruption_are_errors(harness):
    h = harness
    admit(h)
    FunctionManager._actions[FOLLOW]["requires_response"] = True
    h.talk.continue_conversation.return_value = (C.KEY_REPLYTYPE_NPCACTION, line(text="", actions=[{"identifier": FOLLOW}]))
    assert_error(h.protocol.dispatch(request(), h.config, h.manager), "Response-required")
    admit(h)
    h.talk.continue_conversation.return_value = (C.KEY_REQUESTTYPE_TTS, None)
    assert_error(h.protocol.dispatch(request(), h.config, h.manager), "interruption")


@pytest.mark.parametrize("field", ["vision_enabled", "use_game_screenshots", "advanced_actions_enabled", "allow_interruption"])
def test_config_admission_before_setup_or_model_invocation(harness, field, monkeypatch):
    h = harness
    setattr(h.config, field, True)
    route, client = route_client(h)
    route._has_route_been_initialized = False
    setup = MagicMock(side_effect=AssertionError("must not set up unsupported capability"))
    monkeypatch.setattr(route, "_setup_route", setup)
    assert_error(client.post("/mantella", json=start_request()).json(), field)
    setup.assert_not_called()
    h.talk.start_conversation.assert_not_called()
    h.chat.tts.synthesize.assert_not_called()


@pytest.mark.parametrize("input_type", [C.KEY_INPUTTYPE_MIC, C.KEY_INPUTTYPE_PTT, "unknown", False])
def test_mic_ptt_rejected_before_real_start(harness, input_type):
    h = harness
    value = start_request()
    value[C.KEY_INPUTTYPE] = input_type
    assert_error(h.protocol.dispatch(value, h.config, h.manager), "Microphone/PTT")
    h.talk.start_conversation.assert_not_called()
    assert h.manager._GameStateManager__stt is None


@pytest.mark.parametrize("configured", [[basic_action("mantella_npc_vision")], [basic_action(requires_response=True)]])
def test_unsupported_configured_action_does_not_get_silently_disabled(harness, configured):
    h = harness
    h.config.actions = configured
    assert_error(h.protocol.dispatch(start_request(), h.config, h.manager), "action")
    assert h.config.actions is configured
    h.talk.start_conversation.assert_not_called()
    h.client.enable_vision_for_next_call.assert_not_called()


@pytest.mark.parametrize("extra", [[C.ACTION_RELOADCONVERSATION], ["unknown"], "reload", None])
def test_extra_actions_reload_explicit_error_not_ignored(harness, extra):
    h = harness
    admit(h)
    h.talk.continue_conversation.reset_mock()
    result = h.protocol.dispatch(request(**{C.KEY_REQUEST_EXTRA_ACTIONS: extra}), h.config, h.manager)
    assert_error(result, "Extra request actions")
    h.talk.continue_conversation.assert_not_called()
    h.talk.reload_conversation.assert_not_called()
    h.talk.end.assert_called_once()


@pytest.mark.parametrize("reply", [
    {}, None, {C.KEY_REPLYTYPE: "new_reply"}, {C.KEY_REPLYTYPE: C.KEY_REPLYTYPE_INTERRUPTED},
    {C.KEY_REPLYTYPE: C.KEY_REQUESTTYPE_TTS, C.KEY_TRANSCRIBE: 12},
    {C.KEY_REPLYTYPE: C.KEY_REPLYTYPE_NPCTALK},
    {C.KEY_REPLYTYPE: C.KEY_REPLYTYPE_NPCACTION, C.KEY_REPLYTYPE_NPCACTION: {}},
    {C.KEY_REPLYTYPE: C.KEY_REPLYTYPE_PLAYERTALK, "unknown_behavior": True},
])
def test_malformed_reply_rows_are_refused(harness, reply):
    h = harness
    admit(h)
    with pytest.raises((PackageProtocolError, TypeError)):
        h.protocol.adapt_reply(reply, C.KEY_REQUESTTYPE_CONTINUECONVERSATION)


@pytest.mark.parametrize("flag", [True, 0, "false", None])
def test_start_narrator_requires_valid_supported_bool(harness, flag):
    h = harness
    h.protocol.prepare_request(start_request(), h.config)
    with pytest.raises(PackageProtocolError):
        h.protocol.adapt_reply({C.KEY_REPLYTYPE: C.KEY_REPLYTTYPE_STARTCONVERSATIONCOMPLETED,
                                C.KEY_STARTCONVERSATION_USENARRATOR: flag}, C.KEY_REQUESTTYPE_STARTCONVERSATION)


def test_new_start_resets_speaker_and_end_rejection_resets_state(harness):
    h = harness
    admit(h)
    h.talk.continue_conversation.return_value = (C.KEY_REPLYTYPE_NPCTALK, line())
    h.protocol.dispatch(request(), h.config, h.manager)
    assert h.protocol.last_spoken == ("Piper", "100")
    h.protocol.dispatch(start_request([actor("Nick", "200")]), h.config, h.manager)
    assert h.protocol.last_spoken is None and h.protocol.participants == {"Nick": "200"}
    h.protocol.dispatch(request(C.KEY_REQUESTTYPE_ENDCONVERSATION), h.config, h.manager)
    assert not h.protocol.active and h.protocol.last_spoken is None


def test_cleanup_failure_and_original_persistence_failure_visible(harness):
    h = harness
    admit(h)
    h.talk.end.side_effect = OSError("offline persistence failure")
    h.talk.continue_conversation.return_value = (C.KEY_REPLYTYPE_ENDCONVERSATION, None)
    result = h.protocol.dispatch(request(), h.config, h.manager)
    assert_error(result, "offline persistence failure")
    assert "cleanup failed" in result["mantella_message"]
    assert h.talk.end.call_count == 2  # original real end path + explicit cleanup attempt
    assert not h.protocol.active


def test_backend_error_is_explanatory_and_cleanup_attempted(harness):
    h = harness
    admit(h)
    h.manager._GameStateManager__talk = None
    result = h.protocol.dispatch(request(), h.config, h.manager)
    assert_error(result, "No running conversation")
    assert "cleanup attempted" in result["mantella_message"]


def test_continue_natural_end_actual_manager_cleans_and_resets(harness):
    h = harness
    admit(h)
    h.talk.continue_conversation.return_value = (C.KEY_REPLYTYPE_ENDCONVERSATION, None)
    assert h.protocol.dispatch(request(), h.config, h.manager) == {C.KEY_REPLYTYPE: C.KEY_REPLYTYPE_ENDCONVERSATION}
    h.talk.end.assert_called_once_with()
    assert h.manager._GameStateManager__talk is None
    assert not h.protocol.active


@pytest.mark.parametrize("value", [None, [], {}, "bad", {C.KEY_REQUESTTYPE: "unknown"},
                                   {C.KEY_REQUESTTYPE: C.KEY_REQUESTTYPE_STARTCONVERSATION}])
def test_bad_requests_actual_endpoint_cleanup(harness, value):
    _, client = route_client(harness)
    assert_error(client.post("/mantella", json=value).json())


def test_malformed_json_actual_endpoint(harness):
    _, client = route_client(harness)
    response = client.post("/mantella", content="{invalid", headers={"content-type": "application/json"})
    assert response.status_code == 200
    assert_error(response.json(), "JSONDecodeError")


@pytest.mark.parametrize("key", ["mantella_vision_ready", "mantella_function_enabled"])
def test_game_enabled_vision_function_fields_rejected_before_start(harness, key):
    value = start_request()
    value[C.KEY_CONTEXT] = {C.KEY_CONTEXT_CUSTOMVALUES: {key: True}}
    assert_error(harness.protocol.dispatch(value, harness.config, harness.manager), key)
    harness.talk.start_conversation.assert_not_called()


def test_unknown_request_behavior_not_ignored(harness):
    value = start_request()
    value["mantella_action_result"] = {"ok": True}
    assert_error(harness.protocol.dispatch(value, harness.config, harness.manager), "Unsupported request fields")


def test_duplicate_names_refs_and_missing_speaker_are_not_guessed(harness):
    h = harness
    for actors in ([actor(), actor(ref="200")], [actor(), actor("Nick", "100")]):
        assert_error(h.protocol.dispatch(start_request(actors), h.config, h.manager), "duplicate")
    admit(h)
    payload = {C.KEY_ACTOR_ACTIONS: [{"identifier": FOLLOW}], C.KEY_ACTOR_SPEAKER: "Missing"}
    with pytest.raises(PackageProtocolError, match="validated current speaker"):
        h.protocol.adapt_reply({C.KEY_REPLYTYPE: C.KEY_REPLYTYPE_NPCACTION,
                                C.KEY_REPLYTYPE_NPCACTION: payload}, C.KEY_REQUESTTYPE_PLAYERINPUT)


def test_reloaded_config_checked_in_actual_setup_before_external_construction(harness, monkeypatch):
    h = harness
    route, client = route_client(h)
    route._has_route_been_initialized = False
    def reload_config():
        h.config.vision_enabled = True
    h.config.update_config_loader_with_changed_config_values = reload_config
    module = importlib.import_module("src.http.routes.mantella_route")
    factories = []
    for name in ("Fallout4", "Skyrim", "create_tts", "LLMClient", "SummaryLLMClient", "ChatManager"):
        factory = MagicMock(side_effect=AssertionError("external construction before admission"))
        monkeypatch.setattr(module, name, factory)
        factories.append(factory)
    assert_error(client.post("/mantella", json=start_request()).json(), "vision_enabled")
    for factory in factories:
        factory.assert_not_called()
    h.talk.start_conversation.assert_not_called()


def test_modern_route_does_not_convert_or_restrict_action_payload(harness):
    h = harness
    h.config.vision_enabled = True  # modern route has no legacy restriction
    h.config.advanced_actions_enabled = True
    _, client = route_client(h, modern=True)
    client.post("/mantella", json=start_request())
    actions = [{"identifier": "mantella_npc_attack_other_npc", "arguments": {"target": "Nick"}}]
    h.talk.continue_conversation.return_value = (C.KEY_REPLYTYPE_NPCTALK, line(actions=actions))
    reply = client.post("/mantella", json=request()).json()
    assert reply[C.KEY_REPLYTYPE_NPCTALK][C.KEY_ACTOR_ACTIONS] == actions
    assert strict_legacy_string_array(reply[C.KEY_REPLYTYPE_NPCTALK]) is None


@pytest.mark.parametrize("mutate", [
    lambda p: p.update({C.KEY_ACTOR_DURATION: float("inf")}),
    lambda p: p.update({C.KEY_ACTOR_ISNARRATION: True}),
    lambda p: p.update({C.KEY_ACTOR_VOICEFILE: ""}),
    lambda p: p.update({C.KEY_ACTOR_ACTIONS_REQUIRE_RESPONSE: True}),
    lambda p: p.update({"unsupported_behavior": True}),
    lambda p: p.pop(C.KEY_ACTOR_SPEAKER),
])
def test_spoken_payload_validation_preserves_or_refuses_not_drops(harness, mutate):
    h = harness
    admit(h)
    payload = h.manager.sentence_to_json(line(), 1)
    mutate(payload)
    with pytest.raises(PackageProtocolError):
        h.protocol.adapt_reply({C.KEY_REPLYTYPE: C.KEY_REPLYTYPE_NPCTALK,
                                C.KEY_REPLYTYPE_NPCTALK: payload}, C.KEY_REQUESTTYPE_CONTINUECONVERSATION)


def test_explicit_end_keeps_timestamp_despite_changed_capabilities(harness, monkeypatch):
    h = harness
    route, client = route_client(h)
    client.post("/mantella", json=start_request())
    h.config.vision_enabled = True
    h.config.has_any_config_value_changed = True
    setup = MagicMock(side_effect=AssertionError("end must not recreate manager"))
    monkeypatch.setattr(route, "_setup_route", setup)
    timestamp = "2026-11-04T22:00:00"
    result = client.post("/mantella", json=request(C.KEY_REQUESTTYPE_ENDCONVERSATION,
                         **{C.KEY_ENDCONVERSATION_TIMESTAMP: timestamp})).json()
    assert result == {C.KEY_REPLYTYPE: C.KEY_REPLYTYPE_ENDCONVERSATION}
    h.talk.end.assert_called_once_with(timestamp)
    setup.assert_not_called()
