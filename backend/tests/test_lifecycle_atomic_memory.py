"""Repair pass 2 (R-04c): a failed or ambiguous final save never loses, truncates or duplicates an NPC's memory.

R-04b made a failed final save retryable and tracked what it had written in memory (FinalSaveProgress). An
independent adversarial run found three ways the files themselves still went wrong, because histories and summary
files were rewritten IN PLACE (open(path, 'w') then write):

* a write that put its bytes in the file and then raised (close / flush error) was not recorded as written, so
  the retry appended the conversation / summary a second time;
* a write that failed part-way truncated the file: earlier memory was destroyed, and the retry appended to the
  fragment (a history fragment cannot even be parsed again);
* a summary-file rollover whose write failed left an EMPTY newest file, which every later prompt reads as the
  NPC's whole memory, and the retry skipped that NPC because its summary was already "written".

Records are now staged in a temporary file and moved over the target with os.replace (src/atomic_file.py), the
digest of each record's published content is kept in FinalSaveProgress so a retry recognises a publication that
landed although its attempt raised, and the rollover is redone by the retry and never publishes an empty file.

These tests drive the real Conversation, conversation_log and Summaries through GameStateManager (the world fixture
of test_lifecycle_save_retry.py); only the summary LLM call is mocked and the file system is fault-injected at the
file-object / os.replace level. Each fault fires at the moment the target file holds the new bytes (or part of
them) in BOTH the in-place implementation (close of the target handle) and the staged one (os.replace onto the
target), so the same test exercises the commit point of either.
"""
import json
import os

import pytest

from src.remember.summaries import Summaries

from tests.test_lifecycle_save_retry import (SUMMARY, WORLD, end_by_request, end_naturally, logs, summaries,  # noqa: F401
                                             world)

OLD_MEMORY = "Lydia remembers the dragon at the western watchtower.\n\n"


class FileFaults:
    """Fault injection on the file objects / os.replace used by conversation_log and Summaries.

    Rules match a substring of the basename of the path being written (for the staged temp file that is
    '.<target name>.<token>.tmp', for os.replace the destination). Each rule fires `times` times."""

    def __init__(self, monkeypatch):
        self.rules: list[dict] = []
        real_open = open
        faults = self

        class Handle:
            def __init__(self, f, rule):
                self.__f, self.__rule = f, rule

            def write(self, data):
                rule = self.__rule
                if rule and rule["kind"] == "partial":
                    self.__f.write(data[:rule["keep"]])
                    self.__f.flush()
                    raise OSError(28, "No space left on device (partial write)")
                return self.__f.write(data)

            def close(self):
                self.__f.close()
                if self.__rule and self.__rule["kind"] == "close":
                    raise OSError(5, "Input/output error (close after the bytes were written)")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()
                return False

            def __getattr__(self, name):
                return getattr(self.__f, name)

        def faulty_open(path, mode="r", *a, **kw):
            f = real_open(path, mode, *a, **kw)
            if "w" in mode or "a" in mode:
                rule = faults._take(os.path.basename(str(path)), ("partial", "close"))
                if rule:
                    return Handle(f, rule)
            return f

        real_replace = os.replace

        def faulty_replace(src, dst, *a, **kw):
            rule = faults._take(os.path.basename(str(dst)), ("replace_landed",))
            real_replace(src, dst, *a, **kw)
            if rule:
                raise OSError(5, "Input/output error (after the rename)")

        monkeypatch.setattr("src.remember.summaries.open", faulty_open, raising=False)
        monkeypatch.setattr("src.conversation.conversation_log.open", faulty_open, raising=False)
        monkeypatch.setattr(os, "replace", faulty_replace)

    def _take(self, name: str, kinds) -> dict | None:
        for rule in self.rules:
            if rule["kind"] in kinds and rule["times"] > 0 and rule["match"](name):
                rule["times"] -= 1
                return rule
        return None

    def add(self, kind: str, match, times: int = 1, keep: int = 0):
        self.rules.append({"kind": kind, "match": match, "times": times, "keep": keep})


def target_name(npc, record: str) -> str:
    return f"{npc.name}.json" if record == "history" else f"{npc.name}_summary_1.txt"


def is_target(name: str):
    """Matches the target file itself (in-place write, or os.replace destination), not a staged temp file."""
    return lambda basename: basename == name


def is_target_or_staged(name: str):
    """Matches the target file and its staged temporary file ('.<name>.<token>.tmp')."""
    return lambda basename: basename == name or basename.startswith(f".{name}.")


def summary_path(folder, npc, n: int = 1) -> str:
    return os.path.join(folder, WORLD, f"{npc.name} - {npc.ref_id}", f"{npc.name}_summary_{n}.txt")


def history_path(folder, npc) -> str:
    return os.path.join(folder, WORLD, f"{npc.name} - {npc.ref_id}", f"{npc.name}.json")


def read(path) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


# --- 1. ambiguous commit: the bytes are in the file, then the write raises -------------------------------------

@pytest.mark.parametrize("record", ["history", "summary"])
@pytest.mark.parametrize("first,then", [("natural", "explicit"), ("explicit", "explicit")])
def test_ambiguous_successful_write_does_not_duplicate_on_retry(world, monkeypatch, record, first, then):
    """The target file already holds the complete new content when the attempt raises (in place: close() of the
    target fails after the write; staged: os.replace onto the target fails after the rename). The retry must
    recognise the landed record instead of writing it again."""
    gsm, talk, _, summarize, folder, (guard, other) = world
    faults = FileFaults(monkeypatch)
    name = target_name(other, record)
    faults.add("close", is_target(name))            # in-place implementation's commit point
    faults.add("replace_landed", is_target(name))   # staged implementation's commit point
    ends = {"natural": end_naturally, "explicit": end_by_request}
    with pytest.raises(OSError):
        ends[first](gsm, talk)
    assert gsm._GameStateManager__talk is talk
    ends[then](gsm, talk)
    assert gsm._GameStateManager__talk is None
    for npc in (guard, other):
        assert logs(folder, npc) == 1, f"{npc.name}: history written {logs(folder, npc)} times"
        assert summaries(folder, npc.name, npc.ref_id) == 1, \
            f"{npc.name}: summary written {summaries(folder, npc.name, npc.ref_id)} times"
    assert summarize.call_count == 1


@pytest.mark.parametrize("record", ["history", "summary"])
def test_close_failure_after_the_bytes_are_written_does_not_duplicate_on_retry(world, monkeypatch, record):
    """close() fails after every byte was written, wherever the bytes went (the target or its staged file)."""
    gsm, talk, _, summarize, folder, (guard, other) = world
    faults = FileFaults(monkeypatch)
    faults.add("close", is_target_or_staged(target_name(other, record)))
    with pytest.raises(OSError):
        end_by_request(gsm, talk)
    end_by_request(gsm, talk)
    for npc in (guard, other):
        assert logs(folder, npc) == 1, f"{npc.name}: history written {logs(folder, npc)} times"
        assert summaries(folder, npc.name, npc.ref_id) == 1
    assert summarize.call_count == 1


# --- 2. partial write: earlier memory must survive the failed attempt and the retry ---------------------------

def test_partial_summary_write_preserves_previous_memory_on_retry(world, monkeypatch):
    gsm, talk, context, _, folder, (guard, other) = world
    path = summary_path(folder, other)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(OLD_MEMORY)
    faults = FileFaults(monkeypatch)
    faults.add("partial", is_target_or_staged(os.path.basename(path)), keep=7)
    with pytest.raises(OSError):
        end_by_request(gsm, talk)
    assert read(path) == OLD_MEMORY, "the failed attempt destroyed the earlier memory"
    assert OLD_MEMORY.strip() in talk._Conversation__rememberer.get_prompt_text([other], WORLD)
    end_by_request(gsm, talk)
    assert read(path) == OLD_MEMORY + SUMMARY, f"retry accepted a fragment: {read(path)!r}"
    assert not [n for n in os.listdir(os.path.dirname(path)) if n.endswith(".tmp")], "staged file left behind"


def test_partial_history_write_preserves_previous_history_on_retry(world, monkeypatch):
    gsm, talk, _, _, folder, (guard, other) = world
    path = history_path(folder, other)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    earlier = [[{"role": "user", "content": "An earlier conversation."}]]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(earlier, f, indent=4)
    faults = FileFaults(monkeypatch)
    faults.add("partial", is_target_or_staged(os.path.basename(path)), keep=10)
    with pytest.raises(OSError):
        end_by_request(gsm, talk)
    with open(path, encoding="utf-8") as f:
        assert json.load(f) == earlier, "the failed attempt destroyed the earlier history"
    end_by_request(gsm, talk)
    with open(path, encoding="utf-8") as f:
        history = json.load(f)
    assert len(history) == 2 and history[0] == earlier[0]


# --- 3. rollover: never an empty newest summary file ----------------------------------------------------------

@pytest.fixture
def full_summaries(world, monkeypatch):
    """Every summary file exceeds the token limit after this conversation's summary is appended, so each NPC's
    file is rolled over to <name>_summary_2.txt holding the re-summary."""
    gsm, talk, context, summarize, folder, npcs = world
    rememberer = talk._Conversation__rememberer
    client = rememberer._Summaries__client
    client.get_count_tokens.side_effect = lambda text: 10**6 if SUMMARY.strip() in text else 1

    def summarize_or_resummarize(text, prompt):
        if isinstance(text, str) and SUMMARY.strip() in text:
            return "COMPACT MEMORY.\n\n"
        return SUMMARY
    summarize.side_effect = summarize_or_resummarize
    return world


def test_failed_rollover_does_not_commit_an_empty_latest_summary(full_summaries, monkeypatch):
    gsm, talk, _, summarize, folder, (guard, other) = full_summaries
    rememberer = talk._Conversation__rememberer
    faults = FileFaults(monkeypatch)
    faults.add("partial", is_target_or_staged(f"{other.name}_summary_2.txt"), keep=0)
    with pytest.raises(OSError):
        end_by_request(gsm, talk)
    # between the attempts, the NPC's memory is what was there plus this conversation - never empty
    assert SUMMARY.strip() in rememberer.get_prompt_text([other], WORLD), \
        "the failed rollover left an empty newest summary file: the NPC lost its memory"
    end_by_request(gsm, talk)
    for npc in (guard, other):
        assert read(summary_path(folder, npc, 2)) == "COMPACT MEMORY.\n\n", f"{npc.name} was not rolled over"
        assert "COMPACT MEMORY." in rememberer.get_prompt_text([npc], WORLD)
        assert summaries(folder, npc.name, npc.ref_id) == 1
    # one conversation summary + one re-summary per NPC; the failed rollover reuses its re-summary
    assert summarize.call_count == 3


def test_empty_resummary_does_not_commit_an_empty_latest_summary(full_summaries):
    gsm, talk, _, summarize, folder, (guard, other) = full_summaries
    rememberer = talk._Conversation__rememberer
    summarize.side_effect = lambda text, prompt: "" if SUMMARY.strip() in str(text) else SUMMARY
    end_by_request(gsm, talk)
    for npc in (guard, other):
        assert not os.path.exists(summary_path(folder, npc, 2)), "an empty re-summary became the latest memory"
        assert SUMMARY.strip() in rememberer.get_prompt_text([npc], WORLD)


def test_successful_rollover_is_unchanged(full_summaries):
    """Control: a rollover that succeeds produces <name>_summary_2.txt with the re-summary, as before."""
    gsm, talk, _, summarize, folder, (guard, other) = full_summaries
    end_by_request(gsm, talk)
    for npc in (guard, other):
        assert read(summary_path(folder, npc, 1)) == SUMMARY
        assert read(summary_path(folder, npc, 2)) == "COMPACT MEMORY.\n\n"
    assert summarize.call_count == 3
