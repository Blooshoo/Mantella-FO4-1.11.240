# Conversation reliability — ranked findings (cloud review 2026-11-05)

Starting commit **94163f6** (`ccr-a959bf09-5dtmxx`), branch `claude/practical-goodall-abxw3m`. File:line
references are at 94163f6 unless marked. "Public script" = `YetAnotherModder/fallout-4-mantella-mod`
b9d72983 (newest public Papyrus source; the installed official package is **not** verified to match it —
see O-01). Architecture/state machines: [ARCHITECTURE.md](ARCHITECTURE.md). Acceptance:
[ACCEPTANCE-CHECKLIST.md](ACCEPTANCE-CHECKLIST.md).

Proved = follows from source and is reproduced by an offline regression. Engine/runtime = needs evidence
from a real game run. Nothing here is Windows-compiled or game-tested.

## Ranked table

| # | ID | Sev. | Where (94163f6) | Trigger | Consequence | Evidence | Repair | Regression | Remaining uncertainty |
|---|---|---|---|---|---|---|---|---|---|
| 1 | **R-01** | Critical (hang) | `backend/src/output_manager.py:298-302, 388, 587-592`; consumer `conversation.py:496`, `sentence_queue.py:31-32`; route `mantella_route.py:66` | generation thread ends without ending the queue: no NPC loaded (radiant start prompt), any exception outside `process_response`'s try, or a client-routing failure (finally then raises `UnboundLocalError` on `active_client`) | `continue_conversation` blocks forever in `queue.get()` **on uvicorn's only event-loop thread**: that request, the end request and every later start never get an answer until MantellaSoftware restarts; `is_generating` stuck → `stop_generation` spins forever | proved; regression times out on parent (5/6) | **fixed d8880d8 + 475fb07 (R-01b)**: `generate_response` owns the end of every response (error sentence → error reply), clears `is_generating` in `finally`; `active_client` bound before the try; sentinel queued before `is_more_to_come=False` | `tests/test_lifecycle_response_termination.py` (incl. the production `GameStateManager.continue_conversation` path) | other unbounded waits remain (O-02) |
| 2 | **R-02** | Critical (hang) | `backend/src/utils.py:150-158`, `output_manager.py:277`; producer `tts/openai_compatible.py:74-81`; consumer `conversation.py:167-171` | TTS output that `wave` misreads or rejects: streamed WAV header (size 0xFFFFFFFF — `openai_compatible` saves the raw response when soundfile rejects it), truncated file, float / WAVE_FORMAT_EXTENSIBLE (rejected on Python 3.11), missing file | streamed: duration **89478.5 s** → the next line busy-waits ~24.8 h on the event-loop thread (backend wedged), and the duration is sent to the game; unreadable: exception outside `generate_sentence`'s try → retry path fails the same way → `process_response`'s finally raises → R-01 wedge | proved (Python 3.11 probes; regression) | **fixed d91170c**: RIFF parser, frames = min(declared, bytes present)/block_align, unknown sizes use bytes present, frame-based formats only, no header-sized reads; failures → normal Text-to-Speech error sentence | `tests/test_lifecycle_audio_duration.py` (15/19 fail on parent) | none for duration; compressed WAVs now refused explicitly |
| 3 | **R-06** | High (no completion) | `backend/src/conversation/conversation.py:174-201, 447-463`; public script `MantellaConversation.psc:316, 266, 389-390` | game script reads actions as `string[]` (public script) while this backend sends `{'identifier':…}` dicts → the goodbye's end action is never seen; the game asks for the next line | pc_to_npc: told to wait for player input after goodbye (mic already off), never saved; **radiant: a new goodbye (TTS) on every request, forever** (3 in 6 requests) | proved against the public script; regression | **fixed 1bff927**: once the goodbye is consumed, the next line request gets `mantella_end_conversation` (handled by the script) and the backend saves + drops the conversation; action-aware scripts unaffected | `tests/test_lifecycle_goodbye_ends.py` (2/3 fail on parent) | installed script version (O-01) |
| 4 | **R-03** | High (wrong audio) | `backend/src/games/fallout4.py:186-219`; `tts/ttsable.py:93-99, 543-558`, Linux `:461, :507` | sentence without a usable `.fuz`: TTS skipped a < 3-character line (`voice_file ""`), LipFuzer/FaceFX/xWMAEncode/Fuz_extractor failure (only logged), copy failure, or a leftover `out.fuz` renamed onto a new line | the game plays the **previous line** (two turns earlier, possibly another NPC/conversation) from `MantellaVoice00/<topicinfo>_1.fuz` under the new subtitle; reply claims success | proved; regression | **fixed cfcc5f0**: slot replaced, else removed (line has no audio, actionable error); if it can be neither, `VoicelinePreparationError` → error reply; `synthesize` clears `out.fuz` | `tests/test_lifecycle_fo4_voiceline.py` (8/9 fail on parent) | engine behaviour for a missing voice file (silent line vs archived placeholder) needs runtime evidence |
| 5 | **R-05** | Med-High (undiagnosable failure) | `native/port240/http/include/f4sehttp240/http_core.h:43, 216-221`; public script `MantellaConstants.psc:7`, `MantellaConversation.psc:1124-1133` | any native error reply (backend not running = WinHTTP 12029, HTTP 500, bad JSON, saturation, …) | script reads `"SKSE_HTTP_error"`, port stored only `"f4se_http_error"` → in-game **"Error: Could not retrieve error"**, reason lost (nothing is logged natively either) | proved against both public script versions; regression | **fixed 4b3e56e**: message stored under both keys | `test_error_reply_readable_by_official_scripts` (4 checks fail on parent) | installed constant unverified (extra key harmless); **F4SE_HTTP.dll must be rebuilt** |
| 6 | **R-04** | Medium (state corruption, paid call) | `backend/src/conversation/conversation.py:480-489`; `game_manager.py:59, 172`; `conversation_log.py:27` | error reply path ends the conversation but keeps it; script cleans up without an end request; next start (or an end request) ends it again | conversation appended twice to every NPC's history and summarised twice (extra LLM call) per error | proved; regression (3 saves → 1) | **fixed 0b2af2e**: `end()` saves once, still stops generation every call | `tests/test_lifecycle_conversation_end.py` | — |
| 7 | **O-01** | High, **open (missing source)** | public script `MantellaConversation.psc:211-269, 316`; backend `game_manager.py:141, 148-154, 191-214` | public scripts predate this backend's wire format: actions are dicts; replies `mantella_npc_action` and `mantella_interrupted` have **no handler (no constant)** | all NPC actions silently dropped (follow, inventory, offended, remove character…; end is handled by R-06); a player line that is exactly an action key (`Follow`, `Wait`, `Inventory`…), advanced actions (tool calls) or a mic interruption → reply consumed, **no request pending, conversation stalls** until a hotkey/listener ends it | proved against the public script; installed version unknown | not fixed: needs the installed script source/version (or a deliberate backend compatibility mode). Acceptance avoids these inputs | — | which `MantellaConversation.pex` is installed |
| 8 | **O-02** | High, open (architecture) | `mantella_route.py:66` (`async def` + sync handlers), `sentence_queue.py:32`, openai client defaults (600 s/request + 2 retries), `xvasynth.py:248`, `xtts.py:77` (no timeout), mic wait `conversation.py:265-277` | slow/hung LLM or TTS, slow summary on start | the server is serialized behind the slow request; the script gives up at 120 s **without telling the backend** (`SetPolling` → `CleanupConversation`); native receive timeout 180 s/step; the next start waits behind the stale request and can time out too | proved by source; latency unmeasured | proposed: TTS request timeouts; a bounded consumer wait (< 120 s) that stops generation and returns an error; longer term, handlers off the event loop with per-conversation cancellation. Not done: changes concurrency semantics, needs runtime latency evidence | — | real LLM/TTS latencies under Proton |
| 9 | **O-03** | Medium, open (engine semantics) | `native/upstream/TopicInfoPatcher/src/Crosshair.cpp:24-28`; public `MantellaRepository.psc:581-583, 659` | start-conversation **hotkey** with `allowCrosshairTracking` (true by default on desktop) | `GetLastActorCoords` returns `[]`; the script indexes `[0..2]` without a length check. If Papyrus aborts on the out-of-range access the hotkey does nothing; if it continues with 0.0, `FindClosestActor(0,0,0,20)` can pick an **unrelated actor near the cell origin** | proved by source; Papyrus out-of-range semantics unverified | not fixed (any coordinates would be faked). Use the activation prompt; native refusal is silent (a log line in `Crosshair.cpp` is a candidate once the Windows build is re-run) | — | Papyrus OOB behaviour |
| 10 | **O-04** | Low-Med (cosmetic, misleading) | `native/port240/include/mantella240/topic_patch.h:33`; `backend/src/games/fallout4.py:55-74`; public `MantellaConversation.psc:336-339` | NPC line of 151–447 UTF-8 bytes | `PatchTopicInfo` → -2, script notifies "Patcher returned -2", line plays with the **previous subtitle**; audio correct | proved (upstream-inherited limit) | not changed: raising 150 needs proof the engine handles longer pooled response strings | — | engine subtitle limits |
| 11 | O-05 | Low | `topic_patch.h:36-45`; public script `:336-339` | -6 (65 536 displaced entries in one game process, or sealed), -4 (guard failed), -3, -5 | notification only; NPC still speaks with stale/placeholder subtitle; conversation continues | proved | none needed for completion; restart the game after -6 | — | — |
| 12 | O-06 | Low | `TakeScreenShot` (`main.cpp:285-288`), public `MantellaRepository.psc:779-785`; `backend/src/image/image_manager.py:236-241` | Vision with in-game screenshots (off by default) | no screenshot → backend silently sends no image; no stale image (leftovers deleted at startup, file consumed on read) | proved | keep Vision off | — | — |
| 13 | O-07 | Low, open | `backend/src/tts/ttsable.py:455-461, 504-509` | backend running natively on Linux with Creation Kit LipGen/LipFuzer paths present | `subprocess.CREATE_NO_WINDOW` does not exist off Windows → no `.lip`/`.fuz` → (before R-03: stale audio) now silent lines + error log | proved (`hasattr` False on Linux) | not fixed: running the tools through wine is untested here | — | Kalli's backend platform |
| 14 | O-08 | Info | `native/port240/http/include/f4sehttp240/http_core.h:79`; `backend/src/http/http_server.py:61` | WinHTTP connects to `localhost`; uvicorn binds 127.0.0.1 | works if `localhost` resolves to IPv4 first (expected under Wine) — otherwise WinHTTP 12029, now visible in-game (R-05) | runtime assumption | none | — | Proton resolution order |
| 15 | O-09 | Low | `http_core.h:216-218`, `dict_store.h:343-348` | store completely full (> 50 000 live dicts + 64 reserve) | error reply silently dropped → script waits its 120 s timeout | proved, practically unreachable (store reclaimed at every start) | none | existing A05 tests | — |

ClearCache and SetOverrideFileName have **no callers** in either public script; their disabled no-op is
harmless. Engine-reader/lifetime (N02/A03), layout, form-reload and durable-character items from
REVIEW-STATUS / CLOUD-HANDOFF are unchanged by this review.

**R-01b (found in the adversarial review of R-01, fixed 475fb07).** R-01 queues the terminator before
clearing `is_more_to_come`; `SentenceQueue.get_next_sentence` then waited in `queue.get()` without ever
re-checking the flag, so a terminator queued and then cleared before the consumer looked
(`resume_after_interrupting_action` clears without stopping generation) could block it forever. The
consumer now waits in 0.1 s slices and returns once the end is declared
(`test_waiting_consumer_returns_when_end_is_declared`, deterministic; fails on the original queue too).

## Follow-up after the independent review (branch `claude/mantella-lifecycle-audio-fixes-jikc06`)

Details, regressions and base/branch counts: [LIFECYCLE-REPAIR-2026-11-05.md](LIFECYCLE-REPAIR-2026-11-05.md).

| ID | Sev. | Defect (at 32b05c6) | Status |
|---|---|---|---|
| **R-03b** | High (wrong audio) | saved voiceline names derived from voice + first 150 chars collided across requests/within a response (inherited or overwritten `.fuz`); failed invalidation of `out.*` only logged; `out.xwm` never invalidated | fixed 0c122d2: per-synthesis names, fail closed on invalidation/save failure |
| **R-04b** | Medium (lost memory) | R-04's guard made a failed first save permanent: never retried | fixed f9844f0: saved-once guard + `FinalSaveProgress`, retry writes only missing records |
| **R-07** | Medium (no reply) | action result arriving while the requesting response finished: continuation silently not started, `player_talk` | fixed a14d574: stop + join pre-action generation first |
| O-10 | open | goodbye: end reply ~1 ms after a goodbye line (other lines are held for their duration); audio cut depends on the installed script's `CleanupConversation` | documented, needs in-game check |
| O-11 | open | xVASynth multi-phrase files are never invalidated and merge errors are swallowed: a failed batch can merge an older phrase | documented, not fixed |
| O-12 | open | a save that fails every time also fails the next start (as before R-04); history file rewritten in place (truncation on a mid-write failure) | documented |

## Native lifetimes and thread requirements (reviewed, not changed)
* F4SE_HTTP240: natives run on Papyrus VM threads and only copy strings/arrays in and out
  (`contract.h`); completions run on dispatcher threads and touch only the mutex-guarded store/queue, never
  the VM; process-lifetime objects are leaked on purpose (exit-time safety). No VM object is retained.
* TopicInfoPatcher, **engine assumptions requiring runtime evidence**: `PatchTopicInfo` publishes the
  response slot from a Papyrus thread while engine UI/subtitle readers run elsewhere (N02: our ownership
  protocol never frees what a reader may hold; concurrent engine writes and aligned single loads are
  unproven); `isMenuModeActive` reads `UI` menu state and `saveFloat`/`restoreFloat` read/write
  `GameSettingCollection` entries off the main thread (upstream behaviour); the `SettingsFloat` /
  `SettingsInt` maps (`main.cpp:68-69`) are unsynchronized — safe only if Papyrus never runs two of those
  natives at once (the public script calls them sequentially from one quest, `MantellaConversation.psc:1673-1704`).
  A mutex there is trivial but would be another uncompiled TopicInfoPatcher change; left for the next
  Windows build together with the `Crosshair.cpp` refusal log line (O-03).

## Not in Git: Kalli's local guard fix
The reported LOCAL-ONLY `evaluate_loaded` revocation fix (revoke permissions/report at entry before
fallible validation) is not on this branch. This branch does not touch `native/port240/plugin/Guard240.h`
or `verify.h`, so it should merge without conflict; its regression must be added with it.

## Verification (this branch, Linux, network-isolated)
* Backend new regressions: 41 tests in `tests/test_lifecycle_*.py`, all pass; each fix's tests were run
  against its parent commit (fail counts above). Focused suite `tests/llm/openclaw tests/test_per_character_llm.py`:
  79 passed (unchanged). Full upstream suite, network-isolated, 94163f6 vs this branch: the FAILED/ERROR set
  (147 network- or asset-dependent upstream tests) is identical; every other upstream test still passes.
* Native: `native/port240/run_tests.sh` and `native/port240/http/run_tests.sh` exit 0 (HTTP 204 checks,
  dispatcher 81, ASan/UBSan/TSan, lints). Proprietary-input checks SKIPPED (inputs unset).
* Not done: Windows compile/link of F4SE_HTTP240 with R-05, any game run, any real LLM/TTS.
