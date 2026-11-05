"""R-04b: a conversation is persisted exactly once - and a FAILED save is retried, without duplicate records.

R-04 (0b2af2e) made Conversation.end() save only on the first call by testing __has_already_ended, which the
first call sets BEFORE saving. When that first save raised (disk full, a locked history/summary file), every
later end() skipped the save: the conversation was never persisted. Before R-04 a failed save was retried
(GameStateManager keeps the conversation until end() returns, so the next end / start ends it again), but every
retry also re-appended the records the failed attempt HAD written, re-ran the summary LLM call and lost the
pending memory shares the first attempt had already cleared.

Now "saved" is its own state, set only once the final save completed; the retry writes only what the failed
attempt did not (FinalSaveProgress: logs, generated summaries, summaries written, shares). These tests drive the
real Conversation, the real conversation_log and the real Summaries persistence through GameStateManager's
natural end (line request after the goodbye) and explicit end (end request); only the summary LLM call is mocked
and file writes are fault-injected.
"""
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from src.conversation.context import Context
from src.conversation.conversation import Conversation
from src.game_manager import GameStateManager
from src.http.communication_constants import communication_constants as comm_consts
from src.llm.messages import AssistantMessage, UserMessage
from src.remember.summaries import Summaries

WORLD = "W"
SUMMARY = "They talked about the weather.\n\n"
CONTINUE = {comm_consts.KEY_REQUESTTYPE: comm_consts.KEY_REQUESTTYPE_CONTINUECONVERSATION,
            comm_consts.KEY_CONTINUECONVERSATION_TOPICINFOFILE: 1}


class DiskFull:
    """open() for the modules under test: the next `budget` writes to a matching path raise ENOSPC."""

    def __init__(self, monkeypatch):
        self.rules: list[list] = []  # [substring, budget]
        real_open = open

        def faulty_open(path, mode="r", *a, **kw):
            if "w" in mode or "a" in mode:
                for rule in self.rules:
                    if rule[0] in os.path.basename(str(path)) and rule[1] > 0:
                        rule[1] -= 1
                        raise OSError(28, "No space left on device", str(path))
            return real_open(path, mode, *a, **kw)
        monkeypatch.setattr("src.remember.summaries.open", faulty_open, raising=False)
        monkeypatch.setattr("src.conversation.conversation_log.open", faulty_open, raising=False)

    def fail(self, name_part: str, times: int = 1):
        self.rules.append([name_part, times])


@pytest.fixture
def world(skyrim, default_config, english_language_info, monkeypatch, example_skyrim_player_character,
          example_skyrim_npc_character, another_example_skyrim_npc_character):
    """A finished pc_to_npc conversation with two NPCs, inside a GameStateManager."""
    default_config.conversation_summary_enabled = True
    default_config.memory_prompt_datetime_prefix = False
    client = MagicMock()
    client.token_limit = 100_000
    client.get_count_tokens.side_effect = lambda text: 1
    rememberer = Summaries(skyrim, default_config, client, english_language_info["language"])
    summarize = MagicMock(return_value=SUMMARY)
    monkeypatch.setattr(rememberer, "summarize_conversation", summarize)
    context = Context(WORLD, default_config, client, rememberer, english_language_info)
    context.add_or_update_characters([example_skyrim_player_character, example_skyrim_npc_character,
                                      another_example_skyrim_npc_character], message_count=0)
    llm = MagicMock()
    llm.is_too_long.return_value = False
    talk = Conversation(context, MagicMock(), rememberer, llm, None, False, False)
    thread = talk._Conversation__messages
    for i in range(4):
        thread.add_message(UserMessage(default_config, f"Message {i}", "Player"))
        thread.add_message(AssistantMessage(default_config))
    gsm = GameStateManager(MagicMock(), MagicMock(), default_config, english_language_info, llm)
    gsm._GameStateManager__talk = talk
    npcs = [example_skyrim_npc_character, another_example_skyrim_npc_character]
    return gsm, talk, context, summarize, skyrim.conversation_folder_path, npcs


def logs(folder, npc) -> int:
    """Conversations recorded in this NPC's history file."""
    for path in (os.path.join(folder, WORLD, npc.name, f"{npc.name}.json"),
                 os.path.join(folder, WORLD, f"{npc.name} - {npc.ref_id}", f"{npc.name}.json")):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return len(json.load(f))
    return 0


def summaries(folder, name, ref_id, needle=SUMMARY.strip()) -> int:
    path = os.path.join(folder, WORLD, f"{name} - {ref_id}", f"{name}_summary_1.txt")
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        return f.read().count(needle)


def end_naturally(gsm, talk):
    """The goodbye was delivered and the game asks for the next line (R-06 path)."""
    talk._Conversation__is_ending = True
    return gsm.continue_conversation(dict(CONTINUE))


def end_by_request(gsm, talk):
    return gsm.end_conversation({comm_consts.KEY_ENDCONVERSATION_TIMESTAMP: 7.25})


ENDS = {"natural": end_naturally, "explicit": end_by_request}


@pytest.mark.parametrize("first,then", [("natural", "natural"), ("natural", "explicit"),
                                        ("explicit", "explicit"), ("explicit", "natural")])
def test_failed_summary_write_is_retried_without_duplicates(world, monkeypatch, first, then):
    gsm, talk, _, summarize, folder, (guard, other) = world
    disk = DiskFull(monkeypatch)
    disk.fail(f"{other.name}_summary", times=1)    # NPC 1's records are written, NPC 2's summary write fails
    with pytest.raises(OSError):
        ENDS[first](gsm, talk)
    assert gsm._GameStateManager__talk is talk, "a conversation whose save failed must be kept for the retry"
    reply = ENDS[then](gsm, talk)
    assert reply[comm_consts.KEY_REPLYTYPE] == comm_consts.KEY_REPLYTYPE_ENDCONVERSATION
    assert gsm._GameStateManager__talk is None
    for npc in (guard, other):
        assert logs(folder, npc) == 1, f"{npc.name}: history written {logs(folder, npc)} times"
        assert summaries(folder, npc.name, npc.ref_id) == 1, f"{npc.name}: summary written {summaries(folder, npc.name, npc.ref_id)} times"
    assert summarize.call_count == 1, "the summary LLM call was repeated by the retry"


@pytest.mark.parametrize("first", ["natural", "explicit"])
def test_failed_history_write_is_retried_without_duplicates(world, monkeypatch, first):
    gsm, talk, _, summarize, folder, (guard, other) = world
    disk = DiskFull(monkeypatch)
    disk.fail(f"{other.name}.json", times=1)       # NPC 1's history is written, NPC 2's write fails
    with pytest.raises(OSError):
        ENDS[first](gsm, talk)
    end_by_request(gsm, talk)
    assert (logs(folder, guard), logs(folder, other)) == (1, 1)
    assert summaries(folder, guard.name, guard.ref_id) == 1 and summaries(folder, other.name, other.ref_id) == 1
    assert summarize.call_count == 1


def test_pending_share_survives_a_failed_attempt_once(world, monkeypatch):
    gsm, talk, context, _, folder, (guard, _other) = world
    context.npcs_in_conversation.add_pending_share(guard.name, "Farengar", "F00D")
    disk = DiskFull(monkeypatch)
    disk.fail("Farengar_summary", times=1)
    with pytest.raises(OSError):
        end_naturally(gsm, talk)
    end_by_request(gsm, talk)
    assert summaries(folder, "Farengar", "F00D", needle=f"{guard.name} shared with Farengar") == 1


def test_a_failure_after_every_record_is_written_does_not_duplicate(world, monkeypatch):
    """Re-summarising a full summary file can fail after the new summary is already appended: that summary
    counts as written."""
    gsm, talk, _, _, folder, (guard, other) = world
    calls = {"n": 0}
    real = Summaries._Summaries__append_new_conversation_summary

    def append_then_fail(self, *a, **kw):
        real(self, *a, **kw)
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device (re-summarising)")
    monkeypatch.setattr(Summaries, "_Summaries__append_new_conversation_summary", append_then_fail)
    with pytest.raises(OSError):
        end_by_request(gsm, talk)
    end_by_request(gsm, talk)
    assert summaries(folder, guard.name, guard.ref_id) == 1 and summaries(folder, other.name, other.ref_id) == 1


@pytest.mark.parametrize("sequence", [("natural", "explicit"), ("explicit",)])
def test_successful_save_happens_exactly_once(world, sequence):
    """Control (passes before and after): a save that succeeds is never repeated - not by a later end request,
    not by the next start (which ends a kept conversation)."""
    gsm, talk, _, summarize, folder, (guard, other) = world
    for step in sequence:
        ENDS[step](gsm, talk)
    talk.end()                                     # what the next start_conversation does to a kept conversation
    assert (logs(folder, guard), logs(folder, other)) == (1, 1)
    assert summaries(folder, guard.name, guard.ref_id) == 1 and summaries(folder, other.name, other.ref_id) == 1
    assert summarize.call_count == 1


def test_every_end_still_stops_generation_after_a_failed_save(world, monkeypatch):
    gsm, talk, *_ = world
    disk = DiskFull(monkeypatch)
    disk.fail(".json", times=10)
    for _ in range(2):
        with pytest.raises(OSError):
            talk.end()
    assert talk.output_manager.stop_generation.call_count == 2
    assert talk.has_already_ended
