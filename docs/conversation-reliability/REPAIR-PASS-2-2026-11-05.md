# Repair pass 2 — response to the independent adversarial run against `d535959`

Base: **d535959e51c55353b98378c8a56b8dce45308539** (`claude/mantella-lifecycle-audio-fixes-jikc06`, fetched and checked
out exactly; not the older local 32b05c6 checkout). Branch: `claude/mantella-fo4-repair-pass-2-8lah1o`. Backend source,
tests and docs only: no file under `native/`, Papyrus, build, config, asset or `backend/src/llm/openclaw*` is changed
(`git diff --name-only d535959..<branch>` lists only `backend/src/**`, `backend/tests/**` and docs).
This is a repair candidate for independent review, **not** deployment, installation or runtime acceptance.

| Commit | Item |
|---|---|
| 6538197 | R-04c — atomic history/summary publication, retry-safe identity, rollover never empty |
| f714bf3 | R-03c — saved voiceline name bounded to 255 UTF-8 bytes, token included |
| ed7c206 | R-03d (was O-11) — xVASynth multi-phrase isolation, response check, all-or-nothing merge |
| a5ca3eb | O-10 — end reply after a goodbye held (asynchronously) until the goodbye has played |
| (this docs commit) | report, FINDINGS, acceptance checklist, handoff |

Every new test drives production code; mocked are only external boundaries: the summary LLM call, the TTS service /
lip-fuz tools (marker stubs), the xVASynth HTTP server (`requests.get/post`) and process (`Popen`), and file-system
faults (wrapped file objects, `os.replace`). Each new test was run against unmodified d535959 source; the ones that pass
there are labelled controls in their docstrings.

## 1. R-04c — memory files: ambiguous commits, partial writes, failed rollovers (issues 1–3)

**Defects at d535959** (`summaries.py` 139-144 / 353-358 / 357-391, `conversation.py` 576-578, `conversation_log.py`):
histories and summaries were rewritten in place (`open(path, 'w')` + write). R-04b's `FinalSaveProgress` flags were
set only when a write returned, so

* bytes written, then close/flush raised → not marked written → the retry appended the history **and** the summary
  again (`Lydia: history written 2 times`, `summary written 2 times`);
* a write failing part-way truncated the file → earlier summaries destroyed; the retry read the fragment and appended
  to it; a truncated history raised `JSONDecodeError` on every retry;
* a rollover whose write failed left an empty `<name>_summary_N+1.txt` — the newest file, read by every later prompt
  as the NPC's whole memory — and the retry skipped the NPC as already written (prompt text `""`). An empty re-summary
  (`summarize_conversation` returns `''` on LLM failure) published an empty newest file even without a fault.

**Fix.** `backend/src/atomic_file.py`: `write_text_atomically` writes the complete content to `.<name>.<token>.tmp` in
the same folder (never matched by the `*.txt` latest-summary scan or the `<name>.json` history path), flushes, fsyncs,
closes and `os.replace`s it; any failure removes the temp and leaves the target untouched. `PublicationAttempt`
stores, before the replace, the newline-normalised sha256 of the content in `FinalSaveProgress.publications`; a retry
compares the file with it (`is_committed`) and skips a record whose publication landed although its attempt raised
— the identity comes from the file, not from an in-memory flag. The caller module's `open` is passed through, so
existing fault injection on `src.remember.summaries.open` / `src.conversation.conversation_log.open` still applies.
`Summaries.__roll_over_if_full` is a separate, idempotent step: the retry re-runs it for NPCs/shares already written,
reuses a cached re-summary (`FinalSaveProgress.resummaries`, no second LLM call), writes the next file atomically and
keeps the current file when the re-summary is empty. File formats (JSON `indent=4`, summary text) are unchanged.

**Regressions** `backend/tests/test_lifecycle_atomic_memory.py` (real `Conversation` / `conversation_log` /
`Summaries` through `GameStateManager`, the `world` fixture of `test_lifecycle_save_retry.py`). Faults fire at the
commit point of **either** implementation (close of the target handle for in-place, `os.replace` onto the target
for staged), so the same test is meaningful on both. d535959: **10 failed / 1 passed**; branch: **11 passed**.
* `test_ambiguous_successful_write_does_not_duplicate_on_retry[history|summary × natural→explicit|explicit→explicit]` (4)
* `test_close_failure_after_the_bytes_are_written_does_not_duplicate_on_retry[history|summary]` (2)
* `test_partial_summary_write_preserves_previous_memory_on_retry`, `test_partial_history_write_preserves_previous_history_on_retry`
* `test_failed_rollover_does_not_commit_an_empty_latest_summary`, `test_empty_resummary_does_not_commit_an_empty_latest_summary`
* control `test_successful_rollover_is_unchanged` (passes on both)

## 2. R-03c — Linux file-name regression from the per-synthesis token (issue 4)

**Defect at d535959** (`ttsable.py` 127-140): the readable part was bounded to 150 *characters*; R-03b's
`' <12-hex token>'` turned a valid 253-byte name into 266 bytes → `ENAMETOOLONG` on rename →
`VoicelineProvenanceError` → TTS error. 150 multi-byte characters were over 255 bytes even before the token.

**Fix.** `TTSable._saved_voiceline_stem`: readable part cut so `readable + ' ' + token + longest extension (.wav/.lip/.fuz)`
≤ 255 UTF-8 bytes (≤ 255 UTF-8 bytes also satisfies NTFS's 255 UTF-16 units), cut on a code-point boundary, prefix
kept, token always whole; the existing-destination refusal is unchanged. Names ≤ 238 bytes of readable text (all
ordinary lines) are byte-identical to d535959's.

**Regressions** `backend/tests/test_lifecycle_voiceline_names.py` (real `TTSable.synthesize`, real rename on a
255-byte NAME_MAX tmp_path). d535959: **5 failed / 2 passed**; branch: **7 passed**. Failing on base:
`test_unique_token_preserves_previously_valid_utf8_filename`, `test_multibyte_text_is_cut_on_a_character_boundary[é|語|🐉]`,
`test_long_names_stay_unique_and_collisions_are_still_refused` (uniqueness + forced-token collision still raises).
Passing on both: control `test_short_ascii_names_are_unchanged`; the combining-pair case (`é`×200 happens to
fit within 255 bytes even on base).
32b05c6 (pre-token): these files cannot be collected there (they import helpers added in 0c122d2/f9844f0), so no
32b05c6 count is claimed here; the reported 32b05c6 behaviour (253-byte case passes, collision refusal fails) is
consistent with the code at that commit (no token, no refusal).

## 3. R-03d — xVASynth multi-phrase stale audio (issue 5, was O-11)

**Defect at d535959** (`xvasynth.py` `_synthesize_voiceline` / `_batch_synthesize` / `_merge_audio_files`): phrase files
named by their text, never removed; batch response ignored; merge logged missing/unreadable phrases and wrote
`out.wav` from what it could read. A failed batch merged the previous synthesis' phrases into this line's fresh-token
file (`3000 of 3000 samples from request 1`); a partial batch was filled with older phrases (`2000 of 3000`).

**Fix.** Phrase files `phrase <synthesis token> NN.wav` (refused if one already exists); non-2xx batch response →
`IncompletePhraseSynthesis`; merge reads every phrase first and writes `out.wav` once, failing closed on a missing,
unreadable, empty or different-sample-rate phrase; phrase files removed in `finally`. `generate_sentence` turns the
exception into a TTS error (no fallback audio), as for every synthesize failure.

**Regressions** `backend/tests/test_lifecycle_xvasynth_phrases.py`: real `xVASynth(config)` constructor (connection
check), `change_voice`, `_split_voiceline`, `_batch_synthesize`, `_merge_audio_files`, `TTSable.synthesize`; only
`requests.get/post` (fake server writing the requested phrase wavs), `Popen` (asserts never called) and the
voice-model json are provided. d535959: **4 failed / 2 passed**; branch: **6 passed**. Failing on base:
`test_xvasynth_batch_failure_cannot_publish_previous_phrase_audio[http_500|partial_phrases]`,
`test_unreadable_phrase_fails_closed`, `test_phrase_files_do_not_outlive_their_synthesis`. Controls (both):
`test_split_produces_several_phrases`, `test_successful_batch_merges_every_phrase_of_this_request`.

## 4. O-10 — end reply before the goodbye's audio (issue 6)

**Defect at d535959.** The line request that follows the goodbye (action-blind scripts) was answered with
`mantella_end_conversation` ~1 ms after the goodbye was handed out, while every other next line is held until the
previous line's estimated duration + `wait_time_buffer` has elapsed (the game requests the next line right after
`Say`).

**Fix (narrow, non-blocking).** The end reply is held for the goodbye's remaining duration — the same hold, for the
same request type, every line already relies on, so no new assumption about script semantics is made.
`Conversation.end_reply_hold_seconds()` (set only on the R-06 path), `GameStateManager.pop_reply_hold_seconds()` (the
conversation is saved and dropped **before** the hold), and the async `/mantella` route `await asyncio.sleep(...)`s it,
so the event loop serves other requests meanwhile. End requests and error ends are not held; no JSON field is added.
(The pre-existing next-line hold in `Conversation.continue_conversation` is still a synchronous wait; unchanged.)

**Regressions** `backend/tests/test_lifecycle_goodbye_audio.py` (real `mantella_route` over ASGI with
`httpx.ASGITransport`, real `GameStateManager` / `Conversation` / `ChatManager`, stub TTS returning a 0.8 s wav).
d535959: **1 failed / 2 passed**; branch: **3 passed** (repeated 3×). `test_goodbye_end_reply_waits_for_outstanding_audio`
also asserts that an `init` request sent during the hold completes first and that the conversation is already saved
and dropped. Controls (both): `test_end_request_after_goodbye_is_not_held`, `test_no_hold_once_the_goodbye_has_played`.
**Not proven:** whether the engine keeps the goodbye audible through `CleanupConversation` — that is the in-game check
in ACCEPTANCE-CHECKLIST.md step 8.

## Test evidence (Linux, network isolated with `unshare -rn`, `env -u MANTELLA_OPENCLAW_CONFIG`)

Environment: uv-managed CPython 3.11.15, `tests/requirements-ci.txt` + `tests/requirements-test.txt` +
`setuptools==81.0.0 openai==2.8.0 pytest==9.1.1` (as TESTED-PYTHON-ENV.txt), run from `backend/`.

* Focused set — `python -m pytest -q -p no:cacheprovider tests/test_lifecycle_*.py tests/llm/openclaw tests/test_per_character_llm.py`:
  d535959 **146 passed**; per commit 6538197 157, f714bf3 164, ed7c206 170, a5ca3eb **173 passed** (146 + 27 new),
  each commit exported and run on its own.
* New files only, on unmodified d535959 source: **20 failed / 7 passed** (the 7 are the controls listed above plus the
  combining-pair case); branch: **27 passed**.
* Broad set — `tests/test_lifecycle_*.py tests/llm tests/test_per_character_llm.py tests/remember tests/tts tests/conversation tests/test_game_manager.py tests/test_output_manager.py tests/test_per_character_tts.py tests/actions tests/http`
  with a scratch-only plugin (not committed) replacing tiktoken's blocked encoding download by a whitespace tokenizer:
  d535959 + new tests **479 passed / 43 failed**; branch **499 passed / 23 failed**. The branch's 23 are the
  identical set failing on d535959 (real LLM/API calls, piper models, xTTS/xVASynth route setup and the route
  endpoint tests that need an initialised install config, `test_reload_conversation`); d535959's other 20 are the new
  red cases. No test was skipped, weakened or deleted.
* Native suites not re-run: no native source changed since d535959, where they were last reported passing.

## Remaining limits (not claimed fixed)

* **Durability.** The parent directory is not fsynced after `os.replace`; after a power loss the rename may be lost
  (the old, intact file remains — never a fragment). On Windows `os.replace` fails while another process holds the
  target open without share-delete (some editors/AV); the save then fails and is retried, it is not corrupted.
* **Retry identity** compares the file with the digest of the content the attempt tried to publish. If something else
  rewrites that file between attempts, a landed-but-raised record is not recognised and would be appended again;
  within the game flow the kept conversation is ended (retried) before a new one starts, so no other save of the
  same NPC intervenes.
* A save that fails every time still fails the next start (O-12, owner's decision, unchanged). Files already damaged
  by earlier versions (truncated history, empty newest summary) are not repaired; leftover `.tmp` files from a failed
  cleanup are never read but are not swept.
* **File names:** cut on code points, so a combining sequence can lose its mark at the cut; the full-path length
  (Windows MAX_PATH) is not addressed.
* **xVASynth:** only the HTTP status of the batch response is checked (the server's body format is not specified in
  this repository); the single-phrase path still relies on `out.wav` (invalidated before every synthesis) existing.
* **Goodbye:** the hold uses the estimated duration (wav length + `wait_time_buffer`), not engine playback state.
* Not done: Windows build, PSC/PEX verification, game run, real LLM/TTS inference. The local guard-reset native repair
  (`evaluate_loaded`) and the local opt-in legacy 0.13.0 adapter are **not in this repository**; nothing here touches,
  replaces or accepts them. OpenClaw / per-character routing paths are unchanged (their focused tests pass).
* Focused and broad passes are evidence for the scenarios tested, not a claim of general reliability.
