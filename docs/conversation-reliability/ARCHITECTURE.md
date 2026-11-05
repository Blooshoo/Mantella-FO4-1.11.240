# Conversation lifecycle — architecture and state machines (FO4 1.11.240 port)

Cloud review 2026-11-05. Starting commit **94163f6** (branch `ccr-a959bf09-5dtmxx`; `main` is only the
17b6d33 snapshot). Source-level map, not runtime evidence. Findings and fixes: [FINDINGS.md](FINDINGS.md);
owner acceptance run: [ACCEPTANCE-CHECKLIST.md](ACCEPTANCE-CHECKLIST.md).

## What is and is not in this repository

| Component | Here? | Exact source used for this map |
|---|---|---|
| Papyrus game scripts (`MantellaConversation`, `MantellaRepository`, `MantellaListenerScript`, `MantellaConstants`, `F4SE_HTTP.psc` with `GetHandle`) | **No** (official package, not redistributable here) | Public reference only: `YetAnotherModder/fallout-4-mantella-mod` **b9d72983** (2025-03-30, newest public); older `art-from-the-machine/fallout-4-mantella-mod` 15f9b132 (2024-07-24). **Not proven to be the installed version** — see version mismatch below. The official `F4SE_HTTP.psc` (GetHandle variant) is only reachable via `F4SEHTTP240_OFFICIAL_PSC`. |
| F4SE_HTTP240 (HTTP natives) | Yes | `native/port240/http` (core: `http_core.h`, `dict_store.h`, `dispatcher.h`; plugin `F4SE_HTTP240.cpp`, `transport_winhttp.cpp`) |
| TopicInfoPatcher (subtitle + settings natives) | Yes | `native/upstream/TopicInfoPatcher/src`, guard `native/port240` |
| Python backend (MantellaSoftware) | Yes | `backend/` (art-from-the-machine/Mantella derivative, Kalli's branch e544227 + later commits) |
| Simple Text Field (text input menu), FaceFXWrapper / LipFuzer / xWMAEncode / Fuz_extractor, TTS servers, LLM | No | external; behaviour not assumed beyond their call sites |

**Version mismatch (proven from source, consequence depends on what is installed).** The public scripts read
NPC actions with `getStringArray(h, "mantella_actor_actions")` (b9d7298 `MantellaConversation.psc:316`); the
FO4 maintainer's backend fork sends plain strings (`YetAnotherModder/Mantella` b060fe1,
`conversation.py:299`), but **this** backend sends dictionaries (`{'identifier': ...}`,
`actions_parser.py:23`, `conversation.py:462`). With the port's typed store that array reads as empty, so
every action is silently dropped. The installed script version must be checked (checklist step 1).

## Components, threads, ownership

| Stage | Owner / thread | Blocking? | Bound |
|---|---|---|---|
| Papyrus conversation quest (`MantellaConversation`) | Papyrus VM threads | No (timer polling) | `HttpTimeout = HTTPTimeOutHolotapeValue` polls (default 240 × 0.5 s = **120 s**, b9d7298 `:219/:480-481`) |
| `F4SE_HTTP.sendLocalhostHttpRequest` → `HttpCore::prepare` | Papyrus thread | No network I/O | request build budget (D6) |
| `Dispatcher` seat thread → `win::post` (WinHTTP, synchronous) | plugin worker thread (4 seats + ≤12 retired) | Yes | connect = script `timeout` ms or 60 s, send 30 s, **receive 180 s per step** (`transport_winhttp.cpp:32,51`) |
| `HttpCore::complete` → `DictStore` / `ReplyQueue` | worker thread | No | queue 256 (drop-oldest), store 50 000 dicts / 256 MiB, 64 error-reserve |
| `F4SE_HTTP.GetHandle` poll | Papyrus timer (0.5 s; 0x97 key hint not synthesized) | No | — |
| uvicorn `POST /mantella` (`mantella_route.py:66`, `async def` calling synchronous handlers) | **the single event-loop thread** | **Yes — the whole server is serialized behind each request** | none of its own |
| `GameStateManager` / `Conversation` | event-loop thread | `continue_conversation` waits on `SentenceQueue.get_next_sentence` (no timeout) and busy-waits the previous line's duration | voiceline duration (R-02), generation end (R-01) |
| `ChatManager.generate_response` → `process_response` (LLM stream, parsers, TTS) | one generation thread per response (`asyncio.run`) | LLM read, TTS (under `tts_access_lock`) | openai client default 600 s/request + retries; TTS: openai-compatible 5/60 s, **xVASynth/XTTS synthesis requests have no timeout** (`xvasynth.py:248`, `xtts.py:77`) |
| `TTSable.synthesize` → `_generate_voiceline_files` (LipGen/FaceFX `.lip`, LipFuzer/xWMAEncode+Fuz_extractor `.fuz`) | generation thread | subprocesses | none |
| `Fallout4.prepare_sentence_for_game` → copy `save/<line>.fuz` to `Sound/Voice/Mantella.esp/MantellaVoice00/00001ED2_1.fuz` (topic 1) or `0005E7AC_1.fuz` (topic 2) | event-loop thread | file copy | — |
| Papyrus `NpcSpeak`: `SetOverrideVoiceType(MantellaVoice)`, `PatchTopicInfo(topic, line)`, `Say(topic)` | Papyrus | engine | — |

There are **no request IDs** anywhere in the official protocol. Correlation is: the script is strictly
request → reply sequential within a conversation; the native `ReplyQueue` is FIFO; the native **epoch**
(bumped by a `mantella_start_conversation` request and by `clearAllDictionaries`) drops queued replies,
cancels in-flight calls (cooperatively) and discards late completions of older epochs. The backend has a
single `GameStateManager.__talk` (one conversation at a time).

## Request/reply contract (backend `communication_constants.py`, script b9d7298)

| Request `mantella_request_type` | Backend reply `mantella_reply_type` | Script reaction |
|---|---|---|
| `mantella_start_conversation` (actors, context, input type) | `mantella_start_conversation_completed` (+ `mantella_use_narrator`) | `RequestContinueConversation` |
| `mantella_continue_conversation` (+ `mantella_topicinfofile` 1/2, alternating) | `mantella_npc_talk` + nested `mantella_npc_talk` {speaker, line, is_narration, voice_file, duration, actions, topicinfofile} | `ProcessNpcSpeak` → `NpcSpeak` → next continue |
| | `mantella_npc_action` (action-only: advanced actions / tool calls) | b9d7298 has **no branch and no constant** for it (`ContinueConversation` `:211-269`): the reply is consumed, no request is pending, the conversation stalls until ended by a hotkey/listener |
| | `mantella_player_talk` | text: "Awaiting player text input" (H key, Simple Text Field); mic: transcribe request |
| | `mantella_tts` (mic transcript) | player input request |
| | `mantella_interrupted` (mic interruption) | b9d7298: **no branch** — stalls as above |
| | `mantella_end_conversation` | `CleanupConversation` |
| | `error` + `mantella_message` | notification + `CleanupConversation` (no end request) |
| `mantella_player_input` | `mantella_npc_talk` (voiced player line or empty) / `mantella_tts` | continue |
| | `mantella_npc_action` when the whole player line equals an action key (`Follow`, `Wait`, `Inventory`, ... — `game_manager.py` `player_input`) | b9d7298: **no branch** — stalls |
| `mantella_end_conversation` | `mantella_end_conversation` | `CleanupConversation` |
| native error (GetHandle ≥ 100000) | error dict | `OnHttpErrorReceived`: reads `MantellaConstants.HTTP_ERROR = "SKSE_HTTP_error"` → notification + `CleanupConversation` |

## State machines

### Game script (public b9d7298; installed version unverified)

```
Idle ──activation perk / start hotkey (crosshair: GetLastActorCoords, disabled on .240)──> Starting
Starting ──start request──> AwaitReply(120 s)
AwaitReply ──npc_talk──> Speaking ──PatchTopicInfo (result only notified) + Say(topic N)──> AwaitReply (next continue)
AwaitReply ──player_talk──> AwaitPlayer ──text (Simple Text Field) / mic──> AwaitReply (player_input)
AwaitReply ──end_conversation | error reply | native error | HttpTimeout──> CleanupConversation ──> Idle
any ──EndConversation() (end action, end hotkey, combat/distance listeners, load game)──> AwaitReply(end) ──> Idle
Idle, every 3000 s ──clearAllDictionaries (only if the quest is not running)──> Idle   (native epoch boundary)
```
`CleanupConversation` after an error/timeout never tells the backend; the backend conversation lingers until
the next start (which ends it) or an end request.

### Native F4SE_HTTP240 per request

```
prepare(handle)  ── invalid port/route/handle, over budget ──> error reply (current epoch)
  │ start request: epoch++  (queue cleared, old in-flight cancel hooks -> seats retired + refilled)
  v
Queued (≤32) ── Full / Saturated ──> error reply
  v
InFlight(epoch e) ── WinHTTP failure/timeout/cancel ──> complete(e, 0)  ─┐
  │                                                                         ├─ e stale → discarded
  └── HTTP status ──> complete(e, status, body) ─── non-200 / bad JSON / store full ──> error reply
                                                └── object ──> reply handle → ReplyQueue → GetHandle
```

### Backend conversation (after this branch's fixes)

```
no __talk ──start──> Conversation(started: greeting → generation thread | player_talk)
continue: ended? ──yes──> end_conversation reply, conversation saved once and dropped (R-04, R-06)
          is_ending and goodbye delivered? ──yes──> end_conversation reply, saved, dropped (R-06)
          queue: sentence ──> npc_talk (FO4 slot replaced, or removed if no .fuz — R-03)
                 error sentence ──> error reply, conversation ended (kept until next start/end request)
                 end of response ──> should_end? → goodbye (is_ending) | auto user message → generation | player_talk
generation thread: ALWAYS ends the response — normal sentinel, or an error sentence if it could not (R-01);
                   terminator queued before is_more_to_come=False; the waiting consumer re-checks that
                   flag every 0.1 s, so a cleared terminator cannot strand it (R-01b);
                   unreadable/streamed audio → TTS error sentence / real duration (R-02)
end request ──> end() (stop generation, save once) ──> dropped
```

## Disabled natives (TopicInfoPatcher, four of eleven) — callers in the public scripts

| Native | Port behaviour | Public caller (b9d7298) | How the result is used | Consequence on .240 |
|---|---|---|---|---|
| `ClearCache()` | no-op (void) | none | — | none |
| `SetOverrideFileName(topic, name)` | no-op (void) | none | — | none: speech uses the two pre-named topic-info `.fuz` slots |
| `TakeScreenShot(name, type, sstype)` | no-op (void) | `MantellaRepository.psc:781` `GenerateMantellaVision` (only when Vision is enabled; default off `:205`) | none (void) | backend `image_manager.get_image` finds no `Mantella_Vision.jpg` and returns no image (silently); leftovers are deleted at startup, so no stale image |
| `GetLastActorCoords()` | empty `float[]` | `MantellaRepository.psc:582-583`, start-conversation hotkey, when `allowCrosshairTracking` (set **true** on desktop, `:659`) | `Actorxyz[0..2]` indexed **without a length check**, then `Game.FindClosestActor(x, y, z, 20)` | hotkey start unusable; if Papyrus continues past the out-of-range access with 0.0 (engine semantics unverified) it can pick an unrelated actor within 20 units of the cell origin. Start conversations through the activation prompt. |

`PatchTopicInfo` (enabled) return codes: 0 ok, -1 None topic, -2 > 150 bytes, -3 not the placeholder/our
text, -4 guard disabled, -5 bad record, **-6 retention exhausted/sealed**. The public script
(`MantellaConversation.psc:336-339`) only shows `"Patcher returned " + ret` and still calls `Say`: the line
plays with the previous (or placeholder) subtitle. None of these codes stops the conversation.
