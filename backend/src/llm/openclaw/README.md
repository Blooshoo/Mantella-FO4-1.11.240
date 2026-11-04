# OpenClaw companion route (optional, opt-in)

Lets one deliberately chosen Mantella character (default: Kalli) speak through a dedicated
OpenClaw agent session instead of Mantella's direct LLM provider, while Mantella keeps
owning everything else: STT, the sentence/TTS/lip pipeline, game actions, summaries, vision
and every other NPC. Disabled unless `openclaw_companion.json` enables it.

## How it plugs in
`ChatManager._get_per_character_client` (the existing per-character LLM seam) asks
`src/llm/openclaw/router.py` first. Only allowlisted characters in 1:1 conversations get an
`OpenClawCompanionClient`; everyone else, and every multi-NPC scene, keeps the normal client.

| Concern | Behaviour |
|---|---|
| Transport | Gateway's documented OpenAI-compatible `POST /v1/chat/completions` (`docs/gateway/openai-http-api.md` in the OpenClaw docs). `model = "openclaw/<agent_id>"`. Default transport is `mock` (offline). |
| Identity / routing | `x-openclaw-session-key: mantella-fo4:<agent>:<character-slug>-<hash(name,ref_id)>[:<conversation>]`. `session_scope: "conversation"` (default) = fresh OpenClaw session per Mantella conversation; `"character"` = one durable session per character. Reserved namespaces (`subagent:`, `cron:`, `acp:`) are rejected. |
| Conversation state | OpenClaw sessions are stateful, Mantella resends its thread. The adapter keeps only line digests (bounded) and sends just the new lines when continuity is provable; otherwise a bounded recap (`recap_messages`, char caps). The character's own previous reply is not resent. |
| Streaming | SSE deltas go straight into Mantella's sentence accumulator as `("content", text)`. |
| Cancellation | (Wrapper `_until_stopped` always cancels+joins its pending read and closes the stream on break, consumer exception, stop or outer task cancellation; applies to all Mantella clients.) `ChatManager.stop_generation` (another thread) now interrupts the stream even while it is blocked on connect, response headers, the first token, between tokens, or in retry backoff: the pending read is cancelled, which closes the HTTP response → the Gateway cancels the agent run (documented disconnect semantics; not exercised against a live Gateway). A stop never triggers a retry, fallback or tool follow-up call. The next turn resyncs. |
| First-token deadline | `first_token_timeout_s` is ONE absolute deadline started before connecting; it bounds connect + response headers + all pre-content reads. SSE comments/heartbeats and ignored frames cannot extend it. |
| Errors / reconnect | Retries (bounded, exponential, `Retry-After` capped) only when the turn demonstrably never reached the Gateway (connection refused, connect/pool timeout: retried in place) or the Gateway answered busy (429/503). Anything after possible acceptance (read/protocol errors, stream without content, first-token timeout, streamed error frame, malformed frame, HTTP 5xx other than 503) is **ambiguous** and never retried — the stateful session may already contain the turn. In `conversation` scope every turn that may have been accepted but did not complete — ambiguous error, partial EOF, reply-cap truncation, consumer stop/cancel, `[DONE]` without content (treated as a failure, so fallback applies) — moves the next turn to a fresh session epoch (`<key>:r<n>`) with a bounded recap; explicit rejections (401/403/404/400) and connect failures do not rotate. **Busy answers (B05):** that the Gateway never persists a turn before answering 429/503 is *not verified*, so by default a busy answer counts as possibly accepted: in `conversation` scope each busy retry first rotates to a fresh epoch and resends a bounded recap there, so no transcript can hold the turn twice under either contract; in `character` scope the durable key cannot rotate, so a busy answer is **not retried** (fallback policy applies, next turn resyncs). An owner who has verified the contract can set `busy_status_is_pre_acceptance: true` to restore same-session retries; if that attestation is wrong, the turn is duplicated (pinned by a test). Costs of the default: an abandoned remote session per busy retry, and a recap even when the busy answer really was pre-acceptance. `character` scope keeps its durable key and resyncs after ambiguous failures (residual: the recap can repeat the failed turn in that transcript). After speech started, a break never retries or falls back (no duplicate lines). Auth/404/400 are not retried. |
| Malformed SSE | Valid JSON with the wrong shape (non-object frame/choices/delta, non-string content) becomes a controlled transport error, so the fallback / partial-reply policy applies. |
| Log safety | Streamed `error` frames are peer-controlled and may echo prompt text or credentials; their `type`/`code` are only *matched* against an allowlist of known categories and replaced by a local constant label (else `unspecified`); no peer text is ever logged or kept. Error bodies of non-200 responses are never read (status/headers only, so they cannot bypass the deadline). The configured token is additionally redacted from `last_error`. |
| Fallback | `fallback_to_direct: true` (default): if OpenClaw is unreachable before any token, the turn is spoken by Mantella's normal provider. |
| Game actions | Never forwarded as tools. If Mantella's dedicated function-calling model is configured, it still selects actions locally; otherwise actions are off for the routed character and a warning is logged once. |
| Summaries / tokens / vision | Delegated to the direct provider; screenshots are never forwarded. |

## Trust boundary
* All game-originated text (Mantella's prompt/bio, world events, STT transcript, NPC lines, tool
  results) is **untrusted data**. It is never sent with the `system` role. It travels in one
  `user` message inside nonce-delimited blocks after a fixed adapter preamble; delimiter look-alikes,
  control and bidi characters are neutralised; everything is length-bounded. Replies are bounded too.
* The adapter never sends `tools`, `tool_choice`, `x-openclaw-model` or `x-openclaw-scopes`.
* Framing is defence in depth only. **The real control is the target agent's policy** (below):
  per the Gateway docs, a bearer token on this endpoint is full operator access and the run uses the
  target agent's tools. Do not point this at `main` or any agent with tools, private memory or SOUL
  material you would not let an arbitrary NPC prompt see. The config validator refuses `main`/`default`.
* No secret is stored in the config: `token_env` names the environment variable read at request
  time; the config loader rejects `token`/`api_key`/`password` keys. The URL must be loopback unless
  `allow_non_loopback` is set deliberately, and may not embed credentials.

## Required agent policy (owner setup, not done by this branch)
Production use needs, on the OpenClaw side, owner-authorised changes this branch does not make:
1. A dedicated agent (suggested id `fallout-kalli`) whose tool policy denies all tools (no exec,
   file, browser, messaging, memory write), with its own workspace and **no** link to the main
   agent's MEMORY/SOUL beyond an intentionally written companion persona.
2. `gateway.http.endpoints.chatCompletions.enabled` (already `true` on BlooDesk as of 2026-11-03)
   and a loopback-reachable Gateway.
3. A credential for the caller exported as `OPENCLAW_MANTELLA_TOKEN` in the backend's environment.
   Today the only documented auth for this endpoint is the shared Gateway token = full operator
   scope; a narrower credential (trusted-proxy identity with `x-openclaw-scopes: operator.write`
   only) would need its own setup. Until one of these is chosen by the owner, use `transport: "mock"`.

## Enabling (after the setup above)
Copy `src/llm/openclaw/openclaw_companion.example.json` to `<Mantella user folder>/openclaw_companion.json`
(or set `MANTELLA_OPENCLAW_CONFIG`), set `"enabled": true`, keep `"transport": "mock"` to test the
game pipeline offline, then switch to `"gateway"`. An invalid file disables the route and logs why.

## Tests
`tests/llm/openclaw/` — stdlib `unittest` (also pytest-collected): config/secrets/loopback
validation, session keys, trust framing, delta/recap, retry/fallback, mid-stream failure,
cancellation (aclose and `break`), reply bounds, actions, multi-NPC, the real Gateway transport
against an in-process `httpx.MockTransport` (request contract, SSE, error objects, 401/503), and
ChatManager-level integration through the real sentence queue. `test_sol_review_regressions.py`
covers the 2026-11-03 Sol review: heartbeat/header deadline (B01), ChatManager stop while the stream
is blocked before and after the first token (B02), malformed SSE shapes (B03), token/newline
sentinel in streamed errors (B04), stateful fake Gateway proving no replay after post-submission
failures and the session-epoch rotation (B05). `BusyAcceptanceContractTests` runs a stateful fake
Gateway under both busy contracts (persist-then-503/429 and reject-before-persist) and checks that no transcript
ever holds the player turn twice, in both session scopes, with and without the owner attestation.
