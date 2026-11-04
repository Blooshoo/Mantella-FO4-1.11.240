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
