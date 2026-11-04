# F4SE_HTTP → Fallout 4 1.11.240 (F4SE 0.7.9): port

Status: **source port written; engine-independent core has tests; the DLL is NOT compiled, NOT loaded,
NOT runtime-accepted.** See "States" below. Upstream: Leidtier/F4SE_HTTP `cdff839` (MIT), pristine copy in
`upstream/F4SE_HTTP/`.

## Contract actually implemented: the OFFICIAL Mantella FO4 script API, not upstream cdff839
The official package's `F4SE_HTTP.psc` (staged audit copy) differs from cdff839:
* adds `int GetHandle()`; `MantellaConversation.psc` polls it: `-1` nothing pending, `h` reply, `h+100000` error;
* comments out `valueType` (declared but never bound upstream) and `RegisterForHttpEvent`;
* listens for a synthetic `0x97` key press as a "data ready" hint, with a poll timer fallback
  (0.5 s; 0.3 s in polling/VR mode) and `HttpTimeout` counted in polls.
This port binds exactly the 25 active official natives (`include/f4sehttp240/contract.h`; the test parses the
official PSC and fails on any name/type drift). It does **not** synthesize the 0x97 key press (no input
injection); replies are picked up by the scripts' poll timer, i.e. up to one poll period later.

## Migration path chosen (minimal, licensed)
| Option | Verdict |
|---|---|
| Keep upstream CommonLibF4 `a22ea1b` (NG-era) | rejected: event path uses pre-NG `REL::ID(69733)` / `REL::ID(445184)` (FunctionArgs.h) and the library predates 1.11.x; no 1.11.240 IDs |
| Official F4SE 0.7.9 headers (hardcoded RVAs) | not chosen as the build base (would compile F4SE internals into the plugin and rewrite every native against `VMValue`/`NativeFunctionN`); **used as the independent second source** for address cross-checks |
| **libxse/commonlibf4 `7c8c6f8`** (GPL-3.0-or-later + modding exceptions; `RUNTIME_LATEST` = 1.11.240; Address Library IDs) | **chosen**: same CommonLib API family as upstream, so natives keep their shape; only the event path and version data needed redesign |

Licensing: port code is MIT (like F4SE_HTTP); a binary linked with libxse is distributed under libxse's
GPL terms. No code from the unlicensed `Dspivey21/Mantella-Fallout4-v1.11.191` was used.

## Engine ABI surface after the port
Removed entirely: Papyrus event dispatch (`DispatchMethodCall`, `BSTThreadScrapFunction`, `ArrayWrapper`,
both legacy IDs, and upstream's leaked per-reply `new ArrayWrapper`). What remains is what every libxse
native needs: `BindNativeMethod` (libxse `NF_util::NativeFunctionBase` vtable layout), stack-frame argument
unmarshalling, `BSFixedString` pool, `vm->CreateArray`, `Variable`/`Array` layouts, MemoryManager.
* Layouts `Variable` (0x10: type@0, value@8) and `Array` (refcount@0, elementType@8, lock@10, elements@18,
  0x30) agree between libxse 7c8c6f8 and F4SE 0.7.9 (`VMValue`, `VMValue::ArrayData`) — source-corroborated
  by two independent sources, **not runtime-proven**.
* Address IDs: `include/f4sehttp240/ids240.h`, verified by `tests/test_http_ids.cpp` against the local exe +
  Address Library, with F4SE 0.7.9's hardcoded RVAs as the second source: GameVM singleton,
  MemoryManager::Allocate/Deallocate must match exactly; `MemoryManager::GetSingleton` (ID 4471522) is a
  getter FUNCTION in libxse, verified by its `lea` returning F4SE `g_mainHeap` 0x3E74AB0 (the old table
  mis-kinded it as data and failed against the real exe on the first actual run); `BSStringPool::GetEntry<char>`
  is verified by F4SE's `StringCache::Ref::ctor` calling it. Stack accessors keep plausibility checks only
  (no second source). The plugin re-runs the same check on the loaded image and **refuses to load** if any
  required entry fails.

## Ownership / return contracts
See `contract.h`. Strings and arrays are copied in and returned by value; libxse creates VM-owned
`BSFixedString`/`Array` objects; the plugin retains no VM object. A None array argument is treated as empty
(`patches/libxse-7c8c6f8-null-array.patch`: libxse 7c8c6f8 dereferences null there).

## Upstream defects fixed in the core (each has a test)
D1 type confusion in getters (UB/crash) · D2 unlocked map shared by HTTP and Papyrus threads · D3 handle
space collides with the official `+100000` error encoding and grows without bound · D4 unbounded recursion on
nested-dictionary cycles / deep JSON · D5 `hasKey` not lowercasing. Also: invalid route/port, bad JSON and
HTTP errors now produce an error reply instead of silence (the scripts otherwise wait for their timeout).

Sol adversarial review 2026-11-03 fixes (each has a test; see `reports/Mantella-240-sol-remediation-2026-11-03.md`):
* **A04** request building is bounded by an aggregate node/byte budget (a 33-node DAG used to expand to 2^32
  JSON nodes under the store lock); over budget → error reply.
* **A05** no eviction of live dictionaries and no reissue of a live handle: the store fails closed when full
  (count + retained-byte budget, small reserve for error replies) and reclaims only at script lifecycle
  boundaries (`clearAllDictionaries`, which MantellaListenerScript calls on a timer while idle, and a
  `mantella_start_conversation` request, which keeps only its own graph). Residual (protocol-inherent): a script
  holding a handle across those boundaries could, after a full 1..99999 counter cycle, alias a new dictionary;
  the official scripts do not do that.
* **A06** conversation epochs: at those same boundaries queued replies are dropped, in-flight WinHTTP requests of
  the old epoch are cancelled, unsent jobs are skipped, and late completions are discarded. Re-review fixes:
  `prepare()` is one transaction (boundary + validation + snapshot + epoch capture, N07); cancellation is
  cooperative — a flag checked between WinHTTP steps, every handle used and closed only by its worker thread (N01;
  the earlier cross-thread close could use a closed/reused handle). A step already blocked runs until it returns or
  its timeout (receive 3 min); its result is discarded by epoch. Four synchronous workers can ALL be occupied by stale blocked requests, starving
  a new conversation (R2-01, unresolved). Boundaries occur only when a valid start request is actually prepared (a start skipped by
  the script's `_shouldRespond` or rejected for bad port/route/handle does not bump the epoch).
* **A07** redirects disabled (`WINHTTP_OPTION_REDIRECT_POLICY_NEVER` + `WINHTTP_DISABLE_REDIRECTS`), 3xx returned
  as an error; `WinHttpSetTimeouts` failure is now fatal for the request. (Windows-only; not executed here.)
* **A01** the plugin no longer uses the pre-libxse `REL::Module::get().base()`; base comes from
  `GetModuleHandleW(nullptr)` (what libxse's `REX::FModule::GetExecutingModule()` wraps). `run_tests.sh` rejects
  `REL::Module` in plugin sources. **A08** forbidden-construct gate is comment-aware with self-tested fixtures.

## Runtime declaration safety
Official F4SE `PluginManager::CheckPluginCompatibility` treats `AddressLibrary_1_11_137` +
`1_11_137Layout` as version independent (loads on every 1.11.x, ignores `compatibleVersions`). Upstream
F4SE_HTTP, the libxse plugin template and milestone-1 TopicInfoPatcher all set both. This port declares
`CompatibleVersions({1.11.240})` with neither independence bit, does not use the libxse plugin rule, and
the load gate re-checks runtime == 1.11.240. `tests/lint_declarations.py` enforces this for both plugins.

## States
| Item | State |
|---|---|
| core (`json.h`, `dict_store.h`, `http_core.h`, `contract.h`) | written; tests in `tests/test_http_core.cpp` (+ ASan/UBSan/TSan in `run_tests.sh`) |
| ID mapping (`ids240.h`, `ids_check.h`) | written; `tests/test_http_ids.cpp` against real exe + Address Library |
| plugin glue `plugin/F4SE_HTTP240.cpp`, `plugin/xmake.lua` | **static source only** — needs MSVC/clang-cl + Windows SDK + libxse checkout. `transport_winhttp.cpp` passed a g++ `-fsyntax-only` check against hand-written stub Win32 headers (typo check only; NOT a Windows compile, says nothing about the real SDK) |
| in-game | **not tested** |

## Build (not executed here)
Windows host (or clang-cl/lld-link + SDK), XMake ≥ 3, libxse `7c8c6f8` with submodules at
`plugin/extern/commonlibf4`, `git apply ../patches/libxse-7c8c6f8-null-array.patch`, then `xmake` in a scratch
copy with `XSE_FO4_MODS_PATH`/`XSE_FO4_GAME_PATH` unset (the recipe refuses otherwise). Output stays in
`build/`. Staging is a separate owner-authorised step.
