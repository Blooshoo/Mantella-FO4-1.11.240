#!/usr/bin/env bash
# Builds and runs the engine-independent F4SE_HTTP 1.11.240 port tests with the host g++ in a temp dir.
# Reads (read-only, optional): MANTELLA240_EXE, MANTELLA240_ADDRLIB, F4SEHTTP240_OFFICIAL_PSC.
# Never touches the game, MO2, or any network.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT
flags=(-std=c++20 -O1 -g -Wall -Wextra -Werror -pthread -I"$here/include" -I"$root/port240/include")

export F4SEHTTP240_UPSTREAM_PSC="${F4SEHTTP240_UPSTREAM_PSC:-$root/upstream/F4SE_HTTP/Scripts/Source/User/F4SE_HTTP.psc}"

g++ "${flags[@]}" "$here/tests/test_http_core.cpp" -o "$out/test_http_core"
g++ "${flags[@]}" "$here/tests/test_http_ids.cpp" -o "$out/test_http_ids"
g++ "${flags[@]}" "$here/tests/test_dispatcher.cpp" -o "$out/test_dispatcher"
"$out/test_http_core"
"$out/test_http_ids"
"$out/test_dispatcher"

# Same core and dispatcher under ASan+UBSan and TSan (store lock; R2-01 blocking-transport scheduling).
g++ "${flags[@]}" -fsanitize=address,undefined -fno-sanitize-recover=all "$here/tests/test_http_core.cpp" -o "$out/test_http_core_asan"
"$out/test_http_core_asan" >/dev/null
g++ "${flags[@]}" -fsanitize=address,undefined -fno-sanitize-recover=all "$here/tests/test_dispatcher.cpp" -o "$out/test_dispatcher_asan"
"$out/test_dispatcher_asan" >/dev/null
g++ "${flags[@]}" -fsanitize=thread "$here/tests/test_http_core.cpp" -o "$out/test_http_core_tsan"
g++ "${flags[@]}" -fsanitize=thread "$here/tests/test_dispatcher.cpp" -o "$out/test_dispatcher_tsan"
# TSan aborts ("unexpected memory mapping") on kernels with high mmap ASLR entropy; disable ASLR for
# this one process only (no system setting is changed).
tsan_run=("$out/test_http_core_tsan")
if command -v setarch >/dev/null 2>&1; then tsan_run=(setarch "$(uname -m)" -R "$out/test_http_core_tsan"); fi
TSAN_OPTIONS=halt_on_error=1 "${tsan_run[@]}" >/dev/null
tsan_disp=("$out/test_dispatcher_tsan")
if command -v setarch >/dev/null 2>&1; then tsan_disp=(setarch "$(uname -m)" -R "$out/test_dispatcher_tsan"); fi
TSAN_OPTIONS=halt_on_error=1 "${tsan_disp[@]}" >/dev/null
echo "sanitizers: asan+ubsan OK, tsan OK"

# Declaration lint: no plugin may claim address-library/layout independence (official F4SE then
# loads it on every 1.11.x) or RUNTIME_LATEST; each must list 1.11.240 explicitly.
python3 -I "$here/tests/lint_declarations.py" \
  "$here/plugin/F4SE_HTTP240.cpp" "$root/upstream/TopicInfoPatcher/src/main.cpp"

# Port must not reference upstream's pre-NG event IDs or the VEH/page-unprotect workaround in
# executable code (comment-aware; the gate self-tests on fixtures first).
if ! python3 -I "$here/tests/forbidden_constructs.py" --self-test "$here"/plugin/*.cpp "$here"/plugin/*.h; then
  echo "FAIL: forbidden construct in plugin source" >&2; exit 1
fi

echo "run_tests.sh: all F4SE_HTTP240 checks passed"
