# Claude Cloud handoff

## Safety and acceptance boundary
**NOT Windows-compiled / NOT game-tested.** Source development only. No live Gateway access, API key, game install or vendor binaries are needed for review/unit tests. Do not start cloud jobs or paid services automatically. Do not copy build outputs to a game/MO2 tree, enable mods, launch a game, change Gateway configuration, or connect to real LLMs. Use tool-less/mock transport only. Private hosting is intentional; preserve upstream license notices.

## Frozen status / prioritized bounded work
1. Read REVIEW-STATUS.md. Latest focused independent review: **8 fixed, 4 partially fixed**. First fix **R2-01 medium worker starvation** in native/port240/http/plugin: four synchronous stale WinHTTP requests can occupy every worker for up to the receive timeout. Add a deterministic blocking transport regression proving a fifth/new-conversation request starts within a bounded budget without releasing old barriers. Use a supported interruptible/asynchronous ownership design; do not just grow the pool or restore unsafe cross-thread handle closure. The false README guarantee was corrected in this snapshot; production code is unchanged.
2. Bound/review **N02/A03 engine/UI string-reader lifetime**. Writer serialization and 4096 retained strings are not engine quiescence. Add a defensible ownership/quiescence design or keep the unresolved limitation explicit; portable fake-pool tests do not prove engine safety.
3. Resolve **B05 accepted-then-503 ambiguity** with a mocked acceptance contract/regression. Current 503 pre-acceptance assumption is unproven. Do not blindly retry accepted/ambiguous turns. Keep character-scope transcript and cancellation limitations explicit.
4. Rerun portable suites; submit small reviewable commits and report exact gates/limitations. Full upstream suite is not a known-green baseline and some tests need assets/config/hardware. Do not fix unrelated failures blindly.
5. **Separately authorized isolated Windows compilation**, after review work: complete TopicInfoPatcher migration from older CommonLib APIs onto pinned libxse (not merely a path swap), audit reachable IDs/layouts; compile F4SE_HTTP against pinned patched libxse. Keep COPY_BUILD=OFF, unset Fallout4Path, MO2_MODS_FOLDER, SKYRIM_FOLDER, XSE_FO4_MODS_PATH, XSE_FO4_GAME_PATH. No deploy/install. A successful Linux guard or headerless COFF smoke DLL is NOT a compiled plugin.
6. **Separate runtime acceptance later**: owner-authorized disabled staging, main-menu F4SE load log, then real conversation. Crosshair lookup and Vision remain disabled/unproven; Simple Text Field contract/package remains unresolved. No runtime success may be inferred from unit tests.

## Post-handoff progress (cloud session, 2026-11-04)
- **R2-01: scheduling fixed portably; WinHTTP still uncompiled.** New `native/port240/http/include/f4sehttp240/dispatcher.h` (seats + retirement, hard thread cap, fail-fast `Saturated` error) replaces the plugin's fixed pool; regression `native/port240/http/tests/test_dispatcher.cpp` uses a cancellation-ignoring blocking transport and never releases old barriers before its assertions. The pre-fix scheduling starves on the same repro (checked in a scratch harness, not committed). Not addressed: stale calls still hold a thread and a socket until their WinHTTP timeout (no async transport). Items 2-6 untouched.

## Reproducible host-portable commands
From repository root, Linux with g++ 13+, Python 3.11, pthread, Python pytest dependencies:

    bash native/port240/run_tests.sh
    bash native/port240/http/run_tests.sh

These compile into temporary directories and run sanitizers. TSan may need permission for setarch -R in a restricted cloud container; report inability instead of marking it passed. Without optional local inputs real EXE/address-library/official-script checks are SKIPPED, not certified. To enable on an authorized local machine (never upload these inputs):

    export MANTELLA240_EXE="/path/to/legal/Fallout4.exe"
    export MANTELLA240_ADDRLIB="/path/to/legal/version-1-11-240-0.bin"
    export F4SEHTTP240_OFFICIAL_PSC="/path/to/legal/F4SE_HTTP.psc"

The scripts read these files only. Clear them for the portable-only suite.

Backend focused tests, from backend/ (venv path is caller-chosen):

    python3.11 -m venv /path/to/test-venv
    /path/to/test-venv/bin/python -m pip install -r tests/requirements-ci.txt -r tests/requirements-test.txt
    env -u MANTELLA_OPENCLAW_CONFIG PYTHONDONTWRITEBYTECODE=1 /path/to/test-venv/bin/python -m pytest -q -p no:cacheprovider tests/llm/openclaw tests/test_per_character_llm.py

Use network isolation after dependencies are installed (on Linux, unshare -rn when permitted). Upstream requirements contain loose versions; TESTED-PYTHON-ENV.txt is an exact installed distribution inventory from publication verification, not a new fully resolved portable lock. Prefer those versions for reproduction; platform-specific packages may require adjustments. Do not run all tests against live user configuration or real services. Some optional audio/STT dependencies need system libraries and are not needed by all targeted tests.

## Native Windows build recipe (not executed or certified)
Use legally provisioned MSVC 19.38+ or clang-cl 19.1.1 + LLD and matching Windows SDK/CRT; XMake >=3.0.0. SDK/CRT are not bundled. Owner had subsequently approved Microsoft terms in a separate continuation; this publication does not download packages, accept terms, or imply toolchain completion. Last recorded toolchain checkpoint had local LLVM 19.1.1 but SDK/CRT/XMake missing; fresh build host must inspect prerequisites.

In a disposable copy of native/port240/http/plugin, fetch https://github.com/libxse/commonlibf4 into extern/commonlibf4, checkout 7c8c6f8a349ac85abf6fc8a7e5a71732d9339954, initialize recursive pinned submodules, then git apply the bundled null-array patch. With a supported configured Windows compiler/SDK, xmake f -p windows -a x64 -m releasedbg followed by xmake builds the artifact only. See plugin/xmake.lua. Toolchain-specific cross configuration remains unverified; do not claim this is a tested one-command cross build. TopicInfoPatcher has NO completed libxse migration/build recipe yet; finish that bounded API migration before attempting its isolated build. Avoid the legacy upstream build dependency auto-fetch recipe as an approved .240 build.

## Evidence
Historical full-input native verification: HTTP 298 checks / 9 IDs, ASan/UBSan/TSan; adapter 72 tests. See REVIEW-STATUS.md. Publication verification results are in PUBLICATION-VERIFICATION.md. No secrets needed or supplied.
