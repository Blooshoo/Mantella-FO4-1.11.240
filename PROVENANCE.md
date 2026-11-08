# Source provenance and license boundaries

Snapshot prepared 2026-11-04. Modified development work is prominently identified here; no upstream Git history is republished. SOURCE-MANIFEST.json records SHA-256 of each original imported file (before documentation-only packaging changes, the backend README rename and sanitization of one machine-path source comment). No production source bug fixes were made during publication.

## Exact local source revisions
- Backend: art-from-the-machine/Mantella upstream, https://github.com/art-from-the-machine/Mantella.git; local branch kalli/openclaw-companion-240, **e54422749c07913d769ef74ca15f210d93c850d7**. Original AGPL-3.0 license: backend/LICENSE. Includes original per-character routing and OpenClaw modifications.
- Native source aggregate: local branch kalli/port-240, **cadf8de6f4253b82cbb83447bc55a24e5a524c9d**; pristine import commit d20b9fa. This private local source history is not uploaded.
- native/upstream/TopicInfoPatcher originally https://github.com/peteben/TopicInfoPatcher at **5e0ebe688f290cfce0f4c9f79c36cb494f10d38b**. Modified for runtime guards/string patching; **GPL-3.0**, native/upstream/TopicInfoPatcher/LICENSE. A vcpkg manifest MIT field does not override this license.
- native/upstream/F4SE_HTTP originally https://github.com/Leidtier/F4SE_HTTP at **cdff83988b8e3940be92768c1a291bdf3e124c3f**. **MIT**, native/upstream/F4SE_HTTP/LICENSE; Copyright (c) 2024 Leidtier retained.
- Original native port240 work follows the linked plugin license as declared by its source provenance: GPL-3.0 for TopicInfoPatcher use; HTTP work under the F4SE_HTTP MIT terms. Retain all component notices when reusing; do not assume one blanket MIT license. This repository aggregates separately built backend/native components, not one combined binary.

## Pinned, fetched separately (not vendored)
- libxse/commonlibf4 https://github.com/libxse/commonlibf4 at **7c8c6f8a349ac85abf6fc8a7e5a71732d9339954**, GPL-3.0-or-later with its modding exceptions. Fetch submodules at their locked revisions; read its license/exceptions before redistribution. Apply native/port240/http/patches/libxse-7c8c6f8-null-array.patch to this revision only.
- Official F4SE source https://github.com/ianpatt/f4se at **6f6a7caa9aeaebc957a70d934b6ab6b80eb768b2** (0.7.9 / 1.11.240), reference only, not bundled.
- Legacy upstream F4SE_HTTP CommonLib submodule: https://github.com/Leidtier/CommonLibF4 at **a22ea1bae7f31e8d8467713d9a57852518bb8a5c**. Historical reference, NOT the selected .240 build dependency.

## Exclusions
Dspivey21/Mantella-Fallout4-v1.11.191 has no identified license grant; none of its code is included. Simple Text Field/Nexus 87074 and official packaged mod scripts/assets are not redistributed; obtain legally from their authors. The untouched FO4 game-mod baseline is not imported. Fallout4.exe, BA2/ESP/PEX/FUZ/LIP/audio/images, address-library .bin, SDK/CRT/vendor binaries, saves, models, personal config, endpoints/credentials, logs and machine handoffs are excluded. Text data/actions originally tracked by the AGPL backend remain; these are not extracted proprietary game binaries.

For asset-dependent tests/runtime, obtain the matching official Mantella distribution separately under its terms; do not copy it into Git. No Windows SDK downloads or terms acceptance is performed by this publication. Existing full requirements preserve upstream loose constraints; see handoff for the precise tested environment inventory and limitations.

## Reviewed repair integration — 2026-11-08

The candidate descends from the hosted repair-pass-2 branch (original reviewed head 65c0a3d75bb8b71db90cfc29559a981285f1a9d5); all earlier hosted repair commits, messages and authorship remain in order. Privacy-authorized owner email normalization changes affected IDs but not trees, attribution names, dates or messages. Existing build reports retain original tested revision IDs for provenance; those IDs are historical evidence, not current branch heads.

Compared all 42 inventoried backend and 20 native deltas against the hosted repair-pass-2 source. Existing hosted repairs were retained rather than copied over. Residual integration imports 15 backend source/test files and 2 native guard source/test files from the isolated November 5 repair candidate (itself based on repair pass 2 plus reviewed parent repairs). They cover NAME_MAX-safe atomic staging, explicit-end audio hold and duplicate ends, opt-in legacy package protocol, fail-closed voice artifacts and guard permission reset. No runtime/configuration/packaged asset directory was copied. REPAIR-SOURCE-MANIFEST.json records exact imported/derived bytes; the original SOURCE-MANIFEST.json remains an initial-snapshot inventory, not a hash of the final repaired tree.

The optional remote adapter is a derived November 8 implementation: removed machine-specific credential-file fallback, retained explicit bounded text routing and request guards, disabled companion/direct fallback, added cancellation/early-close fail-closed latching. Its offline tests use synthetic config and MockTransport; private launch/config/handoff data is excluded. These backend additions/modifications are under the backend AGPL-3.0 terms; native guard additions follow the TopicInfoPatcher GPL-3.0 terms. HTTP MIT notice and original GPL/AGPL license texts are unchanged. Modified author contributions are identified by this record and Git history; upstream notices are not replaced.
