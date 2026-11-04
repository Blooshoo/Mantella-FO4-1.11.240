#!/usr/bin/env bash
# Builds the portable guard + verifier with the host g++ into a temp dir and runs them.
# Real-file tests read Fallout4.exe and the Address Library READ-ONLY.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="$(mktemp -d)"
g++ -std=c++20 -O1 -Wall -Wextra -Werror -I"$here/include" "$here/tests/test_guard.cpp" -o "$out/test_guard"
g++ -std=c++20 -O1 -Wall -Wextra -Werror -I"$here/include" -I"$here/plugin" -x c++ - -o "$out/glue" <<'CPP'
#include "Guard240.h"
int main() { return mantella240::plugin::allowed("PatchTopicInfo") ? 1 : 0; }  // must deny before evaluation
CPP
g++ -std=c++20 -O1 -Wall -Wextra -I"$here/include" "$here/tools/verify240.cpp" -o "$out/verify240"
g++ -std=c++20 -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined -fno-sanitize-recover=all -I"$here/include" "$here/tests/test_guard.cpp" -o "$out/test_guard_asan"
g++ -std=c++20 -O1 -g -Wall -Wextra -Werror -pthread -fsanitize=address,undefined -fno-sanitize-recover=all -I"$here/include" "$here/tests/test_topic_patch.cpp" -o "$out/test_topic_patch"
g++ -std=c++20 -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined -fno-sanitize-recover=all -I"$here/include" -I"$here/plugin" "$here/tests/test_plugin_gates.cpp" -o "$out/test_plugin_gates"
"$out/glue"
"$out/test_plugin_gates"
"$out/test_topic_patch"
g++ -std=c++20 -O1 -g -Wall -Wextra -Werror -pthread -fsanitize=thread -I"$here/include" "$here/tests/test_topic_patch.cpp" -o "$out/test_topic_patch_tsan"
tsan_run=("$out/test_topic_patch_tsan")
if command -v setarch >/dev/null 2>&1; then tsan_run=(setarch "$(uname -m)" -R "$out/test_topic_patch_tsan"); fi
TSAN_OPTIONS=halt_on_error=1 "${tsan_run[@]}" >/dev/null && echo "test_topic_patch: tsan OK"
"$out/test_guard"
"$out/test_guard_asan" >/dev/null && echo "test_guard: asan+ubsan OK"
if [[ -n "${MANTELLA240_EXE:-}" && -n "${MANTELLA240_ADDRLIB:-}" ]]; then
  "$out/verify240" "$MANTELLA240_EXE" "$MANTELLA240_ADDRLIB"
fi
rm -rf "$out"
