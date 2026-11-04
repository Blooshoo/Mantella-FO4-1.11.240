# Actual Windows-target build evidence — 2026-11-04

## Scope and result

Tested project source: **2d82a3b2f9efdd5e6ce0738b9cb5d21b765e5876** (not a later moving branch). Independent local Linux cross build, real Microsoft STL/SDK headers/libraries, real plugin source, no game. All 294 project source files compared unchanged against the frozen revision. This evidence publication changes no engine source.

- **F4SE_HTTP.dll built and linked successfully**, not merely a headerless smoke DLL. Its main and WinHTTP translation units compiled; a dependency object subset archive linked with no unresolved-symbol overrides, placeholder functions or /force.
- **TopicInfoPatcher.dll did NOT build.** The actual compile exited 1 with **32 errors / 119 warnings**, chiefly old CommonLib APIs against the declared libxse migration target. [All 32 errors with source context](TopicInfoPatcher-errors.log) are preserved. Warning-only SDK/dependency prelude and vendor-source excerpts are omitted; no error diagnostic is omitted. Absolute build paths become `<BUILD_ROOT>` placeholders.
- No installation, enabling, game/Proton launch, live API access or runtime acceptance. One HTTP artifact is NOT the complete Mantella native replacement set.

## What the source author should do next (handoff item 5)

Inspect the [full error log](TopicInfoPatcher-errors.log), then complete the bounded TopicInfoPatcher API migration rather than swapping include paths:

1. [PCH.h](../../../native/upstream/TopicInfoPatcher/src/PCH.h):18 still aliases F4SE::log, absent in this pinned libxse. [main.cpp](../../../native/upstream/TopicInfoPatcher/src/main.cpp):21-24 uses old log_directory/fmt plumbing. Update logging initialization consistently with the declared dependency, preserving refusal/error behavior.
2. main.cpp:139,330,350 uses removed REL::Module; :255 uses REL::VariantOffset; :291 uses REL::RelocationID. Establish the actual supported 1.11.240 module/relocation API and evidence for each mapping. Do not guess old/NG/VR offsets or silently disable natives just to compile.
3. main.cpp:384 references absent F4SE::RUNTIME_LATEST_VR. Decide and document genuine supported runtime behavior; do not broaden compatibility declarations. Existing F4SE::Init(a_f4se,false) is deprecated, not a compile error by itself.
4. Preserve the guarded topic_slot::Ops ownership-transfer protocol and **all static_asserts at main.cpp:175-177**: BGSLocalizedString pointer size, BSFixedStringCS pointer size, TESResponse alignment for atomic_ref. None failed in the real-header compile, and none was weakened. That does NOT establish actual engine reader access/layout/lifetime. If a future real-header assertion fails, investigate the model rather than deleting it.
5. Re-run portable regressions, then actual Windows-target compilation/link/metadata/import checks. Keep source changes and their new SHA distinct from this frozen successful HTTP build. Report skipped gates honestly.

Preserve M01 capacity accounting, no-allocation-after-publication and sealing; preserve M02 transactional tracking. Engine reader alignment/access, competing writers/form reloads, real string/response layout/lifetime, script handling of -6, WinHTTP runtime cancellation/handle behavior, actual DLL/Proton operation and durable-character session recap continuity remain unresolved. No build result closes those gates.

## HTTP artifact declaration (binary NOT published)

[Decoded binary metadata/imports](HTTP-artifact-metadata.json):

- File F4SE_HTTP.dll, **922624 bytes**, SHA-256 **6545d67282a02840ed87629df79d9c4152e026b6156f740a8f012b5d755af14a**.
- PE32+ AMD64 DLL, normal nonzero CRT entry point; exports exactly F4SEPlugin_Load and F4SEPlugin_Version.
- Metadata version 1, plugin F4SE_HTTP version 1.240.0.0; only compatible runtime **1.11.240.0** (packed 0x010B0F00), followed by zero terminators. Address/structure-independence bits are both zero. xseMinimum is zero/unspecified: project targets F4SE 0.7.9 but this field does not enforce that minimum.
- Requires current supported Visual C++ v14 **x64** runtime and UCRT/API sets, ordinary OS DLLs including WinHTTP. Built against CRT/STL 14.44.35220. Exact direct import list is in the metadata JSON; no curl/cpr/spdlog/commonlib DLL import. libxse objects/header-only spdlog are compiled in; curl/cpr are not used.
- LLVM readobj and the independent [binary parser](verify_pe.py) both passed. Imports do not establish runtime availability under Proton or cover every dynamic lookup. No runtime DLLs are bundled; no generated DLL/import library is committed.

## Exact dependency/toolchain inputs

- Existing **Clang/LLVM/LLD 19.1.1**, x86_64-pc-windows-msvc target, no system package installation.
- **xwin 0.10.0** for legal project-local extraction. Acquire/use Microsoft SDK/CRT only with the owner's applicable license assent. This handoff contains no vendor packages and performs no download/license acceptance itself.
- Microsoft CRT/STL package family Microsoft.VC.14.44.17.14, version **14.44.35220**; SDK family **10.0.26100** selected by the frozen Microsoft manifest. Component payload subversions follow the manifest, not an assumption that every UCRT payload has the same family version.
- Top-level Microsoft manifest SHA-256 b300b5af6b2056b64861c3bb0329fe3356e24cd2c4b45e9d78a9a7ec123b1ed6; package manifest SHA-256 963760b01f621ed4fa40d1cf97aeb042f7bc6e04e3176c13c38abb19d265f789. Local inputs retained by the build owner, not uploaded here. These hashes record the observed inputs, not independent vendor signature authentication. If those exact manifests are unavailable to the reproducer, record the replacement manifest/version/hash explicitly rather than claiming identical provenance.
- [libxse/commonlibf4](https://github.com/libxse/commonlibf4): **7c8c6f8a349ac85abf6fc8a7e5a71732d9339954**, recursive commonlib-shared **29fbdb0e2dc548c9ab22f6964981d75090dc9094**.
- Apply the repository's [null-array dependency patch](../../../native/port240/http/patches/libxse-7c8c6f8-null-array.patch). That three-line guard is the ONLY dependency source modification; it is explicitly declared, not a claim of pristine upstream libxse.
- [spdlog v1.16.0](https://github.com/gabime/spdlog/tree/v1.16.0), **486b55554f11c9cccc913e11a87085b2a91f706f**, matching the dependency's package requirement. This explicit cross build uses header-only std::format/wchar spdlog instead of XMake's separately compiled logging package.

XMake >=3 was absent; it was NOT installed. The documented target/source lists were inspected and translated into the explicit build-only Python drivers below. This is not a successful execution of the checked-in XMake recipe or a full legacy CommonLib CMake build.

## Reproduction — isolated scratch build only

Use a fresh disposable repository copy and a legally provisioned SDK laid out as xwin's sdk/crt and sdk/sdk trees. Keep the evidence scripts here; set environment paths to YOUR scratch repository root and existing LLVM bin directory. No local paths, credentials or vendor files are needed from the original build host.

    export MANTELLA_BUILD_ROOT="$(pwd)"
    export LLVM_BIN="/path/to/existing/llvm/bin"
    unset Fallout4Path MO2_MODS_FOLDER XSE_FO4_MODS_PATH XSE_FO4_GAME_PATH
    mkdir -p deps obj generated evidence staging

Keep CMake COPY_BUILD=OFF if inspecting/configuring that recipe; these direct scripts never invoke CMake/XMake install hooks or any copy-to-game targets. Compiler/link subprocesses also remove the deployment variables. Never point MANTELLA_BUILD_ROOT at an active mod/game installation.

Populate dependencies without installing into a game:

    git clone --no-checkout https://github.com/libxse/commonlibf4.git deps/commonlibf4
    git -C deps/commonlibf4 checkout --detach 7c8c6f8a349ac85abf6fc8a7e5a71732d9339954
    git -C deps/commonlibf4 submodule update --init --recursive
    git -C deps/commonlibf4 apply --check ../../native/port240/http/patches/libxse-7c8c6f8-null-array.patch
    git -C deps/commonlibf4 apply ../../native/port240/http/patches/libxse-7c8c6f8-null-array.patch
    git clone --branch v1.16.0 --depth 1 https://github.com/gabime/spdlog.git deps/spdlog
    cp docs/build-evidence/2026-11-04/api_macro_compat.h generated/

Provision SDK/CRT separately under applicable legal permission; these scripts do not fetch it. Then:

    python3 docs/build-evidence/2026-11-04/compile_plugin.py http
    python3 docs/build-evidence/2026-11-04/compile_plugin.py transport
    python3 docs/build-evidence/2026-11-04/compile_plugin.py topic

The last command fails on this frozen project source for the logged migration reasons. It generates ordinary Version.h from the checked-in template. For every diagnostic rather than the normal first-20-error cutoff:

    PYTHONPATH=docs/build-evidence/2026-11-04 python3 -c 'import compile_plugin as b; raise SystemExit(b.compile_one("native/upstream/TopicInfoPatcher/src/main.cpp", "topic-full", ["/FI"+str(b.R/"native/upstream/TopicInfoPatcher/src/PCH.h"), "-ferror-limit=0"]))'

Common compiler flags (full include paths in [compile_plugin.py](compile_plugin.py)):

    --target=x86_64-pc-windows-msvc /std:c++latest /EHsc /MD /O2 /DNDEBUG
    /D_CRT_USE_BUILTIN_OFFSETOF /DUNICODE /D_UNICODE /DNOMINMAX /DWIN32_LEAN_AND_MEAN
    /DSPDLOG_USE_STD_FORMAT /DSPDLOG_WCHAR_TO_UTF8_SUPPORT -fms-compatibility-version=19.44

_CRT_USE_BUILTIN_OFFSETOF is the SDK-supported clang constexpr offset control. It preserves every sizeof/alignof/offsetof static assertion; no checks are disabled. This build is not warning-clean: 64 HTTP main warnings, 73 first transport warnings. Dependency sweeps use /W0 but preserve errors/assertions.

### Dependency subset/link details — do not mistake a partial library build for success

    python3 docs/build-evidence/2026-11-04/compile_dependencies.py

That full source sweep attempted 98 units: 95 initially passed, three failed. Expect exit 1, not a clean library result:

- F4SE/API.cpp: Windows MAX_PATH/ERROR macros from spdlog conflict with qualified REX names.
- F4SE/RegistrationMap.cpp:325,354 and RegistrationMapUnique.cpp:406,435: bare return in non-void Register/Unregister. These two units are unused by HTTP and are NOT patched with invented results.

    python3 docs/build-evidence/2026-11-04/link_http.py

The link driver recompiles API.cpp with [api_macro_compat.h](api_macro_compat.h): parse libxse first, include the original logging headers, undefine the two Windows macros. This is generated build-only include/macro hygiene, not a stub implementation or dependency logic rewrite. It archives the **96 successfully compiled objects**, excluding the two failed unused registration-map units; final HTTP link exits **0** with all required symbols resolved. No /force or dummy routines. Do not reuse stale object files from another build/flags revision: start with an empty obj directory.

Link flags: /dll /machine:x64 /opt:ref /opt:icf /incremental:no /timestamp:0, the three CRT/UCRT/UM x64 library paths, HTTP/transport objects and subset library; OS libraries kernel32, winhttp, advapi32, bcrypt, d3d11, d3dcompiler, dbghelp, dxgi, ole32, shell32, user32, version, ws2_32. Normal compiler directives select dynamic CRT libraries. Scripts save exact argument arrays and logs under scratch evidence/. Outputs remain scratch staging/.

    "$LLVM_BIN/llvm-readobj" --file-headers --coff-exports --coff-imports staging/F4SE_HTTP.dll
    python3 docs/build-evidence/2026-11-04/verify_pe.py
    sha256sum staging/F4SE_HTTP.dll

The binary parser checks this candidate's runtime/export/flag contract; adapt deliberately if a reviewed future source revision changes that declaration. Timestamp zero does not claim a second independent bit-identical rebuild. Scripts here are sanitized, parameterized derivatives of the executed local build drivers, syntax checked; the original actual execution and its output are the evidence, not a claim that these relocated scripts were already executed end-to-end in Cloud.

## M02 independent correctness verdict

Independent check of 2d82a3b against 295ca9e: **M02 fixed for the portable implementation**, M01 accounting/ownership/sealing remains intact; no new demonstrated high/medium production defect. Fresh equivalent 4096-record fixtures calibrated **7 allocations** for a long-text rollover. All seven injected failures returned -6, retained all records and preserved modeled references/slot, with no unexpected sealing. All 4096 prior records passed eligibility checks; first/middle/last were actually repatched. Uninjected success tracked only the new record; old responses returned the documented -3.

Same independent driver against 295ca9e failed as expected: fault points 5/6 wiped tracking. Additional fresh cold/steady long-text sweeps hit **10/8 allocation failures**, all passing under ASan/UBSan with leak detection. Supplied native and HTTP portable gates passed, including TSan; HTTP core 195 and dispatcher 81 checks, zero failures. Real input/official PSC checks were skipped, not certified. Backend unchanged and not rerun. The committed rollover regression retries in evolving state; independent fresh-state coverage removes that isolation caveat without pretending the committed test is an exhaustive identical-state sweep. These are observed local results, not new Cloud runs and not DLL runtime tests.

Portable commands from repository root:

    env -u MANTELLA240_EXE -u MANTELLA240_ADDRLIB -u F4SEHTTP240_OFFICIAL_PSC bash native/port240/run_tests.sh
    env -u MANTELLA240_EXE -u MANTELLA240_ADDRLIB -u F4SEHTTP240_OFFICIAL_PSC bash native/port240/http/run_tests.sh

## Publication/privacy/license boundary

Only evidence text, JSON metadata and first-party build-only drivers/shim are included. No generated DLL/lib/object, SDK/vendor binary/header/library, compiler distribution, proprietary game data, manifest URL dump, credentials, private transcript or machine-specific absolute path is included. The error log retains repository source excerpts and diagnostic file/line references; copied vendor-header excerpts are removed. Scripts import headers by name, not by vendoring their contents.

Retain upstream notices and pinned source provenance. HTTP upstream/port MIT does not erase GPL-3.0 obligations of linked libxse/commonlib-shared; spdlog retains its MIT notice. No legal permission to redistribute SDK/game inputs is implied. This is a private evidence handoff, not a binary release, game deployment, runtime certificate or authorization to start live tests.
