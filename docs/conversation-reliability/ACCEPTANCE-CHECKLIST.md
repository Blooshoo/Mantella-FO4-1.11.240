# Owner-run acceptance: one complete conversation on a disposable save

Goal: prove one real conversation completes on Fallout 4 1.11.240 / F4SE 0.7.9 (Proton) — start, NPC
lines with matching audio and subtitles, player turns, goodbye, clean end, a second start. Run by the
owner on the owner's machine. Unit tests do not substitute for any step. Findings referenced:
[FINDINGS.md](FINDINGS.md).

## 0. Builds (record SHA-256 of each DLL and the Git SHA)
- [ ] **F4SE_HTTP.dll rebuilt from this branch** — R-05 changed `http_core.h`, which is compiled into the
      plugin; the 2d82a3b DLL does not contain it. Same recipe as `docs/build-evidence/2026-11-04/`.
- [ ] **TopicInfoPatcher.dll**: unchanged by this branch, but 94163f6 (G01/G02) still needs its real-target
      compile of `main.cpp` + `Crosshair.cpp` and link. Note whether Kalli's local `evaluate_loaded` guard
      fix is included (it is not in Git).
- [ ] MantellaSoftware started from this branch's `backend/`. COPY_BUILD stays OFF; staging is a manual,
      owner-authorised copy.

## 1. Versions that decide the outcome (O-01) — write them down
- [ ] Mantella FO4 package version (Nexus file name). If it ships `Scripts/Source/User/MantellaConversation.psc`,
      check: actions read with `getStringArray(..., KEY_ACTOR_ACTIONS)` (public b9d7298: yes → NPC actions
      are dropped with this backend; goodbye still ends thanks to R-06) or as nested dictionaries; whether
      `mantella_npc_action` / `mantella_interrupted` replies are handled (public: no → avoid them below);
      `MantellaConstants.HTTP_ERROR` value.
- [ ] Holotape HTTP timeout (default 240 polls = 120 s).

## 2. Disposable save and safe settings
- [ ] Back up the whole saves folder. Use a throwaway character/save; do not save over anything real
      (avoid sleeping/fast travel autosaves, or delete them afterwards).
- [ ] Text input (microphone off — mic interruption replies are unhandled by the public script).
- [ ] Vision off (TakeScreenShot is disabled; O-06). Advanced actions / function calling off.
- [ ] Start conversations through the **activation prompt**, not the start-conversation hotkey
      (`GetLastActorCoords` is disabled; with crosshair tracking on — the desktop default — the hotkey
      either does nothing or can pick the wrong actor; O-03). Disable crosshair tracking in the holotape if
      offered.
- [ ] FO4 voice tools present for the backend (Creation Kit LipFuzer, or FaceFXWrapper + FonixData.cdf +
      xWMAEncode + Fuz_extractor). A known-good TTS. LLM: owner's choice (prefer local/free for this run).

## 3. Run
1. [ ] Launch through F4SE, stop at the main menu. Check `My Games/Fallout4/F4SE/`:
       `F4SE_HTTP240.log` contains `F4SE_HTTP240 loaded`; `TopicInfoPatcher.log` guard lines show
       `PatchTopicInfo`, `isMenuModeActive`, `saveFloat`/`restoreFloat` enabled and the four disabled natives
       disabled; F4SE's own log shows both plugins loaded.
2. [ ] Load the disposable save.
3. [ ] **Negative probe — MantellaSoftware NOT running.** Activate a named NPC and start Mantella.
       Expected within a few seconds: notification `Received F4SE_HTTP error: request failed (WinHTTP error 12029)`
       (if the installed script uses `SKSE_HTTP_error`) and `Conversation has ended`. Record exactly what is
       shown. `Error: Could not retrieve error` means the installed script reads another key; `HTTP Timeout`
       after ~120 s means the transport did not fail fast — record both, they are findings, not passes.
4. [ ] Start MantellaSoftware and wait for `Waiting for player to select an NPC...`.
5. [ ] Start a conversation with a named NPC (one listed in `fallout4_characters.csv`).
6. [ ] NPC greeting: audible, the NPC's own voice, subtitle = audio. Note any `Patcher returned N`
       notification (-2 = line over 150 bytes, subtitle shows the previous text; known, O-04).
7. [ ] Three short player lines (H key). Never a bare action word such as `Follow`, `Wait`, `Inventory`
       (stalls with the public script; O-01). For **every** NPC line: audio matches its subtitle and is never
       a repeat of an earlier line (R-03). A line with no audio must coincide with a MantellaSoftware log
       error `No .fuz voiceline was created ...`.
8. [ ] Type `goodbye`. Expected: the NPC's goodbye line, then `Conversation has ended` within a few seconds
       (R-06). MantellaSoftware: the conversation is summarised once; the NPC's conversation history file under
       `<Mantella save folder>/data/Fallout4/conversations/` gained exactly one entry (R-04).
       **Record whether the goodbye line plays to its end or is cut off** when the conversation ends (O-10).
       Since repair pass 2 (a5ca3eb) the backend holds the end reply that answers the line request after the
       goodbye until the goodbye's estimated duration has elapsed (as it holds every next line); with an
       action-aware script the end request is still answered at once. Whether the installed script's
       `CleanupConversation` cuts an in-flight line is only observable here. Record also: no error notification,
       and `Conversation has ended` no later than ~1 s after the goodbye finishes.
9. [ ] Immediately start a second conversation (same or another NPC): it must start (backend still
       answering; R-01/R-02). End it with `goodbye` too.
10. [ ] Quit to the main menu without saving, exit. Restore/discard the throwaway save.

## 4. Collect (whatever the outcome)
`F4SE_HTTP240.log`, `TopicInfoPatcher.log`, F4SE's log, the Papyrus log if enabled, the MantellaSoftware
console/log, approximate timestamps for each step, DLL hashes, Git SHA, the versions from step 1.

## 5. Pass / stop
- **Pass:** steps 1–10 as expected, no CTD, no previous-line audio, no `Could not retrieve error`, the backend
  answers the second conversation.
- **Stop immediately** (collect logs, change nothing): CTD, the game or Papyrus hanging, a plugin refusing to
  load, or MantellaSoftware no longer answering. Engine-reader/string-lifetime (N02) and form-reload
  questions remain open whatever this run shows; one clean conversation is evidence, not certification.
