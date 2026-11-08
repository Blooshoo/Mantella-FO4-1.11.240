# Experimental source-only Fallout 4 repair

Target: **Fallout 4 1.11.240.0 / F4SE 0.7.9**, restricted typed input and generated speech. This is an independent modified Mantella aggregate, not an official release. No compiled plugin, mod package, game assets, model weights, voice tools or provider credentials are distributed.

## Evidence and limitations

The owner reported typed NPC dialogue, audible generated speech, explicit end/restart recall within the same live runtime on a local loaded checkpoint. This is owner observation, not a reproducible acceptance test of this Git revision. It does not prove restart persistence, Follow/combat, subtitle/lip-sync completeness, arbitrary NPCs or the full upstream suite. This publication does not modify or test the live game/MO2 setup.

This tree contains native source, including later HTTP error aliases and guard repairs. Exact source-to-installed-DLL pairing is unestablished; **no bit-identical rebuild or binary parity claim** is made. Historical Windows build evidence refers only to its frozen revisions. Portable tests do not compile the Windows engine integration or prove engine reader ownership/layout assumptions. Do not deploy without a separately reviewed Windows build and runtime acceptance.

Text input requires a legally obtained, compatible Simple Text Field package (Nexus Fallout 4 87074); TextInput Menu 108770 and declaration-only stubs are not equivalent. Compatibility/package integration must be checked against the actual authorized installation. No packaged scripts or assets are bundled. Linux microphone/PTT is not implemented by the restricted route. Piper is a selectable upstream TTS implementation, **not bundled or automatically configured**: obtain a licensed voice/model separately and test its settings and pipeline. Offline WAV/tool tests are not production Piper or in-game acceptance.

The remote route is for ordinary NPC text only. No private companion identity/persona, external voice integration or personality-budget repair is included. The generic OpenClaw code retains its own separately documented limitations; its mock tests do not authenticate a live identity or establish an unofficial OAuth API contract.

## Explicit route selection

The default `backend/main.py` is unchanged. `backend/main_legacy_package.py` explicitly selects the source-reviewed 0.13.0 package protocol (exact protocol/archive digest in `backend/src/http/legacy_package_protocol.py`). It refuses unsupported capabilities instead of silently coercing them.

`backend/main_remote.py` additionally opts into `src.llm.remote_adapter`. The remote client resolves **only `DEEPSEEK_API_KEY` from the process environment, lazily when a request is made**. There is no credential-file discovery/fallback. Set it using your own secure process environment; never put its value in a config file, command transcript or repository. Starting this route and sending a real request may incur provider charges and is not part of offline tests.

For both dialogue and summary, explicit config values must be:
- `llm_api` / `summary_llm_api`: `https://api.deepseek.com`
- `llm` / `summary_llm`: `deepseek-flash` (availability not established here)
- `llm_params` / `summary_llm_params`: `{"max_tokens":128,"temperature":0.4,"extra_body":{"thinking":{"type":"disabled"}}}`
- `conversation_summary_enabled` and `summary_llm_enabled` true; port 4999; desktop Fallout4; Cut narrations.
- Explicitly false: vision, game screenshots, advanced actions, interruption, random routing, per-character LLM/TTS overrides, model profiles and telemetry. Use the exact attribute/config mappings in ConfigLoader; missing values fail closed.
- Only parameterless, non-response-required basic actions. A restricted example enables EndConversation, Follow, Attack and StandDown; derive disabled actions from your exact action inventory. Do not assume the default action set is supported.

Requests are guarded to the exact HTTPS origin/path/method and bounded text-only body; proxies and redirects are disabled, provider SDK retry count is zero. An error, cancelled or early-closed stream latches the client closed. Recreating it/restarting after ambiguous acceptance is an explicit operator decision, not a replay guarantee. Generic error strings suppress provider error prose. The companion router and direct fallback are disabled for this opt-in route. No global client monkeypatch is used.

## Portable verification (no providers/game/downloads)

Use Python 3.11 with the upstream test requirements and versions in TESTED-PYTHON-ENV.txt (historical inventory; not a fresh-install lockfile). Set `PYTHON` to your prepared interpreter. On Linux, a fresh network namespace keeps these tests offline:

```sh
cd backend
unshare -rn env -u MANTELLA_OPENCLAW_CONFIG PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -m pytest -q -p no:cacheprovider tests/test_lifecycle_* tests/llm/openclaw tests/test_per_character_llm.py tests/test_atomic_name_boundary.py tests/test_legacy_package_protocol.py tests/test_paired_candidate_interactions.py tests/test_package_explicit_end_hold.py tests/test_astra_package_sequence.py tests/test_windows_voice_tools.py tests/test_remote_adapter.py
cd ..
unshare -rn bash native/port240/run_tests.sh
unshare -rn bash native/port240/http/run_tests.sh
```

Leave optional proprietary input variables unset. The voice-tool unit tests use synthetic fixtures and mocks; no Wine/game tool invocation is required. For a later separately authorized voice pipeline test, set `MANTELLA_VOICE_WINE` to your legal executable and `MANTELLA_VOICE_WORK_ROOT` to disposable isolated storage, never a game prefix. Obtain the licensed CK tools separately. Do not use live game/tool directories as writable work storage.

These are focused gates, not a full-suite green claim or deployment permission. Preserve AGPL-3.0 backend, GPL-3.0 TopicInfoPatcher and MIT HTTP notices; see PROVENANCE.md.
