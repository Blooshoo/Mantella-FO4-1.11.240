"""Opt-in, fail-closed boundary for one *source-reviewed* FO4 package.

This is not PEX verification or a deployment approval. No native/store coercion,
model routing, conversation internals or lifecycle implementation is changed.
"""
from copy import deepcopy
import math
from typing import Any

from src.http.communication_constants import communication_constants as C

PACKAGE_PROTOCOL = "fallout4-aio-0.13.0-573c5a89-text-basic-v1"
PACKAGE_SHA256 = "573c5a8929375fd015b2eb12e3f9be843f9031131f9d0f8902e3cd740c86852f"
BASIC_ACTIONS = frozenset({
    C.ACTION_RELOADCONVERSATION, C.ACTION_ENDCONVERSATION, C.ACTION_REMOVECHARACTER,
    "mantella_npc_offended", "mantella_npc_forgiven", "mantella_npc_follow",
    "mantella_npc_inventory",
})


class PackageProtocolError(ValueError):
    pass


class LegacyPackageProtocol:
    """Text-only, non-narrated basic actions; extra request actions are refused.

    Speakerless action-only replies are refused even after a spoken reply: the
    modern manager has discarded the sentence target, which could differ from
    the last speaker. Explicit keyword-action speakers are membership-validated.
    No last-speaker/last-added fallback and no fabricated speech/audio fields.
    An instance belongs to one route/manager, not a global protocol switch.
    """

    def __init__(self, package_protocol: str):
        if package_protocol != PACKAGE_PROTOCOL:
            raise PackageProtocolError(f"Unknown package protocol: {package_protocol!r}")
        self.reset()

    def reset(self):
        self.active = False
        self.participants: dict[str, str] = {}
        self.npcs: set[str] = set()
        self.last_spoken: tuple[str, str] | None = None

    @staticmethod
    def _require(condition: bool, message: str):
        if not condition:
            raise PackageProtocolError(message)

    def check_config(self, config):
        # Strict identities prevent absent/ambiguous configuration from admitting
        # a capability; do not silently rewrite a user's config or action list.
        self._require(getattr(getattr(config, "game", None), "name", None) == "FALLOUT4",
                      "Only desktop Fallout4 is admitted by this package adapter")
        for field in ("vision_enabled", "use_game_screenshots", "advanced_actions_enabled", "allow_interruption"):
            self._require(getattr(config, field, None) is False,
                          f"Unsupported capability: {field} must explicitly be false")
        self._require(getattr(getattr(config, "narration_handling", None), "name", None)
                      == "CUT_NARRATIONS", "Restricted text mode requires Cut narrations")
        actions = getattr(config, "actions", None)
        self._require(isinstance(actions, list), "Configured actions must be an explicit list")
        for action in actions:
            identifier = getattr(action, "identifier", None)
            self._require(identifier in BASIC_ACTIONS,
                          f"Unsupported configured action: {identifier!r}; no actions were disabled automatically")
            self._require(getattr(action, "requires_response", None) is False,
                          f"Response-required action is unsupported: {identifier}")

    def _roster(self, actors):
        self._require(isinstance(actors, list) and bool(actors), "An explicit nonempty actor roster is required")
        participants, npcs, refs = {}, set(), set()
        for actor in actors:
            self._require(isinstance(actor, dict), "Actor roster entries must be objects")
            name, ref, is_player = (actor.get(C.KEY_ACTOR_NAME), actor.get(C.KEY_ACTOR_REFID),
                                    actor.get(C.KEY_ACTOR_ISPLAYER))
            self._require(isinstance(name, str) and bool(name.strip()), "Actor name is missing")
            self._require(type(ref) in (str, int) and str(ref) != "", "Actor reference identity is missing")
            self._require(type(is_player) is bool, "Actor player flag must be boolean")
            self._require(name not in participants and str(ref) not in refs,
                          "Ambiguous duplicate actor name/reference; cannot target legacy actions safely")
            participants[name] = str(ref)
            refs.add(str(ref))
            if not is_player:
                npcs.add(name)
        self._require(bool(npcs), "At least one explicitly identified NPC is required")
        self.participants, self.npcs = participants, npcs
        if self.last_spoken and participants.get(self.last_spoken[0]) != self.last_spoken[1]:
            self.last_spoken = None

    def prepare_request(self, request: Any, config) -> dict:
        self._require(isinstance(request, dict) and bool(request), "Request must be a nonempty JSON object")
        allowed = {C.KEY_REQUESTTYPE, C.KEY_INPUTTYPE, C.KEY_ACTORS, C.KEY_CONTEXT,
                   C.KEY_STARTCONVERSATION_WORLDID, C.KEY_CONTINUECONVERSATION_TOPICINFOFILE,
                   C.KEY_REQUESTTYPE_PLAYERINPUT, C.KEY_ENDCONVERSATION_TIMESTAMP,
                   C.KEY_REQUEST_EXTRA_ACTIONS, C.KEY_INPUT_NAMESINCONVERSATION}
        self._require(not (set(request) - allowed),
                      f"Unsupported request fields: {sorted(set(request) - allowed)}")
        kind = request.get(C.KEY_REQUESTTYPE)
        self._require(kind in {C.KEY_REQUESTTYPE_INIT, C.KEY_REQUESTTYPE_STARTCONVERSATION,
                               C.KEY_REQUESTTYPE_CONTINUECONVERSATION, C.KEY_REQUESTTYPE_PLAYERINPUT,
                               C.KEY_REQUESTTYPE_ENDCONVERSATION}, f"Unsupported request type: {kind!r}")
        # End must remain available for cleanup, including timestamp forwarding.
        if kind == C.KEY_REQUESTTYPE_ENDCONVERSATION:
            self._require(set(request) <= {C.KEY_REQUESTTYPE, C.KEY_ENDCONVERSATION_TIMESTAMP},
                          "End request may contain only its request type and timestamp")
            return deepcopy(request)
        self.check_config(config)
        self._require(request.get(C.KEY_INPUTTYPE, C.KEY_INPUTTYPE_TEXT) == C.KEY_INPUTTYPE_TEXT,
                      "Microphone/PTT/barge-in input is unsupported; select text input")
        extra = request.get(C.KEY_REQUEST_EXTRA_ACTIONS, [])
        self._require(type(extra) is list and not extra,
                      "Extra request actions (including reload) are unsupported, not ignored")
        def reject_enabled_capabilities(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"mantella_vision_ready", "mantella_function_enabled"}:
                        self._require(child is False, f"Unsupported game capability: {key}")
                    reject_enabled_capabilities(child)
            elif isinstance(value, list):
                for child in value:
                    reject_enabled_capabilities(child)
        reject_enabled_capabilities(request)
        if kind == C.KEY_REQUESTTYPE_STARTCONVERSATION:
            self.reset()
            self._require(request.get(C.KEY_INPUTTYPE) == C.KEY_INPUTTYPE_TEXT,
                          "Start must explicitly select text input (no inherited microphone state)")
            self._roster(request.get(C.KEY_ACTORS))
        elif kind in {C.KEY_REQUESTTYPE_CONTINUECONVERSATION, C.KEY_REQUESTTYPE_PLAYERINPUT}:
            self._require(self.active, "No adapter-admitted conversation is active")
            if C.KEY_ACTORS in request:
                self._roster(request[C.KEY_ACTORS])
        elif kind == C.KEY_REQUESTTYPE_INIT:
            self._require(not self.active, "Initialize during a conversation is unsupported")
        if C.KEY_REQUESTTYPE_PLAYERINPUT in request:
            self._require(isinstance(request[C.KEY_REQUESTTYPE_PLAYERINPUT], str), "Player input must be text")
        if C.KEY_CONTINUECONVERSATION_TOPICINFOFILE in request:
            self._require(type(request[C.KEY_CONTINUECONVERSATION_TOPICINFOFILE]) is int
                          and request[C.KEY_CONTINUECONVERSATION_TOPICINFOFILE] in (1, 2),
                          "TopicInfo slot must be integer 1 or 2")
        return deepcopy(request)

    def _actions(self, actions):
        self._require(type(actions) is list, "Actions must be an object array")
        result = []
        for action in actions:
            self._require(type(action) is dict and set(action) == {"identifier"},
                          "Only parameterless identifier-only actions are supported; arguments/metadata cannot be dropped")
            identifier = action["identifier"]
            self._require(isinstance(identifier, str) and identifier in BASIC_ACTIONS,
                          f"Unsupported action: {identifier!r}")
            result.append(identifier)
        return result

    def adapt_reply(self, reply: Any, request_type: str) -> dict:
        self._require(type(reply) is dict, "Backend reply must be an object")
        result = deepcopy(reply)
        kind = result.get(C.KEY_REPLYTYPE)
        allowed = {
            C.KEY_REPLYTTYPE_INITCOMPLETED: {C.KEY_REPLYTYPE},
            C.KEY_REPLYTTYPE_STARTCONVERSATIONCOMPLETED: {C.KEY_REPLYTYPE, C.KEY_STARTCONVERSATION_USENARRATOR},
            C.KEY_REPLYTYPE_NPCTALK: {C.KEY_REPLYTYPE, C.KEY_REPLYTYPE_NPCTALK},
            C.KEY_REPLYTYPE_NPCACTION: {C.KEY_REPLYTYPE, C.KEY_REPLYTYPE_NPCACTION},
            C.KEY_REPLYTYPE_PLAYERTALK: {C.KEY_REPLYTYPE},
            C.KEY_REQUESTTYPE_TTS: {C.KEY_REPLYTYPE, C.KEY_TRANSCRIBE},
            C.KEY_REPLYTYPE_ENDCONVERSATION: {C.KEY_REPLYTYPE},
            "error": {C.KEY_REPLYTYPE, "mantella_message"},
        }
        self._require(kind in allowed, f"Unsupported backend reply: {kind!r}; interruption cannot be synthesized")
        self._require(not (set(result) - allowed[kind]), "Unsupported backend reply fields cannot be dropped")
        if kind == C.KEY_REPLYTTYPE_INITCOMPLETED:
            self._require(request_type == C.KEY_REQUESTTYPE_INIT, "Unexpected initialize reply")
        elif kind == C.KEY_REPLYTTYPE_STARTCONVERSATIONCOMPLETED:
            self._require(request_type == C.KEY_REQUESTTYPE_STARTCONVERSATION, "Unexpected start reply")
            self._require(result.get(C.KEY_STARTCONVERSATION_USENARRATOR) is False,
                          "A valid false narrator flag is required in restricted text mode")
            self.active = True
        elif kind == C.KEY_REQUESTTYPE_TTS:
            self._require(isinstance(result.get(C.KEY_TRANSCRIBE), str), "Transcript envelope must contain text")
        elif kind == "error":
            self._require(isinstance(result.get("mantella_message"), str), "Error message must be text")
            raise PackageProtocolError("Backend error: " + result["mantella_message"])
        elif kind in {C.KEY_REPLYTYPE_NPCACTION, C.KEY_REPLYTYPE_NPCTALK}:
            is_action = kind == C.KEY_REPLYTYPE_NPCACTION
            if not is_action and C.KEY_REPLYTYPE_NPCTALK not in result:
                self._require(request_type == C.KEY_REQUESTTYPE_PLAYERINPUT,
                              "Empty npc_talk is only a player-input acknowledgement")
                return result
            payload = result.get(kind)
            self._require(type(payload) is dict, "NPC payload must be an object")
            payload_keys = {C.KEY_ACTOR_SPEAKER, C.KEY_ACTOR_ACTIONS, C.KEY_ACTOR_ACTIONS_REQUIRE_RESPONSE}
            if not is_action:
                payload_keys |= {C.KEY_ACTOR_LINETOSPEAK, C.KEY_ACTOR_ISNARRATION, C.KEY_ACTOR_VOICEFILE,
                                 C.KEY_ACTOR_DURATION, C.KEY_CONTINUECONVERSATION_TOPICINFOFILE}
            self._require(not (set(payload) - payload_keys), "Unsupported NPC payload fields cannot be dropped")
            if C.KEY_ACTOR_ACTIONS_REQUIRE_RESPONSE in payload:
                self._require(payload[C.KEY_ACTOR_ACTIONS_REQUIRE_RESPONSE] is False,
                              "Response-required actions are unsupported")
            actions = self._actions(payload.get(C.KEY_ACTOR_ACTIONS, []))
            speaker = payload.get(C.KEY_ACTOR_SPEAKER)
            self._require(isinstance(speaker, str) and speaker in self.participants,
                          "A validated current speaker is required; no arbitrary action target is chosen")
            if actions:
                self._require(speaker in self.npcs, "Actions must target an explicitly identified current NPC")
            payload[C.KEY_ACTOR_ACTIONS] = actions
            payload.pop(C.KEY_ACTOR_ACTIONS_REQUIRE_RESPONSE, None)  # explicit false only; no response behavior
            if is_action:
                self._require(bool(actions), "Empty action-only reply is unsupported")
                # Deliberately omit line/voice/duration: package executes action
                # before checking its missing-line sentinel. Never request Say/TTS.
                result = {C.KEY_REPLYTYPE: C.KEY_REPLYTYPE_NPCTALK,
                          C.KEY_REPLYTYPE_NPCTALK: {C.KEY_ACTOR_SPEAKER: speaker, C.KEY_ACTOR_ACTIONS: actions}}
            else:
                self._require(isinstance(payload.get(C.KEY_ACTOR_LINETOSPEAK), str)
                              and bool(payload[C.KEY_ACTOR_LINETOSPEAK].strip()), "Spoken reply requires nonempty text")
                self._require(payload.get(C.KEY_ACTOR_ISNARRATION) is False, "Narrated replies are unsupported")
                self._require(isinstance(payload.get(C.KEY_ACTOR_VOICEFILE), str)
                              and bool(payload[C.KEY_ACTOR_VOICEFILE]), "Spoken reply requires its actual voice file")
                duration = payload.get(C.KEY_ACTOR_DURATION)
                self._require(type(duration) in (int, float) and math.isfinite(duration) and duration >= 0,
                              "Spoken reply duration must be finite and nonnegative")
                self._require(type(payload.get(C.KEY_CONTINUECONVERSATION_TOPICINFOFILE)) is int
                              and payload[C.KEY_CONTINUECONVERSATION_TOPICINFOFILE] in (1, 2),
                              "Spoken reply requires TopicInfo slot 1 or 2")
                if speaker in self.npcs:
                    self.last_spoken = (speaker, self.participants[speaker])
        elif kind == C.KEY_REPLYTYPE_ENDCONVERSATION:
            self.reset()
        return result

    def failure(self, error: Exception, manager=None) -> dict:
        message = f"Package protocol refused: {type(error).__name__}: {error}"
        self.reset()
        if manager is not None:
            try:
                cleanup = manager.end_conversation({})
                if not isinstance(cleanup, dict) or cleanup.get(C.KEY_REPLYTYPE) != C.KEY_REPLYTYPE_ENDCONVERSATION:
                    message += "; cleanup attempt returned no end acknowledgement"
                else:
                    message += "; cleanup attempted (not proof of audio/persistence correctness)"
            except Exception as cleanup_error:
                message += f"; cleanup failed: {type(cleanup_error).__name__}: {cleanup_error}"
        else:
            message += "; no game manager available for cleanup"
        return {C.KEY_REPLYTYPE: "error", "mantella_message": message}

    def dispatch(self, request: Any, config, manager) -> dict:
        """Call actual production methods; no private manager/thread mutation."""
        try:
            prepared = self.prepare_request(request, config)
            kind = prepared[C.KEY_REQUESTTYPE]
            if kind == C.KEY_REQUESTTYPE_INIT:
                reply = {C.KEY_REPLYTYPE: C.KEY_REPLYTTYPE_INITCOMPLETED}
            else:
                methods = {C.KEY_REQUESTTYPE_STARTCONVERSATION: manager.start_conversation,
                           C.KEY_REQUESTTYPE_CONTINUECONVERSATION: manager.continue_conversation,
                           C.KEY_REQUESTTYPE_PLAYERINPUT: manager.player_input,
                           C.KEY_REQUESTTYPE_ENDCONVERSATION: manager.end_conversation}
                reply = methods[kind](prepared)
            return self.adapt_reply(reply, kind)
        except Exception as error:
            return self.failure(error, manager)
