"""Cross-component contracts of the paired candidate: pass2 (65c0a3d) + local atomic-name fix + opt-in legacy
0.13.0 package adapter. Offline; no network, model, TTS service or game.

What is covered here and nowhere else:
* O-10 (pass2) x adapter: the end reply that answers the line request after a goodbye is held on the PACKAGE
  route too (real route, real GameStateManager, real Conversation/ChatManager, stub TTS wav), the goodbye's R-06
  end action reaches the package as the string identifier its compiled ProcessNpcSpeak reads with
  getStringArray, and the hold does not block other requests.
* The hold is always consumed and only applied to an adapted END reply: never to a refused/non-end reply.
* The merged modern route still holds (merge did not drop pass2's branch).
* R-04c publication x local fix: the real conversation_log history writer publishes a valid long NPC name.
The PEX evidence that the package reads actions with getStringArray and raises actions before the missing-line
check is in the candidate's evidence/ directory, not asserted here.
"""
import asyncio
import json
import time
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI

import src.http.routes.mantella_route as route_module
from src.config.definitions.game_definitions import GameEnum
from src.config.definitions.llm_definitions import NarrationHandlingEnum
from src.conversation.conversation import Conversation
from src.conversation.conversation_log import conversation_log
from src.game_manager import GameStateManager
from src.http.communication_constants import communication_constants as C
from src.http.legacy_package_protocol import PACKAGE_PROTOCOL
from src.http.routes.mantella_route import mantella_route

from tests.lifecycle_support import chat_manager_factory, wav_bytes  # noqa: F401
from tests.test_legacy_package_protocol import (  # noqa: F401  (fixture re-export)
    assert_error, harness, line, request, route_client, start_request,
)

GOODBYE_S = 0.8
CONTINUE = {C.KEY_REQUESTTYPE: C.KEY_REQUESTTYPE_CONTINUECONVERSATION, C.KEY_CONTINUECONVERSATION_TOPICINFOFILE: 1}
INIT = {C.KEY_REQUESTTYPE: C.KEY_REQUESTTYPE_INIT}


def strict_package_config():
    return NS(game=GameEnum.FALLOUT4, narration_handling=NarrationHandlingEnum.CUT_NARRATIONS,
              vision_enabled=False, use_game_screenshots=False, advanced_actions_enabled=False,
              allow_interruption=False, actions=[], show_http_debug_messages=False)


@pytest.fixture
def package_goodbye(chat_manager_factory, default_config, english_language_info, tmp_path, monkeypatch,
                    example_characters_pc_to_npc):
    """Real Conversation whose end sequence has started, behind the real PACKAGE /mantella route."""
    saved = MagicMock()
    monkeypatch.setattr("src.conversation.conversation.conversation_log.save_conversation_log", saved)
    default_config.wait_time_buffer = 0.0
    wav = tmp_path / "goodbye.wav"
    wav.write_bytes(wav_bytes(GOODBYE_S))
    manager, _ = chat_manager_factory(str(wav))
    context = MagicMock()
    context.config = default_config
    context.npcs_in_conversation = example_characters_pc_to_npc
    context.have_actors_changed = False
    llm = MagicMock()
    llm.is_too_long.return_value = False
    rememberer = MagicMock()
    talk = Conversation(context, manager, rememberer, llm, None, False, False)
    game = MagicMock()
    # A bare MagicMock game serializes line_to_speak as {} (the pass2 modern fixture never inspects it); the
    # adapter rightly refuses that, so give the game the identity text hook a real game applies.
    game.modify_sentence_text_for_game.side_effect = lambda text: text
    gsm = GameStateManager(game, manager, default_config, english_language_info, llm)
    gsm._GameStateManager__talk = talk
    talk.initiate_end_sequence()

    route = mantella_route(strict_package_config(), english_language_info, package_protocol=PACKAGE_PROTOCOL)
    route._mantella_route__game = gsm
    monkeypatch.setattr(route, "_can_route_be_used", lambda: True)
    protocol = route._mantella_route__package_protocol
    # Admit the real roster as a package start would (start itself is covered by the adapter suite).
    roster = [{C.KEY_ACTOR_NAME: c.name, C.KEY_ACTOR_REFID: str(c.ref_id), C.KEY_ACTOR_ISPLAYER: c.is_player_character}
              for c in example_characters_pc_to_npc.get_all_characters()]
    protocol._roster(roster)
    protocol.active = True
    npc = next(c.name for c in example_characters_pc_to_npc.get_all_characters() if not c.is_player_character)
    app = FastAPI()
    route.add_route_to_server(app)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mantella")
    return NS(client=client, gsm=gsm, rememberer=rememberer, saved=saved, protocol=protocol, npc=npc)


async def post(client, body):
    response = await client.post("/mantella", json=body)
    return response.json(), time.monotonic()


def test_package_route_holds_end_reply_after_goodbye_and_sends_string_end_action(package_goodbye):
    s = package_goodbye

    async def scenario():
        goodbye, handed_out = await post(s.client, dict(CONTINUE))
        end_task = asyncio.create_task(post(s.client, dict(CONTINUE)))
        await asyncio.sleep(0.1)
        other, other_done = await post(s.client, dict(INIT))
        talk_dropped = s.gsm._GameStateManager__talk is None
        saves = (s.rememberer.save_conversation_state.call_count, s.saved.call_count)
        end, end_done = await end_task
        await s.client.aclose()
        return goodbye, handed_out, other, other_done, talk_dropped, saves, end, end_done

    goodbye, handed_out, other, other_done, talk_dropped, saves, end, end_done = asyncio.run(scenario())
    assert goodbye[C.KEY_REPLYTYPE] == C.KEY_REPLYTYPE_NPCTALK, goodbye
    payload = goodbye[C.KEY_REPLYTYPE_NPCTALK]
    assert payload[C.KEY_ACTOR_SPEAKER] == s.npc
    assert isinstance(payload[C.KEY_ACTOR_LINETOSPEAK], str) and payload[C.KEY_ACTOR_LINETOSPEAK].strip()
    # R-06 goodbye end action, converted to the string array the compiled package reads
    assert payload[C.KEY_ACTOR_ACTIONS] == [C.ACTION_ENDCONVERSATION]
    assert all(type(a) is str for a in payload[C.KEY_ACTOR_ACTIONS])
    assert payload[C.KEY_ACTOR_DURATION] == pytest.approx(GOODBYE_S, abs=0.05)
    assert other == {C.KEY_REPLYTYPE: C.KEY_REPLYTTYPE_INITCOMPLETED}
    assert talk_dropped and saves == (1, 1)
    assert end == {C.KEY_REPLYTYPE: C.KEY_REPLYTYPE_ENDCONVERSATION}
    held = end_done - handed_out
    assert GOODBYE_S - 0.05 <= held < GOODBYE_S + 1.0, held
    assert other_done < end_done, "held package end reply blocked another request"
    assert s.protocol.active is False


def test_package_route_no_hold_once_goodbye_has_played(package_goodbye):
    s = package_goodbye

    async def scenario():
        await post(s.client, dict(CONTINUE))
        await asyncio.sleep(GOODBYE_S + 0.05)
        start = time.monotonic()
        end, done = await post(s.client, dict(CONTINUE))
        await s.client.aclose()
        return end, done - start

    end, took = asyncio.run(scenario())
    assert end == {C.KEY_REPLYTYPE: C.KEY_REPLYTYPE_ENDCONVERSATION}
    assert took < 0.3


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []

    async def fake_sleep(seconds):
        recorded.append(seconds)
    monkeypatch.setattr(route_module.asyncio, "sleep", fake_sleep)
    return recorded


def test_hold_consumed_but_not_applied_to_non_end_package_reply(harness, sleeps, monkeypatch):
    h = harness
    route, client = route_client(h)
    assert client.post("/mantella", json=start_request()).json()[C.KEY_REPLYTYPE] == \
        C.KEY_REPLYTTYPE_STARTCONVERSATIONCOMPLETED
    pops = []
    monkeypatch.setattr(h.manager, "pop_reply_hold_seconds", lambda: pops.append(1) or 5.0)
    h.talk.continue_conversation.return_value = (C.KEY_REPLYTYPE_NPCTALK, line())
    reply = client.post("/mantella", json=request()).json()
    assert reply[C.KEY_REPLYTYPE] == C.KEY_REPLYTYPE_NPCTALK
    assert pops == [1] and sleeps == []


def test_refused_package_reply_is_never_held(harness, sleeps, monkeypatch):
    h = harness
    route, client = route_client(h)
    client.post("/mantella", json=start_request())
    monkeypatch.setattr(h.manager, "pop_reply_hold_seconds", lambda: 5.0)
    h.talk.continue_conversation.return_value = (
        C.KEY_REPLYTYPE_NPCTALK, line(actions=[{"identifier": "mantella_npc_follow", "arguments": {}}]))
    reply = client.post("/mantella", json=request()).json()
    assert_error(reply, "arguments/metadata cannot be dropped")
    assert "cleanup attempted" in reply["mantella_message"]
    assert sleeps == []


def test_package_end_from_continue_uses_real_manager_hold(harness, sleeps):
    h = harness
    route, client = route_client(h)
    client.post("/mantella", json=start_request())
    h.talk.continue_conversation.return_value = (C.KEY_REPLYTYPE_ENDCONVERSATION, None)
    h.talk.end_reply_hold_seconds.return_value = 0.4
    reply = client.post("/mantella", json=request()).json()
    assert reply == {C.KEY_REPLYTYPE: C.KEY_REPLYTYPE_ENDCONVERSATION}
    assert sleeps == [0.4]
    h.talk.end.assert_called_once()
    assert h.manager._GameStateManager__talk is None
    assert h.manager.pop_reply_hold_seconds() == 0.0
    assert route._mantella_route__package_protocol.active is False


def test_merged_modern_route_still_holds(harness, sleeps):
    h = harness
    route, client = route_client(h, modern=True)
    h.manager.start_conversation(start_request())
    h.talk.continue_conversation.return_value = (C.KEY_REPLYTYPE_ENDCONVERSATION, None)
    h.talk.end_reply_hold_seconds.return_value = 0.4
    reply = client.post("/mantella", json=request()).json()
    assert reply == {C.KEY_REPLYTYPE: C.KEY_REPLYTYPE_ENDCONVERSATION}
    assert sleeps == [0.4]


# Exact config.ini disabled_actions value documented for the restricted package mode: everything except the four
# parameterless legacy actions the compiled 0.13.0 package handles (Inventory is excluded: requires_response).
RESTRICTED_ENABLED = {"Attack", "StandDown", "EndConversation", "Follow"}


def _all_action_names():
    from src.config.definitions.action_definitions import _scan_action_names
    return _scan_action_names()


@pytest.fixture
def restore_actions():
    from src.actions.function_manager import FunctionManager
    saved = dict(FunctionManager._actions)
    yield FunctionManager
    FunctionManager._actions.clear()
    FunctionManager._actions.update(saved)


def test_real_default_action_set_is_refused_by_package_preflight(restore_actions):
    """Real setup.py path: load_all_actions(config default disabled_actions) + get_legacy_actions. The default
    leaves non-basic prompt actions (Barter, Flee, Listen, MoveTo, response-required Inventory) enabled, so the
    package route must refuse rather than silently strip them."""
    from src.config.definitions.action_definitions import ActionDefinitions
    from src.http.legacy_package_protocol import LegacyPackageProtocol, PackageProtocolError
    FM = restore_actions
    FM.load_all_actions(disabled_actions=ActionDefinitions.get_disabled_actions_config_value().value)
    config = strict_package_config()
    config.actions = FM.get_legacy_actions()
    with pytest.raises(PackageProtocolError, match="Unsupported configured action|Response-required"):
        LegacyPackageProtocol(PACKAGE_PROTOCOL).check_config(config)


def test_documented_restricted_disabled_actions_pass_package_preflight(restore_actions):
    from src.http.legacy_package_protocol import BASIC_ACTIONS, LegacyPackageProtocol
    FM = restore_actions
    names = _all_action_names()
    assert RESTRICTED_ENABLED <= set(names)
    disabled = [n for n in names if n not in RESTRICTED_ENABLED]
    FM.load_all_actions(disabled_actions=disabled)
    config = strict_package_config()
    config.actions = FM.get_legacy_actions()
    identifiers = sorted(a.identifier for a in config.actions)
    assert identifiers == sorted([C.ACTION_ENDCONVERSATION, "mantella_npc_follow", "mantella_npc_forgiven",
                                  "mantella_npc_offended"])
    assert set(identifiers) <= BASIC_ACTIONS and not any(a.requires_response for a in config.actions)
    LegacyPackageProtocol(PACKAGE_PROTOCOL).check_config(config)  # must not raise
    print("RESTRICTED_DISABLED_ACTIONS=" + ", ".join(disabled))


@pytest.mark.parametrize("name", ["N" * 240, "語" * 80])
def test_real_history_writer_publishes_valid_long_npc_name(tmp_path, monkeypatch, name):
    """Directory '<name> - <ref>' and file '<name>.json' are valid (<=255 bytes); the published 65c0a3d staging
    name would exceed NAME_MAX. Uses the real save_conversation_log + atomic publication."""
    assert len(f"{name}.json".encode()) <= 255 and len(f".{name}.json.0123456789ab.tmp".encode()) > 255
    monkeypatch.setattr(conversation_log, "game_path", str(tmp_path))
    character = NS(name=name, ref_id="0001A4D7")
    conversation_log.save_conversation_log(character, [{"role": "user", "content": "hi"}], "world")
    conversation_log.save_conversation_log(character, [{"role": "user", "content": "again"}], "world")
    folder = tmp_path / "world" / f"{name} - 0001A4D7"
    files = list(folder.iterdir())
    assert files == [folder / f"{name}.json"]
    assert json.loads(files[0].read_text(encoding="utf-8")) == [[{"role": "user", "content": "hi"}],
                                                                [{"role": "user", "content": "again"}]]
