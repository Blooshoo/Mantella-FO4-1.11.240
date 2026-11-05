# Lifecycle repair candidate — response to the independent review of `claude/practical-goodall-abxw3m`

Base: **32b05c61a205b44ed5ef345681af54cea93114a5** (`claude/practical-goodall-abxw3m`). Branch:
`claude/mantella-lifecycle-audio-fixes-jikc06`. Backend only; no native, Papyrus, build or asset file is touched.
This is a repair candidate for independent review, **not** deployment or runtime acceptance.

Every new regression drives the real production code (`TTSable.synthesize`, `Fallout4.prepare_sentence_for_game`,
`ChatManager`, `Conversation`, `GameStateManager`, `conversation_log`, `Summaries`). Mocked are only the external
boundaries: TTS service / LipFuzer / FaceFX / xWMAEncode / Fuz_extractor (marker-writing stubs that fail silently
like the real tools), the LLM (scripted async stream), and file-system faults (injected `open` / `os.remove` /
`os.rename` failures). Each new test was run against the base commit; the ones that pass there are labelled
controls in the test docstrings.

## 1. R-03b — stale generated audio after saved-file collisions or failed invalidation (fixed)

**Defect (base).** `TTSable.synthesize` saved every line as `voicelines/save/<voice> <first 150 chars of text>`
`.wav/.lip/.fuz`. Same voice + same sanitized/truncated text = same names, across requests and within a response:
* a later line whose `.fuz` generation failed (the tools only log) kept the **earlier line's `.fuz`** under its own
  name and `prepare_sentence_for_game` copied it into the slot;
* sentences are synthesized ahead of playback, so a later line with the same 150-char prefix **overwrote a queued
  line's files**; the queued subtitle was voiced with the other line's audio;
* when the earlier saved `.fuz` could not be unlinked, the bare `except` logged, left `out.fuz` unrenamed, and the
  earlier `.fuz` was played;
* cleanup of `out.wav/.lip/.fuz` failing was only a warning ("Failed to remove spoken voicelines"), after which
  the leftover was used as this line's output; `out.xwm` (xWMAEncode output that Fuz_extractor packs) was never
  invalidated at all.

**Fix (`backend/src/tts/ttsable.py`).** Saved names carry a per-synthesis token (`uuid4().hex[:12]`) after the
readable text, so a file can only belong to the synthesis that created it; an already-existing destination is
refused. Before synthesis every intermediate (`out.wav`, `.lip`, `.fuz`, `.xwm`, `_r.wav`) is removed; one that
exists and cannot be removed raises `VoicelineProvenanceError`. A wav that cannot be saved under its fresh name
raises too (before: a path with no file behind it was returned). `generate_sentence` already turns any synthesize
exception into a Text-to-Speech error sentence (`voice_file ""`), which `GameStateManager` answers with an error
reply — no fallback to an older file. A `.fuz` that cannot be renamed leaves the line without a `.fuz`, so R-03's
slot removal gives it no audio rather than an old line.

**Regressions** `backend/tests/test_lifecycle_voiceline_provenance.py` — base: **8 failed / 4 passed**; branch:
**12 passed**. Failing on base, each with its stale-audio assertion: inherited saved `.fuz`
(`b'FUZ:AUDIO#1'` played for request B), queued line overwritten by a prefix-colliding later line, undeletable
earlier saved `.fuz` played, deletable leftover `out.xwm` packed into the new `.fuz`, undeletable `out.wav` /
`out.fuz` / `out.xwm` used as this line's output (3), provenance failure through `ChatManager.generate_sentence`
not surfacing as a TTS error. Controls passing on both: same-turn repeated line keeps valid audio for both
occurrences; deletable leftover `out.wav` / `out.fuz` (already R-03); wav rename failure never returns a file.

**Trade-off.** Saved voicelines are no longer overwritten by a repeated phrase, so `voicelines/save` grows by one
set of files per line for the session (the folder is in the temp dir and is wiped at startup by
`setup.py: cleanup_tmp`). Previously growth was bounded only by distinct phrases.

**Not fixed (found while reviewing, outside this item's scope):** xVASynth multi-phrase synthesis
(`tts/xvasynth.py:_synthesize_voiceline`) writes per-phrase files named by their text into the voiceline folder,
never removes them, and `_merge_audio_files` swallows a missing/unreadable phrase file. A failed batch request
therefore merges an older phrase file of the same name into `out.wav`. Needs its own harness (XVASynth's
constructor connects to the server); only affects xVASynth with multi-phrase lines.

## 2. R-04b — a failed first save suppressed every retry (fixed)

**Defect (base).** `Conversation.end()` saved only when `__has_already_ended` was False, and set it before saving.
A first save that raised (disk full, locked history/summary file) therefore made every later end skip the save:
the conversation was never persisted. Before R-04 (cfcc5f0) the retry happened, but re-appended every record the
failed attempt had written, repeated the summary LLM call, and lost the pending memory shares the first attempt
had already cleared.

**Fix.** `__is_saved` (set only after the final save returns) replaces "ended" as the save guard. The final save
carries a `FinalSaveProgress` (`backend/src/remember/remembering.py`): conversation logs written, generated
summaries (reused, never re-summarised), summaries written, shares written, and the pending shares captured once.
`Summaries.save_conversation_state` takes it as an optional keyword; `__append_new_conversation_summary` reports a
summary as written right after its file write, before the re-summarising of a full file, which can still fail.
Reload / departed-NPC saves pass no progress and are unchanged. `GameStateManager` needs no change: it drops the
conversation only after `end()` returns, so a failed save leaves it in place, and the next end (natural or
explicit) or start retries. The first non-None end timestamp is kept for the retry.

**Regressions** `backend/tests/test_lifecycle_save_retry.py` (real `Conversation` + `conversation_log` + `Summaries`
through `GameStateManager`, `summarize_conversation` mocked, writes fault-injected) — base: **8 failed / 3 passed**
(e.g. "Lydia: summary written 0 times"); pre-R-04 naive retry (cfcc5f0): **10 failed / 1 passed** (e.g. histories
`(2, 2) == (1, 1)`, summary LLM called twice; its natural-end cases also fail because R-06 is absent there);
branch: **11 passed**. Covered: failed summary write retried by natural→natural, natural→explicit,
explicit→explicit, explicit→natural; failed history write retried after natural or explicit end; pending share
delivered exactly once after a failed attempt; failure after the summary is appended (re-summarise step) does not
duplicate; every end still stops generation after a failed save. Control (passes on base): a successful save is
never repeated by a later end request or the next start.

**Remaining boundary.** A save that fails *every* time is retried at the next start too, and the exception there
fails that start request — identical to the behaviour before R-04 (the owner's choice between losing that
conversation's memory and blocking new conversations until the disk/file problem is fixed; not decided here).
`conversation_log.save_conversation_log` still rewrites the history file in place; a failure *during* `json.dump`
can leave it truncated, which every retry then fails to parse. Atomic write-and-rename would close that; not
changed here.

## 3. Races investigated

### R-07 — action result arriving while the response that requested the action is finishing (fixed)

**Defect (base), reproduced deterministically.** For an action with `requires_response` (e.g. Inventory)
`ChatManager.process_response` queues the action-only sentence as soon as the tool call is parsed and stops; the
generation thread is still alive while it drains the stream, closes it and queues the terminator. The game runs the
action and returns the result with its next continue. `resume_after_interrupting_action` then added the result,
cleared the queue and called `__start_generating_npc_sentences`, which starts nothing while the old thread
`is_alive()` — yet reported "resumed". The old thread's terminator ended the response and the game got
`mantella_player_talk`: the action result was never answered (`client.calls == 1`).

**Fix (`Conversation.resume_after_interrupting_action`).** Stop and join the pre-action generation
(`__stop_generation`, the same call `end()` / actor changes use) before the result is added and the continuation
starts. Its leftover sentences are discarded, exactly as when it had finished first.

**Regressions** `backend/tests/test_lifecycle_action_result.py` (real `ChatManager` / `Conversation` /
`GameStateManager`; the mock LLM holds the stream open 1.5 s after the tool call) — base: **2 failed / 1 passed**
(the "after it finished" control passes); branch: **3 passed** (repeated 3×; the 1.5 s tail is cancelled, so the
run takes ~0.25 s).

### Goodbye completion before audio completion (not fixed — boundary documented)

Measured on this branch (scratch probe, not committed): with a 2.0 s goodbye, the line request that follows it is
answered with `mantella_end_conversation` **~1 ms** later (R-06 path), and the conversation is saved and dropped.
Every other next line is held by the backend until the previous line's duration has elapsed
(`Conversation.continue_conversation`), because the game requests the next line right after `Say`. So the backend
provably declares the end before the goodbye's audio has finished. Whether that **cuts** the goodbye depends on
what the installed script's `CleanupConversation` does to an in-flight `Say` (the action-aware path has the same
ordering: its end request is also sent on receipt of the end action). That script is not in this repository (O-01)
and engine behaviour cannot be observed here, so no fix is made: holding the end reply for the remaining duration
would be a guess about script semantics and would block the event loop for it (O-02). **Acceptance check:** in-game,
end a conversation by saying goodbye with an action-blind script and confirm whether the goodbye line plays to the
end.

## What this branch does not prove or change

* No Windows compile, no game run, no Papyrus/PEX semantic verification, no real LLM/TTS call. Compiled PSC/PEX
  verification and in-game acceptance remain local gates.
* The separate local opt-in legacy 0.13.0 text/basic-action adapter is not in this repository and was not reviewed;
  nothing here claims full Papyrus compatibility. The local-only guard-reset native repair (`evaluate_loaded`) is
  not in Git and is untouched (no native files changed).
* OpenClaw / per-character routing, protocol defaults, the 1.11.240 runtime/editor guards, M01/M02 and the disabled
  natives are unchanged (no file under `native/`, `backend/src/llm/openclaw*`, `backend/src/config` is modified).

## Test evidence (Linux, network isolated with `unshare -rn`, `env -u MANTELLA_OPENCLAW_CONFIG`)

Environment: uv CPython 3.11.15, `tests/requirements-ci.txt` + `tests/requirements-test.txt` + `setuptools==81.0.0`,
`openai==2.8.0`, `pytest==9.1.1` (as TESTED-PYTHON-ENV.txt).

* Focused lifecycle + OpenClaw set (`tests/test_lifecycle_*.py tests/llm/openclaw tests/test_per_character_llm.py`):
  base 120 passed; branch **146 passed** (120 + 26 new); per commit 0c122d2 132, f9844f0 143, a14d574 146.
* Broad set (`tests/test_lifecycle_*.py tests/llm tests/test_per_character_llm.py tests/remember tests/tts tests/conversation tests/test_game_manager.py tests/test_output_manager.py tests/test_per_character_tts.py tests/actions tests/http`, run on a clean worktree of a14d574): base **446 passed, 23 failed**; branch **472 passed, 23 failed**; the 23 FAILED are the identical set on both (real LLM/API calls, piper models, xTTS/xVASynth route setup, `test_reload_conversation`). Run with a scratch-only pytest plugin (not committed) that replaces
  the tiktoken encoding download (blocked by the cloud proxy) with a whitespace tokenizer, so tests constructing a
  real `LLMClient` (all of `tests/remember/test_summaries.py`, `test_output_manager.py`, `test_context.py`, …) run
  instead of erroring. Without it the same 84 network errors occur on base and branch alike.
* `SOURCE-MANIFEST.json` records the 17b6d33 snapshot and was not updated by any later commit; left unchanged.
* Native portable suites (`native/port240/run_tests.sh`, `native/port240/http/run_tests.sh`): both exit 0 (guard tests ASan/UBSan; HTTP ASan/UBSan + TSan, lints); real-file and official-PSC checks SKIPPED (inputs unset)
  (unchanged sources; run as a regression check only).
